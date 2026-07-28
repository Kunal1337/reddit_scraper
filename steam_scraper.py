"""
steam_scraper.py — Steam Community Discussions scraper.

Pulls discussion threads (the original post = "post") and their replies
(= "reply") from one or more game hubs, and appends to a single deduplicated
CSV in the shared schema (so Reddit data can later flow into the same file).

There is no official API for Steam discussions, so this scrapes HTML with
requests + BeautifulSoup. That makes the CSS SELECTORS the fragile part:
if a run comes back with 0 records or empty text, use the --inspect helper
to check a live page and fix the SELECTORS block.

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
from datetime import datetime, timezone

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

FORUM_PAGES_PER_APP = 2     # how many pages of the thread list to walk
THREADS_PER_APP = 15        # cap total threads scraped per game
REPLIES_PER_THREAD = 40     # cap replies pulled per thread
REQUEST_DELAY = 2.0         # seconds between requests — be polite, avoid throttling
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
}

# The shared schema — identical to the Reddit scraper's columns.
FIELDNAMES = [
    "source", "author_id", "action_type", "game",
    "text", "timestamp", "score", "sentiment", "topic", "permalink",
]


# --- HTTP helper ------------------------------------------------------------

def get(session, url):
    """Fetch a URL politely, with a delay and mature-content cookie set."""
    time.sleep(REQUEST_DELAY)
    # This cookie skips the age gate that some mature games' forums show.
    resp = session.get(url, headers=HEADERS,
                       cookies={"wants_mature_content": "1", "birthtime": "0"},
                       timeout=20)
    resp.raise_for_status()
    return resp.text


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


def make_record(author_id, action_type, game, text, timestamp, permalink):
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
    }


# --- Dedup helpers ----------------------------------------------------------

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


def scrape_thread(session, url, game):
    """Scrape one thread: the original post + its replies."""
    records = []
    try:
        html = get(session, url)
    except Exception as e:
        print(f"  !! could not load thread {url}: {e}")
        return records

    soup = BeautifulSoup(html, "html.parser")

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
        ))

    # --- replies (action_type = "reply") ---
    for block in soup.select(SELECTORS["reply_block"])[:REPLIES_PER_THREAD]:
        text_el = block.select_one(SELECTORS["reply_text"])
        text = clean(text_el.get_text()) if text_el else ""
        if not text:
            continue  # skip empty/deleted
        records.append(make_record(
            author_id=extract_author(block),
            action_type="reply",
            game=game,
            text=text,
            timestamp=read_timestamp(block),
            permalink=url,
        ))

    return records


def scrape_app(session, appid, game):
    print(f"Scraping Steam discussions for {game} (app {appid})...")
    thread_urls = get_thread_urls(session, appid, FORUM_PAGES_PER_APP)
    print(f"  found {len(thread_urls)} threads; scraping up to {THREADS_PER_APP}")
    records = []
    for url in thread_urls[:THREADS_PER_APP]:
        records.extend(scrape_thread(session, url, game))
    print(f"  -> {len(records)} records")
    return records


def scrape_all(appid_to_game):
    session = requests.Session()
    all_records = []
    for appid, game in appid_to_game.items():
        try:
            all_records.extend(scrape_app(session, appid, game))
        except Exception as e:
            print(f"  !! failed on app {appid}: {e}")
    return all_records


# --- CSV output: append only new records ------------------------------------

def append_new(records, path=OUTPUT_CSV):
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


# --- Entry point ------------------------------------------------------------

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--inspect":
        inspect_page(sys.argv[2])
        return
    records = scrape_all(APPID_TO_GAME)
    append_new(records)


if __name__ == "__main__":
    main()


""" run this :  rm engagement_data_steam.csv
python3 steam_scraper.py


python3 -m streamlit run app.py"""