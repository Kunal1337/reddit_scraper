"""
migrate_add_locked_column.py — ONE-TIME schema migration.

steam_scraper.py now writes a "locked" column (added so the dashboard can
tell genuinely-unanswered threads apart from threads a moderator closed).
The CSV already on disk has an older header without that column — appending
new 11-column rows under a 10-column header would break pandas.read_csv the
moment it hits a mismatched row. This script rewrites the file once with a
unified header, backfilling "locked" = "" (unknown) for every existing row,
since there's no way to know a historical thread's locked status without
re-fetching it.

Run this ONCE, before the next scheduled scrape or backfill run:
    python3 migrate_add_locked_column.py
"""

import csv
import shutil

import steam_scraper as base

INPUT_PATH = base.OUTPUT_CSV
BACKUP_PATH = base.OUTPUT_CSV + ".before_locked_migration.bak"


def main():
    shutil.copyfile(INPUT_PATH, BACKUP_PATH)
    print(f"Backed up {INPUT_PATH} -> {BACKUP_PATH}")

    with open(INPUT_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        old_fieldnames = reader.fieldnames
        rows = list(reader)

    if "locked" in old_fieldnames:
        print("'locked' column already present — nothing to migrate.")
        return

    for row in rows:
        row["locked"] = ""  # unknown — this row predates locked-thread detection

    with open(INPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=base.FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nMigrated {len(rows)} rows to include the 'locked' column (blank = unknown/pre-migration).")
    print(f"If anything looks wrong, your original file is safe at {BACKUP_PATH}")


if __name__ == "__main__":
    main()