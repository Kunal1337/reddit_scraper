"""
steam_scraper.py — Steam Community Discussions scraper.

Pulls discussion threads (the original post = "post") and their replies
(= "reply") from one or more game hubs, and appends to a single deduplicated
CSV in the shared schema (so Reddit data can later flow into the same file).

Built to collect OVER TIME:
  - Threads aren't dropped just because they scroll off the "recent" list.
    Any thread first seen within KNOWN_THREAD_MAX_AGE_DAYS keeps getting
    re-checked for new replies, using what's already in the CSV as memory.
  - Each thread's replies are paginated (not just the first page), stopping
    early once a page brings back nothing new — so long threads accumulate
    properly instead of being capped at the first N replies forever.

There is no official API for Steam discussions, so this scrapes HTML with
requests + BeautifulSoup. That makes the CSS SELECTORS the fragile part:
if a run comes back with 0 records or empty text, use the --inspect helper
to check a live page and fix the SELECTORS block. The reply-pagination
pattern (SELECTORS["ctp_param"] below) is the other fragile bit — if a long
thread's replies never grow past the first page, that's usually why.
Open a long thread in a browser, click through its comment pages, and check
the URL pattern it actually uses.

Setup (once):
    pip3 install requests beautifulsoup4

Run:
    python3 steam_scraper.py

Verify/fix selectors on a live page:
    python3 steam_scraper.py --inspect https://steamcommunity.com/app/4032350/discussions/
"""

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# --- Config -----------------------------------------------------------------

# Map each Steam app ID to the game it represents. The app ID lives right in
# the discussion URL: steamcommunity.com/app/<APPID>/discussions/
# Add or remove games here — one run scrapes all of them into the same CSV.
APPID_TO_GAME = {
    "4032350": "EA Sports College Football 27",
    "3940610": "Madden NFL 27",
    "2807960": "Battlefield 6",
    "1172470": "Apex Legends",
    "1222670": "The Sims 4",
}

FORUM_PAGES_PER_APP = 2       # how many pages of the thread list to walk for FRESH threads
THREADS_PER_APP = 15          # cap on fresh threads scraped per game, per run
KNOWN_THREAD_MAX_AGE_DAYS = 30  # keep re-checking a thread for new replies for this many days after its first post
MAX_REPLY_PAGES = 5           # walk up to this many comment pages per thread, per run
REQUEST_DELAY = 2.0           # seconds between requests — be polite, avoid throttling
OUTPUT_CSV = "engagement_data_steam.csv"

# A real browser-like user agent; Steam is picky about bare-bones clients.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# --- SELECTORS (the fragile bit — verify with --inspect if runs come back empty) ---
SELECTORS = {
    "thread_link": "a.forum_topic_overlay",      # links on the forum list page
    "op_author":   ".forum_op .forum_op_author, .authorline .hoverunder",
    "op_text":     ".forum_op .content",          # original post body
    "reply_block": ".commentthread_comment",      # each reply container
    "reply_text":  ".commentthread_comment_text",
    "timestamp_attr": "data-timestamp",           # unix ts attribute Steam sets
    "ctp_param": "ctp",                           # query param for comment page N — VERIFY on a live long thread
}

# Verified directly against a live locked Steam thread: locked topics show a
# distinctly-named icon asset right next to "This topic has been locked" text
# near the top of the page. A plain substring check on the raw HTML is more
# robust here than guessing a CSS selector, since we don't have the exact
# containing element's class name — only confirmed evidence of this filename.
# If lock-detection ever silently stops working, this string is the first
# thing to re-verify (same spirit as SELECTORS above).
LOCKED_ICON_MARKER = "forum_topicicon_locked"

# The shared schema — identical to the Reddit scraper's columns.
FIELDNAMES = [
    "source", "author_id", "action_type", "game",
    "text", "timestamp", "score", "sentiment", "topic", "permalink", "locked",
]


# --- HTTP helper ------------------------------------------------------------

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 5
MAX_CONSECUTIVE_THROTTLE_FAILURES = 5  # stop the WHOLE run if we hit this many blocked requests in a row

