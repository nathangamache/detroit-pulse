#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
from datetime import datetime

import anthropic
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

engine        = create_engine(os.getenv("DATABASE_URL"))
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 256

# ── System prompt — highly specific to Detroit dispatch radio ─────────────────
# This is the most important part. The prompt needs to encode the exact
# knowledge a Detroit dispatch expert would apply when reviewing these.

SYSTEM_PROMPT = """You are an expert at interpreting Detroit metro area emergency dispatch radio transmissions and extracting accurate street addresses from them.

Your job is to determine the correct normalized address for a given scanner radio transcript, or determine that no address is present.

## Metro Detroit geography you must know:

**Mile roads** run east-west as numbered corridors:
6 Mile, 7 Mile, 8 Mile (Michigan/Ohio state line), 9 Mile, 10 Mile, 11 Mile, 12 Mile, 13 Mile, 14 Mile, 15 Mile, 16 Mile

**Major north-south roads:** Woodward Ave, Gratiot Ave, Mound Rd, Van Dyke Ave, Dequindre Rd, John R St, Schoenherr Rd, Livernois Ave, Outer Drive, Harper Ave, Telegraph Rd

**Major east-west/diagonal roads:** Northwestern Hwy, Ford Rd, Michigan Ave, Plymouth Rd, Tireman Ave, Vernor Hwy, Ann Arbor Rd, Cherry Hill Rd, Warren Ave, 7 Mile Rd, 8 Mile Rd

**Common shorthand:** "8 Mile" = 8 Mile Road, "Gratiot" = Gratiot Avenue, "Mound" = Mound Road, "Northwestern" = Northwestern Highway, "Telegraph" = Telegraph Road

## THE MOST CRITICAL RULE — Number disambiguation:

Numbers in dispatch audio describe MANY things besides streets. You MUST determine from context whether a number refers to a location or something else.

**Numbers that are NEVER part of an address:**
- Round/shot counts: "6 rounds", "shots fired", "multiple shots"
- Unit/badge/car numbers: "Unit 1344", "Car 42", "Engine 30", "Ladder 10", "Rescue 2", "Medic 5"
- Floor numbers: "20th floor", "floor 4", "third floor"
- Radio channels/frequencies: "channel 3", "tac 6", "go to 4"
- Times: "at 1340", "1600 hours"
- Person counts: "3 males", "two victims", "four occupants"
- Alarm levels: "second alarm", "box alarm 3", "still alarm"
- Signal codes: "10-4", "10-14", "signal 4"

**HALLUCINATION TRAP — The most common error:**
If a transcript says "6 rounds fired at 14303 East Warren" — the "6" is rounds fired, NOT a street.
The correct answer is "14303 East Warren Avenue, Detroit, MI" — NOT "14303 6 Mile Road, Detroit, MI"

Similarly: "three victims at 9 Mile and Woodward" — "three" is victim count, NOT an address number.
Correct: "9 Mile Road and Woodward Avenue, Royal Oak, MI"

## Output format:

Return a JSON object with exactly these fields:
{
  "label": "the normalized address string OR NO_LOCATION",
  "confidence": "HIGH | MEDIUM | LOW",
  "reasoning": "one sentence explaining your decision"
}

**Address format rules:**
- Street address: "14303 East Warren Avenue, Detroit, MI"
- Intersection: "9 Mile Road and Woodward Avenue, Royal Oak, MI"  
- Named place: "Family Dollar, Cherry Hill Road, Westland, MI"
- If no address: "NO_LOCATION"

**When to return NO_LOCATION:**
- Transcript is pure radio chatter with no location (e.g. "copy that, unit 42 responding")
- Only unit numbers mentioned, no location
- Status updates with no new location information
- Transmission is too garbled to extract a reliable address

**When NOT to return NO_LOCATION:**
- A street name is clearly mentioned even without a house number
- An intersection is mentioned
- A known landmark, business, or park is mentioned
- A hospital, school, or other named location is mentioned

Always return valid JSON only. No preamble, no explanation outside the JSON."""


