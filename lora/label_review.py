#!/usr/bin/env python3

import json
import os
import sys
import readline  # enables arrow keys in input()
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

engine = create_engine(os.getenv("DATABASE_URL"))

# ── Priority queues — reviewed in this order ─────────────────────────────────

QUEUES = [
    {
        "name":  "Mile road extractions (hallucination-prone)",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.normalized_address ~ '\\d+ Mile'
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
    {
        "name":  "MEDIUM confidence geocodes (ambiguous calls)",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.geocode_confidence = 'MEDIUM'
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
    {
        "name":  "NO_LOCATION from high-activity feeds",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.normalized_address = 'NO_LOCATION'
              AND tc.feed_id IN (
                  'wayneco_detroit_police_fire',
                  'wayneco_detroit_police_dispatch',
                  'wayneco_detroit_fire',
                  'wayneco_detroit_ems'
              )
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
    {
        "name":  "General HIGH confidence (positive examples)",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.geocode_confidence = 'HIGH'
              AND tc.normalized_address != 'NO_LOCATION'
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
]

FEED_DISPLAY = {
    "wayneco_detroit_police_fire":     "DPD/Fire",
    "wayneco_detroit_police_dispatch": "DPD Dispatch",
    "wayneco_detroit_fire":            "Detroit Fire",
    "wayneco_detroit_ems":             "Detroit EMS",
    "wayneco_plymouthnorthville":      "Plymouth-Northville",
    "wayneco_downriver":               "Downriver",
}


def get_stats(conn):
    total = conn.execute(text(
        "SELECT COUNT(*) FROM transcript_chunks WHERE reviewed IS NOT NULL"
    )).scalar()
    today = conn.execute(text(
        "SELECT COUNT(*) FROM transcript_chunks "
        "WHERE reviewed >= NOW() - INTERVAL '24 hours'"
    )).scalar()
    return total, today


def save_label(conn, chunk_id, label, source="human"):
    conn.execute(text("""
        UPDATE transcript_chunks
        SET reviewed     = NOW(),
            review_label = :label,
            review_source = :source
        WHERE chunk_id = :cid
    """), {"cid": chunk_id, "label": label, "source": source})
    conn.commit()


def run_review(limit_per_queue=30):
    reviewed_count = 0

    with engine.connect() as conn:
        total_done, today_done = get_stats(conn)
        print(f"\n{'='*60}")
        print(f"  Detroit Pulse — LoRA Label Review")
        print(f"  Total reviewed: {total_done}  |  Today: {today_done}")
        print(f"  Target: 500 total before training")
        print(f"{'='*60}\n")

        for queue in QUEUES:
            rows = conn.execute(
                text(queue["query"]), {"limit": limit_per_queue}
            ).fetchall()

            if not rows:
                continue

            print(f"\n── {queue['name']} ({len(rows)} examples) ──\n")
            proceed = input("Review this batch? [Y/n] ").strip().lower()
            if proceed == 'n':
                continue

            for row in rows:
                feed_label = FEED_DISPLAY.get(row.feed_id, row.feed_id)
                conf_color = {
                    'HIGH': '\033[92m', 'MEDIUM': '\033[93m',
                    'LOW': '\033[91m', 'FAILED': '\033[90m',
                }.get(row.geocode_confidence, '')
                reset = '\033[0m'

                print(f"\n{'─'*60}")
                print(f"Feed:       {feed_label}")
                print(f"Geo conf:   {conf_color}{row.geocode_confidence}{reset}")
                print(f"\nTranscript:\n  {row.raw_transcript}\n")
                print(f"Qwen said:  \033[1m{row.normalized_address}\033[0m\n")
                print("  [k] keep as-is    [e] edit    [n] NO_LOCATION    [s] skip    [q] quit")

                while True:
                    choice = input("  > ").strip().lower()

                    if choice == 'q':
                        print(f"\nSession complete. Reviewed {reviewed_count} examples.")
                        return

                    elif choice == 's':
                        break

                    elif choice == 'k':
                        save_label(conn, str(row.chunk_id),
                                   row.normalized_address, "human")
                        reviewed_count += 1
                        print(f"  ✓ Kept: {row.normalized_address}")
                        break

                    elif choice == 'n':
                        save_label(conn, str(row.chunk_id),
                                   "NO_LOCATION", "human")
                        reviewed_count += 1
                        print("  ✓ Marked NO_LOCATION")
                        break

                    elif choice == 'e':
                        corrected = input("  Correct address: ").strip()
                        if corrected:
                            save_label(conn, str(row.chunk_id),
                                       corrected, "human")
                            reviewed_count += 1
                            print(f"  ✓ Saved: {corrected}")
                        break

                    else:
                        print("  Use k/e/n/s/q")

        print(f"\nSession complete. Reviewed {reviewed_count} examples this session.")
        total_done, _ = get_stats(conn)
        remaining = max(0, 500 - total_done)
        print(f"Total reviewed: {total_done}/500  ({remaining} remaining to training threshold)")


if __name__ == "__main__":
    try:
        run_review()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