_consecutive_throttle_failures = 0


class ThrottledStop(Exception):
    """Raised when too many requests in a row look like throttling. Signals
    the caller to stop the entire run rather than continuing to push against
    what looks like a block."""
    pass


def get(session, url):
    """Fetch a URL politely, with a delay and mature-content cookie set.
    Retries with backoff on throttling-style responses (429/503) instead of
    silently treating them the same as any other failure. If throttling keeps
    happening across many requests in a row, raises ThrottledStop so the
    whole run halts instead of grinding through the rest of the games."""
    global _consecutive_throttle_failures
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY)
        try:
            resp = session.get(
                url, headers=HEADERS,
                cookies={"wants_mature_content": "1", "birthtime": "0"},
                timeout=20,
            )
        except requests.exceptions.RequestException as e:
            last_exc = e
            print(f"    ! network error (attempt {attempt}/{MAX_RETRIES}) for {url}: {e}")
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            wait = BACKOFF_BASE_SECONDS * attempt
            print(f"    ! got HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES}) for {url} — waiting {wait}s")
            time.sleep(wait)
            last_exc = requests.exceptions.HTTPError(f"HTTP {resp.status_code} after {attempt} attempts")
            continue

        resp.raise_for_status()
        _consecutive_throttle_failures = 0
        return resp.text

    _consecutive_throttle_failures += 1
    if _consecutive_throttle_failures >= MAX_CONSECUTIVE_THROTTLE_FAILURES:
        raise ThrottledStop(
            f"{_consecutive_throttle_failures} requests in a row failed after retries — "
            "stopping the run rather than continuing to push against a likely block."
        )
    raise last_exc


# --- Parsing helpers --------------------------------------------------------

def clean(text):
    return " ".join(text.split()).strip() if text else ""


def read_timestamp(node):
    """Try to pull a unix timestamp attribute and convert to ISO; else blank."""
    if node is None:
        return ""
    el = node.select_one(f"[{SELECTORS['timestamp_attr']}]")
    if el and el.has_attr(SELECTORS["timestamp_attr"]):
        try:
            ts = int(el[SELECTORS["timestamp_attr"]])
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            pass
    return ""


def extract_author(block):
    """Find a reply's author via its profile link, not a fragile class name.
    Skips profile links that sit inside quoted text (e.g. 'Originally posted by X')."""
    text_el = block.select_one(SELECTORS["reply_text"])
    quoted_links = set(text_el.find_all("a")) if text_el else set()
    for a in block.find_all("a", href=True):
        href = a["href"]
        if "steamcommunity.com/id/" in href or "steamcommunity.com/profiles/" in href:
            if a in quoted_links:
                continue  # this is a quoted user, not the reply's author
            name = clean(a.get_text())
            if name:
                return name
    return ""


def make_record(author_id, action_type, game, text, timestamp, permalink, locked=False):
    """One row in the shared schema. Steam has no per-post score, so it's blank."""
    return {
        "source": "steam",
        "author_id": author_id or "[unknown]",
        "action_type": action_type,
        "game": game,
        "text": text,
        "timestamp": timestamp,
        "score": "",        # Steam discussions have no per-post upvote score
        "sentiment": "",     # placeholder — filled in later
        "topic": "",         # placeholder — filled in later
        "permalink": permalink,
        "locked": "true" if locked else "false",
    }


# --- Dedup / memory helpers --------------------------------------------------

def fingerprint(record):
    """Unique-enough key for a post/reply: which thread + who + when."""
    return f"{record['permalink']}|{record['author_id']}|{record['timestamp']}"


def load_existing_fingerprints(path):
    """Read fingerprints already saved, so we don't append duplicates."""
    seen = set()
    if not os.path.exists(path):
        return seen
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            seen.add(fingerprint(row))
    return seen