FEED_CONTEXT = {
    "wayneco_detroit_police_fire":     "Detroit city — DPD/Fire combined dispatch. High call volume. Covers all of Detroit.",
    "wayneco_detroit_police_dispatch": "Detroit city — DPD primary dispatch. Addresses typically Detroit neighborhoods.",
    "wayneco_detroit_fire":            "Detroit city — Detroit Fire Department. Structure fires, vehicle fires, EMS.",
    "wayneco_detroit_ems":             "Detroit city — Detroit EMS. Medical calls throughout Detroit.",
    "wayneco_plymouthnorthville":      "Plymouth Township and Northville area — suburban Wayne County.",
    "wayneco_downriver":               "Downriver communities — Wyandotte, Lincoln Park, Ecorse, River Rouge area.",
    "wayneco_dearborn":                "Dearborn and Dearborn Heights — large Arab-American community, dense residential.",
    "oaklandco_dispatch":              "Oakland County — suburban communities north of Detroit: Troy, Royal Oak, Pontiac area.",
    "washtenawco":                     "Washtenaw County — Ann Arbor, Ypsilanti area.",
}


# ── DB queries — same priority queues as label_review.py ─────────────────────

QUEUES = {
    "mile_roads": {
        "name":  "Mile road extractions (hallucination-prone)",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.geocode_source, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.normalized_address ~ '\\d+ Mile'
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
    "medium_confidence": {
        "name":  "MEDIUM confidence geocodes",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.geocode_source, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.geocode_confidence = 'MEDIUM'
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
    "no_location_active": {
        "name":  "NO_LOCATION from high-activity feeds",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.geocode_source, tc.feed_id
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
    "high_confidence": {
        "name":  "HIGH confidence positives (reinforcement)",
        "query": """
            SELECT tc.chunk_id, tc.raw_transcript, tc.normalized_address,
                   tc.geocode_confidence, tc.geocode_source, tc.feed_id
            FROM transcript_chunks tc
            WHERE tc.reviewed IS NULL
              AND tc.geocode_confidence = 'HIGH'
              AND tc.geocode_source IN ('photon', 'google')
              AND tc.normalized_address != 'NO_LOCATION'
              AND tc.normalized_address IS NOT NULL
              AND length(tc.raw_transcript) > 20
            ORDER BY RANDOM()
            LIMIT :limit
        """,
    },
}


# ── Claude labeling ───────────────────────────────────────────────────────────

def build_user_prompt(transcript: str, qwen_output: str,
                      geocode_confidence: str, feed_id: str) -> str:
    feed_ctx = FEED_CONTEXT.get(feed_id, f"Feed: {feed_id}")
    return f"""Feed context: {feed_ctx}
Geocode confidence of Qwen's output: {geocode_confidence}
Qwen's current output: {qwen_output}

Scanner transcript:
{transcript}

What is the correct normalized address for this transmission?"""


def ask_claude(transcript: str, qwen_output: str,
               geocode_confidence: str, feed_id: str) -> dict:
    """
    Ask Claude to label a single transcript.
    Returns dict with: label, confidence, reasoning
    """
    user_prompt = build_user_prompt(
        transcript, qwen_output, geocode_confidence, feed_id
    )

    try:
        response = claude_client.messages.create(
            model      = MODEL,
            max_tokens = MAX_TOKENS,
            system     = SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)

        # Validate required fields
        if "label" not in result:
            return {"label": qwen_output, "confidence": "LOW",
                    "reasoning": "parse_error: no label field"}

        return result

    except json.JSONDecodeError as e:
        return {"label": qwen_output, "confidence": "LOW",
                "reasoning": f"parse_error: {str(e)[:60]}"}
    except anthropic.RateLimitError:
        time.sleep(60)
        return {"label": "SKIP", "confidence": "LOW",
                "reasoning": "rate_limit — skipped"}
    except Exception as e:
        return {"label": "SKIP", "confidence": "LOW",
                "reasoning": f"api_error: {str(e)[:60]}"}


def save_label(conn, chunk_id: str, label: str,
               reasoning: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    conn.execute(text("""
        UPDATE transcript_chunks
        SET reviewed      = NOW(),
            review_label  = :label,
            review_source = 'claude'
        WHERE chunk_id = :cid
    """), {"cid": chunk_id, "label": label})
    conn.commit()


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_stats(conn) -> dict:
    row = conn.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE reviewed IS NOT NULL)                   AS total,
            COUNT(*) FILTER (WHERE review_source = 'claude')               AS by_claude,
            COUNT(*) FILTER (WHERE review_source = 'human')                AS by_human,
            COUNT(*) FILTER (WHERE reviewed >= NOW() - INTERVAL '24 hours') AS today
        FROM transcript_chunks
    """)).fetchone()
    return dict(row._mapping)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    queue_names:  list[str],
    limit:        int,
    dry_run:      bool,
    verbose:      bool,
) -> None:
    stats_before = None

    with engine.connect() as conn:
        stats_before = get_stats(conn)

        print(f"\n{'='*62}")
        print(f"  Detroit Pulse — Automated LoRA Labeler (Claude {MODEL})")
        print(f"  Mode: {'DRY RUN — no DB writes' if dry_run else 'LIVE'}")
        print(f"  Queues: {', '.join(queue_names)}")
        print(f"  Limit per queue: {limit}")
        print(f"  Total reviewed so far: {stats_before['total']} "
              f"(claude={stats_before['by_claude']}, "
              f"human={stats_before['by_human']})")
        print(f"{'='*62}\n")

        total_labeled = 0
        total_kept    = 0
        total_changed = 0
        total_no_loc  = 0
        total_skipped = 0

        for qname in queue_names:
            queue = QUEUES[qname]
            rows  = conn.execute(
                text(queue["query"]), {"limit": limit}
            ).fetchall()

            if not rows:
                print(f"  [{qname}] No unreviewed examples found.\n")
                continue

            print(f"── {queue['name']} ({len(rows)} examples) ──")

            for i, row in enumerate(rows):
                result = ask_claude(
                    transcript         = row.raw_transcript,
                    qwen_output        = row.normalized_address,
                    geocode_confidence = row.geocode_confidence,
                    feed_id            = row.feed_id,
                )

                label     = result.get("label", "").strip()
                reasoning = result.get("reasoning", "")
                conf      = result.get("confidence", "")

                if label == "SKIP":
                    total_skipped += 1
                    continue

                changed = label != row.normalized_address
                if changed:
                    total_changed += 1
                else:
                    total_kept += 1
                if label == "NO_LOCATION":
                    total_no_loc += 1

                save_label(conn, str(row.chunk_id), label, reasoning, dry_run)
                total_labeled += 1

                if verbose or changed:
                    action = "CHANGED" if changed else "kept"
                    print(f"  [{i+1:3d}/{len(rows)}] {action}")
                    print(f"    Transcript: {row.raw_transcript[:70]}")
                    if changed:
                        print(f"    Qwen:   {row.normalized_address}")
                        print(f"    Claude: {label}  [{conf}]")
                        print(f"    Why:    {reasoning}")
                    else:
                        print(f"    Label:  {label}  [{conf}]")
                elif (i + 1) % 25 == 0:
                    print(f"  {i+1}/{len(rows)} processed "
                          f"({total_changed} changed, {total_kept} kept)...")

                # Small delay to avoid hammering the API
                time.sleep(0.3)

            print()

        stats_after = get_stats(conn)

        print(f"\n{'='*62}")
        print(f"  Session complete {'(DRY RUN)' if dry_run else ''}")
        print(f"  Labeled this run:  {total_labeled}")
        print(f"    Kept Qwen output: {total_kept}")
        print(f"    Changed by Claude: {total_changed}")
        print(f"    Marked NO_LOCATION: {total_no_loc}")
        print(f"    Skipped (API err):  {total_skipped}")
        print(f"  Total in DB now:   {stats_after['total']} "
              f"(+{stats_after['total'] - stats_before['total']})")
        print(f"  By Claude total:   {stats_after['by_claude']}")
        print(f"  By human total:    {stats_after['by_human']}")
        remaining = max(0, 500 - stats_after['total'])
        print(f"  To training gate:  {remaining} remaining")
        print(f"{'='*62}\n")

        if not dry_run and total_labeled > 0:
            print("Next step: python lora/export_dataset.py")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-label LoRA training data using Claude"
    )
    parser.add_argument(
        "--queue",
        choices=list(QUEUES.keys()),
        default=None,
        help="Run a single queue only",
    )
    parser.add_argument(
        "--all-queues",
        action="store_true",
        help="Run all queues including HIGH confidence positives",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max examples per queue (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show decisions without writing to DB",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every example, not just changes",
    )
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env or environment")
        sys.exit(1)

    if args.queue:
        queue_names = [args.queue]
    elif args.all_queues:
        queue_names = list(QUEUES.keys())
    else:
        # Default: the three high-value queues (skip HIGH confidence —
        # those are already well-labeled by the geocoder)
        queue_names = ["mile_roads", "medium_confidence", "no_location_active"]

    run(
        queue_names = queue_names,
        limit       = args.limit,
        dry_run     = args.dry_run,
        verbose     = args.verbose,
    )


if __name__ == "__main__":
    main()