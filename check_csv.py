"""
dedupe_csv.py — ONE-TIME cleanup for exact duplicate rows.

Why this is needed: the regular 6-hourly scraper and a manual backfill run
can end up writing overlapping data if they run concurrently — each one
dedupes against whatever the CSV looked like *when it started*, so neither
knows about the other's in-flight writes. A git merge that keeps "both
changes" on a conflict can then leave a handful of exact duplicate rows
sitting in the file (typically only for threads that were newly active
during the overlap window, not the whole dataset).

This reuses the exact same fingerprint (permalink | author_id | timestamp)
used everywhere else in the pipeline, so "duplicate" here means the same
thing it means throughout the rest of the project — not a heuristic guess.

Run:
    python3 dedupe_csv.py
"""

import csv
import shutil

import steam_scraper as base

INPUT_PATH = base.OUTPUT_CSV
BACKUP_PATH = base.OUTPUT_CSV + ".before_dedupe.bak"


def main():
    # Always keep a backup before rewriting anything.
    shutil.copyfile(INPUT_PATH, BACKUP_PATH)
    print(f"Backed up {INPUT_PATH} -> {BACKUP_PATH}")

    seen = set()
    kept_rows = []
    total = 0
    duplicates = 0

    with open(INPUT_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            total += 1
            fp = base.fingerprint(row)
            if fp in seen:
                duplicates += 1
                continue
            seen.add(fp)
            kept_rows.append(row)

    with open(INPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)

    print(f"\n{total} rows read, {duplicates} exact duplicates removed, "
          f"{len(kept_rows)} rows kept -> {INPUT_PATH}")
    print(f"If anything looks wrong, your original file is safe at {BACKUP_PATH}")


if __name__ == "__main__":
    main()