#!/usr/bin/env python3

import argparse
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

PREVIEW_SQL = """
    SELECT
        tc.chunk_id,
        tc.raw_transcript,
        tc.review_label,
        tc.geocode_confidence,
        tc.feed_id
    FROM transcript_chunks tc
    WHERE tc.review_source = 'claude'
      AND tc.review_label != 'NO_LOCATION'
      AND tc.geocode_confidence IN ('FAILED', 'LOW')
      AND tc.reviewed IS NOT NULL
    ORDER BY RANDOM()
    LIMIT 10
"""

COUNT_SQL = """
    SELECT COUNT(*)
    FROM transcript_chunks
    WHERE review_source = 'claude'
      AND review_label != 'NO_LOCATION'
      AND geocode_confidence IN ('FAILED', 'LOW')
      AND reviewed IS NOT NULL
"""

DELETE_SQL = """
    UPDATE transcript_chunks
    SET reviewed      = NULL,
        review_label  = NULL,
        review_source = NULL
    WHERE review_source = 'claude'
      AND review_label != 'NO_LOCATION'
      AND geocode_confidence IN ('FAILED', 'LOW')
      AND reviewed IS NOT NULL
"""

STATS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE review_source = 'human')  AS human,
        COUNT(*) FILTER (WHERE review_source = 'claude') AS claude,
        COUNT(*)                                          AS total
    FROM transcript_chunks
    WHERE reviewed IS NOT NULL
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making changes")
    args = parser.parse_args()

    with engine.connect() as conn:
        # Stats before
        stats = conn.execute(text(STATS_SQL)).fetchone()
        print(f"\nCurrent label counts:")
        print(f"  Human:  {stats.human}")
        print(f"  Claude: {stats.claude}")
        print(f"  Total:  {stats.total}")

        # Count bad labels
        bad_count = conn.execute(text(COUNT_SQL)).scalar()
        print(f"\nUnverifiable Claude labels (FAILED/LOW geocode): {bad_count}")

        if bad_count == 0:
            print("Nothing to clean.")
            return

        # Preview sample
        print(f"\nSample of labels to be removed:")
        rows = conn.execute(text(PREVIEW_SQL)).fetchall()
        for i, r in enumerate(rows):
            print(f"\n  [{i+1}] Feed: {r.feed_id}")
            print(f"       Transcript: {r.raw_transcript[:90]}")
            print(f"       Claude said: {r.review_label}")
            print(f"       Geocode:     {r.geocode_confidence}")

        if args.dry_run:
            print(f"\nDRY RUN — no changes made.")
            print(f"Run without --dry-run to remove {bad_count} labels.")
            return

        # Confirm
        print(f"\nThis will clear {bad_count} unverifiable Claude labels.")
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != 'y':
            print("Aborted.")
            return

        conn.execute(text(DELETE_SQL))
        conn.commit()

        # Stats after
        stats = conn.execute(text(STATS_SQL)).fetchone()
        print(f"\nDone. Updated label counts:")
        print(f"  Human:  {stats.human}")
        print(f"  Claude: {stats.claude}")
        print(f"  Total:  {stats.total}")
        print(f"\nNext: python lora/export_dataset.py")


if __name__ == "__main__":
    main()