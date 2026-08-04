"""
backfill_scraper.py — ONE-TIME historical backfill for Steam discussions.

The regular steam_scraper.py only ever discovers threads while they're still
sitting in Steam's "active" forum list (top ~15 per run). This script's job
is different: walk each game's forum page-by-page to find threads it never
had a chance to see, back to a single STANDARDIZED_START_DATE shared across
every game — so all games end up with a comparable time window instead of
some going back further than others by accident.

Why a standardized date instead of "everything since January": Steam's
browsable forum list has a hard ceiling (~400 most-recently-active topics,
full stop — not a paging limit, an actual wall). For long-running forums
(Apex Legends, The Sims 4), that ceiling can reach back only a matter of
weeks, not months. Picking one shared cutoff date — set to whatever the
SHALLOWEST game can actually reach — keeps every game's coverage equal and
honest, rather than some games getting months of history and others getting
none. Update STANDARDIZED_START_DATE below once you've checked each game's
real ceiling (jump to its last forum-list page and read the oldest date).

IMPORTANT — this is a heuristic, not a guarantee:
  Steam's forum list appears to be sorted by "last activity," not "date
  posted." An old thread that got bumped by a reply today can sit right
  next to a brand new one. This script stops a game's backfill once it's
  seen CONSECUTIVE_OLD_STOP threads in a row older than the cutoff — which
  usually works, but can occasionally miss a rarely-active thread that's
  still technically in range. Given the cutoff is now set near each game's
  real ceiling anyway, this mostly just confirms "we've reached the wall,"
  rather than doing the heavy lifting it woulda for a deeper cutoff.

This is meant to be run ONCE (or occasionally re-run to extend coverage),
by hand, on your own machine — NOT on the 6-hourly GitHub Action. It will
be slower and use more requests than the regular run.

Run:
    python3 backfill_scraper.py
"""

import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

# Reuse everything shared with the regular scraper instead of duplicating it.
import steam_scraper as base

# --- Backfill-specific config -------------------------------------------

# Shared across ALL games — set to the SHALLOWEST game's reachable ceiling,
# since that's what actually caps a fair, comparable window across all of
# them. Checked directly against each game's last forum-list page:
#   The Sims 4............ May 30
#   Madden NFL 27.......... June 5
#   Apex Legends........... June 29
#   EA Sports CFB 27....... July 1
#   Battlefield 6.......... July 7   <- shallowest; this sets the standard
STANDARDIZED_START_DATE = datetime(2026, 7, 7, tzinfo=timezone.utc)

MAX_PAGES_PER_APP = 150          # safety cap on forum-list pages walked per game (natural ceiling is usually much lower)
CONSECUTIVE_OLD_STOP = 20        # stop a game's backfill after this many pre-cutoff threads in a row
MAX_REPLY_PAGES_BACKFILL = 50    # want full history here, not just "catch up" — much higher than the regular scraper


def get_op_timestamp(url, session):
    """Peek at a thread's original-post timestamp without fully scraping it,
    so we can decide whether it's in-scope before spending a full scrape on it."""
    try:
        html = base.get(session, url)
    except Exception as e:
        print(f"    !! could not check {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    op = soup.select_one(".forum_op")
    ts_iso = base.read_timestamp(op) if op else ""
    if not ts_iso:
        return None
    try:
        return datetime.fromisoformat(ts_iso)
    except ValueError:
        return None


def backfill_app(session, appid, game, known_permalinks, existing_fingerprints):
    print(f"\nBackfilling {game} (app {appid})...")
    base_url = f"https://steamcommunity.com/app/{appid}/discussions/0/"

    consecutive_old = 0
    new_records = []
    page = 1

    while page <= MAX_PAGES_PER_APP and consecutive_old < CONSECUTIVE_OLD_STOP:
        page_url = f"{base_url}?fp={page}"
        try:
            html = base.get(session, page_url)
        except Exception as e:
            print(f"  !! could not load forum page {page}: {e}")
            page += 1
            continue

        soup = BeautifulSoup(html, "html.parser")
        links = [a.get("href") for a in soup.select(base.SELECTORS["thread_link"])]
        links = [href for href in links if href]

        if not links:
            print(f"  page {page}: no threads found — forum list exhausted")
            break

        print(f"  page {page}: {len(links)} threads listed")

        for url in links:
            if url in known_permalinks:
                continue  # already have this one from the regular scraper

            op_ts = get_op_timestamp(url, session)
            if op_ts is None:
                continue  # couldn't read a timestamp, skip rather than guess

            if op_ts < STANDARDIZED_START_DATE:
                consecutive_old += 1
                if consecutive_old >= CONSECUTIVE_OLD_STOP:
                    print(f"  hit {CONSECUTIVE_OLD_STOP} threads in a row before {STANDARDIZED_START_DATE.date()} — stopping this game")
                    break
                continue

            consecutive_old = 0  # reset — this one's in scope
            print(f"    + new thread in range: {url}")
            records = base.scrape_thread(session, url, game, existing_fingerprints)
            new_records.extend(records)
            known_permalinks.add(url)

        page += 1

    print(f"  -> {len(new_records)} records collected for {game}")
    return new_records


def main():
    # Temporarily raise the reply-page cap for this run only.
    base.MAX_REPLY_PAGES = MAX_REPLY_PAGES_BACKFILL

    known_threads = base.load_known_threads(base.OUTPUT_CSV)
    known_permalinks = set(known_threads.keys())
    existing_fingerprints = base.load_existing_fingerprints(base.OUTPUT_CSV)

    session = base.requests.Session()
    all_new_records = []
    for appid, game in base.APPID_TO_GAME.items():
        try:
            all_new_records.extend(
                backfill_app(session, appid, game, known_permalinks, existing_fingerprints)
            )
        except base.ThrottledStop as e:
            print(f"  !! stopping the entire backfill early: {e}")
            break
        except Exception as e:
            print(f"  !! failed on app {appid}: {e}")

    base.append_new(all_new_records, seen=existing_fingerprints)


if __name__ == "__main__":
    main()