def load_known_threads(path):
    """Map each previously-seen Steam permalink -> {game, first_seen}, using
    the CSV itself as memory. This is what lets a thread keep being checked
    for new replies even after it scrolls off Steam's 'recent' list."""
    known = {}
    if not os.path.exists(path):
        return known
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("source") != "steam":
                continue
            perma = row.get("permalink", "")
            if not perma:
                continue
            ts = row.get("timestamp", "")
            entry = known.setdefault(perma, {"game": row.get("game", ""), "first_seen": ts})
            if ts and (not entry["first_seen"] or ts < entry["first_seen"]):
                entry["first_seen"] = ts
    return known


def within_recheck_window(first_seen_iso, max_age_days):
    if not first_seen_iso:
        return False
    try:
        posted = datetime.fromisoformat(first_seen_iso)
    except ValueError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return posted >= cutoff


# --- Core scrape ------------------------------------------------------------

def get_thread_urls(session, appid, pages):
    """Walk the forum list pages and collect thread URLs."""
    urls = []
    base = f"https://steamcommunity.com/app/{appid}/discussions/0/"
    for page in range(1, pages + 1):
        page_url = f"{base}?fp={page}"
        try:
            html = get(session, page_url)
        except Exception as e:
            print(f"  !! could not load forum page {page}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select(SELECTORS["thread_link"]):
            href = a.get("href")
            if href and href not in urls:
                urls.append(href)
    return urls


def scrape_thread(session, url, game, existing_fingerprints, first_page_html=None):
    """Scrape one thread: the original post, then paginate through reply
    pages until a page brings back nothing new or MAX_REPLY_PAGES is hit.
    If first_page_html is provided (e.g. a caller already fetched page 1 to
    check the post date), it's reused instead of fetching the same URL twice."""
    records = []
    if first_page_html is not None:
        html = first_page_html
    else:
        try:
            html = get(session, url)
        except Exception as e:
            print(f"  !! could not load thread {url}: {e}")
            return records

    soup = BeautifulSoup(html, "html.parser")
    is_locked = LOCKED_ICON_MARKER in html

    # --- original post (action_type = "post") ---
    op = soup.select_one(".forum_op")
    if op:
        author_el = op.select_one(SELECTORS["op_author"])
        text_el = op.select_one(SELECTORS["op_text"])
        records.append(make_record(
            author_id=clean(author_el.get_text()) if author_el else "",
            action_type="post",
            game=game,
            text=clean(text_el.get_text()) if text_el else "",
            timestamp=read_timestamp(op),
            permalink=url,
            locked=is_locked,
        ))

    # --- replies (action_type = "reply"), paginated ---
    page = 1
    while page <= MAX_REPLY_PAGES:
        if page > 1:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}{SELECTORS['ctp_param']}={page}"
            try:
                html = get(session, page_url)
            except Exception as e:
                print(f"  !! could not load reply page {page} for {url}: {e}")
                break
            soup = BeautifulSoup(html, "html.parser")

        blocks = soup.select(SELECTORS["reply_block"])
        if not blocks:
            break  # no more reply pages

        new_count = 0
        for block in blocks:
            text_el = block.select_one(SELECTORS["reply_text"])
            text = clean(text_el.get_text()) if text_el else ""
            if not text:
                continue  # skip empty/deleted
            record = make_record(
                author_id=extract_author(block),
                action_type="reply",
                game=game,
                text=text,
                timestamp=read_timestamp(block),
                permalink=url,
                locked=is_locked,
            )
            if fingerprint(record) not in existing_fingerprints:
                new_count += 1
            records.append(record)

        # Page 1 is always worth capturing (dedup handles repeats at write
        # time). Beyond that, stop once a page brings nothing new — we've
        # caught up to what we already have for this thread.
        if page > 1 and new_count == 0:
            break
        page += 1

    return records


def scrape_app(session, appid, game, known_threads, existing_fingerprints):
    print(f"Scraping Steam discussions for {game} (app {appid})...")

    fresh_urls = get_thread_urls(session, appid, FORUM_PAGES_PER_APP)[:THREADS_PER_APP]
    fresh_set = set(fresh_urls)

    # Bring back older threads for this game that are still inside the
    # re-check window, so they keep accumulating replies instead of being
    # dropped the moment something newer bumps them off the front page.
    recheck_urls = [
        perma for perma, info in known_threads.items()
        if info["game"] == game
        and perma not in fresh_set
        and within_recheck_window(info["first_seen"], KNOWN_THREAD_MAX_AGE_DAYS)
    ]

    all_urls = fresh_urls + recheck_urls
    print(f"  {len(fresh_urls)} fresh threads + {len(recheck_urls)} re-checked older threads = {len(all_urls)} total")

    records = []
    for url in all_urls:
        records.extend(scrape_thread(session, url, game, existing_fingerprints))
    print(f"  -> {len(records)} records")
    return records


def scrape_all(appid_to_game, known_threads, existing_fingerprints):
    session = requests.Session()
    all_records = []
    for appid, game in appid_to_game.items():
        try:
            all_records.extend(scrape_app(session, appid, game, known_threads, existing_fingerprints))
        except ThrottledStop as e:
            print(f"  !! stopping the entire run early: {e}")
            break
        except Exception as e:
            print(f"  !! failed on app {appid}: {e}")
    return all_records


# --- CSV output: append only new records ------------------------------------

def append_new(records, path=OUTPUT_CSV, seen=None):
    if seen is None:
        seen = load_existing_fingerprints(path)
    new = [r for r in records if fingerprint(r) not in seen]
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()   # only write header the first time
        writer.writerows(new)
    print(f"\n{len(records)} scraped, {len(new)} new appended, "
          f"{len(records) - len(new)} duplicates skipped -> {path}")


# --- Inspection helper (use when selectors need fixing) ---------------------

def inspect_page(url):
    """Dump a page's structure so you can verify/fix the SELECTORS block."""
    session = requests.Session()
    html = get(session, url)
    soup = BeautifulSoup(html, "html.parser")
    print(f"\nInspecting: {url}\n" + "=" * 60)
    checks = {
        "thread_link (forum list only)": SELECTORS["thread_link"],
        "forum_op (thread page only)": ".forum_op",
        "op_text (thread page only)": SELECTORS["op_text"],
        "reply_block (thread page only)": SELECTORS["reply_block"],
        "reply_text (thread page only)": SELECTORS["reply_text"],
    }
    for label, sel in checks.items():
        matches = soup.select(sel)
        print(f"  {label:38s} -> {len(matches)} match(es)  [{sel}]")
    print("=" * 60)
    print("0 matches on a selector = that selector needs updating in SELECTORS.")
    print("Tip: open the same URL in your browser, right-click an element,")
    print("     'Inspect', and read the real class names.")
    print()
    if LOCKED_ICON_MARKER in html:
        print(f"Locked-thread marker '{LOCKED_ICON_MARKER}' FOUND on this page — "
              "if this page is NOT actually locked, that's a false positive to investigate.")
    else:
        print(f"Locked-thread marker '{LOCKED_ICON_MARKER}' not found on this page "
              "(expected, unless you're inspecting a thread you know is locked).")
    print()
    print("To verify reply pagination (SELECTORS['ctp_param']): open a thread")
    print("with 40+ replies in your browser, click to a later comment page,")
    print("and check whether the URL matches '<thread_url>?ctp=2' style —")
    print("if it uses something else, update ctp_param or the page_url logic")
    print("in scrape_thread() to match.")


# --- Entry point ------------------------------------------------------------

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--inspect":
        inspect_page(sys.argv[2])
        return
    known_threads = load_known_threads(OUTPUT_CSV)
    existing_fingerprints = load_existing_fingerprints(OUTPUT_CSV)
    records = scrape_all(APPID_TO_GAME, known_threads, existing_fingerprints)
    append_new(records, seen=existing_fingerprints)


if __name__ == "__main__":
    main()


""" run this :  rm engagement_data_steam.csv
python3 steam_scraper.py


python3 -m streamlit run app.py"""