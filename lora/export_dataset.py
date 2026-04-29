#!/usr/bin/env python3

import json
import os
import random
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

engine   = create_engine(os.getenv("DATABASE_URL"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

NORMALIZE_SYSTEM = """You are a Detroit metro area dispatch address normalizer.
Convert dispatch shorthand into full, geocodable location strings.

Metro Detroit geography reference:
- Mile roads run east-west: 6 Mile, 7 Mile, 8 Mile (state line),
  9 Mile, 10 Mile, 11 Mile, 12 Mile, 13 Mile, 14 Mile, 15 Mile
- Major north-south corridors: Woodward Avenue, Gratiot Avenue, Mound Road,
  Van Dyke Avenue, Dequindre Road, John R Street, Schoenherr Road,
  Beeline Highway, Livernois Avenue, Outer Drive, Harper Avenue
- Major diagonal / east-west roads: Northwestern Highway, Telegraph Road,
  Ford Road, Michigan Avenue, Ecorse Road, Cherry Hill Road,
  Plymouth Road, Tireman Avenue, Vernor Highway, Ann Arbor Road

CRITICAL - NUMBER DISAMBIGUATION:
A number is part of an address ONLY when it is immediately preceded or
followed by a street name or clear address context.
Numbers describing rounds, floors, unit numbers, times, or people counts
are NEVER part of an address.

If no address is detectable, return exactly: NO_LOCATION
Return ONLY the address string. No explanation."""

NORMALIZE_USER = "Transmission: {transcript}"


def format_chat(transcript: str, label: str) -> str:
    """Format as Qwen2.5-Instruct chat template for SFT training."""
    return (
        "<|im_start|>system\n"
        f"{NORMALIZE_SYSTEM}\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"{NORMALIZE_USER.format(transcript=transcript)}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
        f"{label}\n"
        "<|im_end|>"
    )


def export_normalize():
    print("\n── Normalization dataset export ──\n")
    examples = []

    with engine.connect() as conn:

        # ── Source 1: Human-reviewed labels (highest priority) ────────────────
        human = conn.execute(text("""
            SELECT raw_transcript, review_label AS label, feed_id,
                   'human' AS source
            FROM transcript_chunks
            WHERE reviewed IS NOT NULL
              AND review_label IS NOT NULL
              AND review_source = 'human'
              AND length(raw_transcript) > 20
        """)).fetchall()
        print(f"Human-reviewed:      {len(human)}")

        for r in human:
            examples.append({
                "text":     format_chat(r.raw_transcript, r.label),
                "label":    r.label,
                "feed_id":  r.feed_id,
                "source":   "human",
            })

        # ── Source 1b: Claude-reviewed labels ────────────────────────────────
        claude_reviewed = conn.execute(text("""
            SELECT raw_transcript, review_label AS label, feed_id,
                   'claude' AS source
            FROM transcript_chunks
            WHERE reviewed IS NOT NULL
              AND review_label IS NOT NULL
              AND review_source = 'claude'
              AND length(raw_transcript) > 20
        """)).fetchall()
        print(f"Claude-reviewed:     {len(claude_reviewed)}")

        for r in claude_reviewed:
            examples.append({
                "text":    format_chat(r.raw_transcript, r.label),
                "label":   r.label,
                "feed_id": r.feed_id,
                "source":  "claude",
            })

        # ── Source 2: Auto-labeled HIGH confidence ────────────
        # Excluded for v4 since the model already handles these well, and we want to focus on edge cases.
        auto_pos_count = conn.execute(text(
            "SELECT COUNT(*) FROM transcript_chunks"
            " WHERE geocode_confidence = 'HIGH'"
            " AND geocode_source IN ('photon', 'google')"
            " AND normalized_address IS NOT NULL"
            " AND normalized_address != 'NO_LOCATION'"
            " AND reviewed IS NULL"
            " AND length(raw_transcript) > 20"
        )).scalar()
        print(f"Auto HIGH (excluded):{auto_pos_count} — skipped, model already handles these")

        # ── Source 3: Auto-labeled NO_LOCATION (address correctly absent) ────
        auto_neg = conn.execute(text("""
            SELECT tc.raw_transcript, 'NO_LOCATION' AS label,
                   tc.feed_id, 'auto_negative' AS source
            FROM transcript_chunks tc
            WHERE tc.normalized_address = 'NO_LOCATION'
              AND tc.geocode_confidence = 'FAILED'
              AND tc.reviewed IS NULL
              AND length(tc.raw_transcript) > 20
              AND length(tc.raw_transcript) < 600
              -- Only use if the chunk has incident content (not pure noise)
              AND tc.correlation_action IN ('NEW', 'UPDATE')
        """)).fetchall()
        print(f"Auto NO_LOCATION:     {len(auto_neg)}")

        for r in auto_neg:
            examples.append({
                "text":    format_chat(r.raw_transcript, r.label),
                "label":   r.label,
                "feed_id": r.feed_id,
                "source":  "auto_negative",
            })

    # ── Deduplication ─────────────────────────────────────────────────────────
    seen = set()
    deduped = []
    for ex in examples:
        key = ex["text"]
        if key not in seen:
            seen.add(key)
            deduped.append(ex)
    print(f"\nAfter dedup:         {len(deduped)}")

    # ── Hard negatives — explicit hallucination correction examples ───────────
    # These directly target the most common failure mode: numbers in transcripts
    # that are NOT addresses getting extracted as mile roads or house numbers.
    # Injected directly into training data, bypassing the labeling pipeline.
    HARD_NEGATIVES = [
        # Round/shot counts misidentified as street numbers
        ("Engine 30 responding, 6 rounds fired at the intersection", "NO_LOCATION"),
        ("Units on scene, shots fired, approximately 8 rounds", "NO_LOCATION"),
        ("12 rounds fired, no address given, units responding", "NO_LOCATION"),
        # Unit numbers misidentified as addresses
        ("Car 42, go to channel 3 for traffic", "NO_LOCATION"),
        ("Unit 8, unit 8, you're needed at the station", "NO_LOCATION"),
        ("Ladder 10, ladder 10, return to quarters", "NO_LOCATION"),
        ("Medic 5, medic 5, you're clear", "NO_LOCATION"),
        ("Engine 59, engine 59, in service", "NO_LOCATION"),
        # Floor/level numbers
        ("Patient located on the 4th floor of the building", "NO_LOCATION"),
        ("Fire on the 20th floor, multiple alarms", "NO_LOCATION"),
        # Signal codes / radio chatter
        ("10-4, copy that, unit 42 responding", "NO_LOCATION"),
        ("10-14, 10-14, signal 4, go to tac 6", "NO_LOCATION"),
        ("Still alarm, still alarm, second alarm box", "NO_LOCATION"),
        ("Copy that, you're on the board", "NO_LOCATION"),
        # Person counts
        ("3 males running from the scene, no address given", "NO_LOCATION"),
        ("Two victims, EMS en route, location unknown", "NO_LOCATION"),
        # Correct examples showing disambiguation (address IS present)
        ("Engine 30 respond to 14303 East Warren structure fire",
         "14303 East Warren Avenue, Detroit, MI"),
        ("Medic 5 to 9 Mile and Woodward, unconscious female",
         "9 Mile Road and Woodward Avenue, Royal Oak, MI"),
        ("Units to 23680 Fordson Drive, Dearborn, medical emergency",
         "23680 Fordson Drive, Dearborn, MI"),
        ("Still alarm at West 8 Mile and Southfield, vehicle fire, engine 59",
         "8 Mile Road and Southfield Road, Detroit, MI"),
        ("Rescue 2 to 3501 Oakwood, Dearborn, medical",
         "3501 Oakwood Boulevard, Dearborn, MI"),
    ]

    hard_neg_count = 0
    for transcript, label in HARD_NEGATIVES:
        text_str = format_chat(transcript, label)
        if text_str not in seen:
            seen.add(text_str)
            deduped.append({
                "text":    text_str,
                "label":   label,
                "feed_id": "hard_negative",
                "source":  "hard_negative",
            })
            hard_neg_count += 1
    print(f"Hard negatives added:{hard_neg_count}")

    # ── NO_LOCATION oversampling ──────────────────────────────────────────────
    # If NO_LOCATION is under 35%, oversample from existing NO_LOCATION examples
    # to bring it up. The hallucination problem (22.9% in v2) comes from the
    # model seeing too few NO_LOCATION examples relative to address examples.
    no_loc_count = sum(1 for e in deduped if e["label"] == "NO_LOCATION")
    no_loc_pct   = no_loc_count / len(deduped) if deduped else 0
    print(f"NO_LOCATION %:       {no_loc_pct:.1%}  (target: 30-40%)")

    TARGET_NO_LOC_PCT = 0.35
    if no_loc_pct < TARGET_NO_LOC_PCT:
        no_loc_examples = [e for e in deduped if e["label"] == "NO_LOCATION"]
        addr_examples   = [e for e in deduped if e["label"] != "NO_LOCATION"]
        # How many NO_LOCATION examples do we need to reach target?
        target_no_loc = int(len(addr_examples) * TARGET_NO_LOC_PCT / (1 - TARGET_NO_LOC_PCT))
        if target_no_loc > len(no_loc_examples):
            # Oversample with replacement
            import random as _random
            extra = _random.choices(no_loc_examples,
                                    k=target_no_loc - len(no_loc_examples))
            deduped = addr_examples + no_loc_examples + extra
            new_pct = target_no_loc / len(deduped)
            print(f"  Oversampled NO_LOCATION from {no_loc_count} to {target_no_loc} "
                  f"({new_pct:.1%} of {len(deduped)} total)")
        else:
            print(f"  Sufficient NO_LOCATION examples already.")
    elif no_loc_pct > 0.60:
        print("WARNING: NO_LOCATION overrepresented (>60%) — capping at 40%")
        pos_examples = [e for e in deduped if e["label"] != "NO_LOCATION"]
        neg_examples = [e for e in deduped if e["label"] == "NO_LOCATION"]
        # Cap = 40% of final total, meaning NO_LOC = 0.4 * (pos + NO_LOC)
        # Solving: target_neg = 0.4 * (len(pos) + target_neg)
        # target_neg = (0.4 * len(pos)) / 0.6
        target_neg = int(len(pos_examples) * 0.40 / 0.60)
        random.shuffle(neg_examples)
        deduped = pos_examples + neg_examples[:target_neg]
        actual_pct = target_neg / len(deduped)
        print(f"  Capped to {len(deduped)} examples ({target_neg} NO_LOC = {actual_pct:.1%})")

    # Recompute after oversampling
    no_loc_count = sum(1 for e in deduped if e["label"] == "NO_LOCATION")
    no_loc_pct   = no_loc_count / len(deduped) if deduped else 0


    # ── Per-feed coverage ─────────────────────────────────────────────────────
    feed_counts: dict = {}
    for ex in deduped:
        feed_counts[ex["feed_id"]] = feed_counts.get(ex["feed_id"], 0) + 1
    print("\nPer-feed coverage:")
    for feed, count in sorted(feed_counts.items(), key=lambda x: -x[1]):
        flag = " ⚠ LOW" if count < 50 else ""
        print(f"  {feed:<45} {count:>4}{flag}")

    # ── Quality gate ─────────────────────────────────────────────────────────
    human_count  = sum(1 for e in deduped if e["source"] == "human")
    claude_count = sum(1 for e in deduped if e["source"] == "claude")
    reviewed_count = human_count + claude_count
    print(f"\nQuality gates:")
    print(f"  Reviewed breakdown: {human_count} human + {claude_count} claude = {reviewed_count} total")
    gates = [
        (len(deduped) >= 1000,    f"Total examples ≥ 1000           {len(deduped)}"),
        (reviewed_count >= 300,   f"Reviewed (human+claude) ≥ 300   {reviewed_count}"),
        (no_loc_pct >= 0.15,      f"NO_LOCATION ≥ 15%               {no_loc_pct:.1%}"),
    ]
    all_pass = True
    for ok, msg in gates:
        status = "✓" if ok else "✗"
        print(f"  [{status}] {msg}")
        if not ok:
            all_pass = False

    if not all_pass:
        print("\n  Some gates failed. You can still export for inspection,")
        print("  but don't train until all gates pass.\n")

    # ── Train / eval split ────────────────────────────────────────────────────
    # Stratify by source so eval has same distribution as train
    random.seed(42)
    random.shuffle(deduped)

    # Hold out 10% for eval, ensuring human examples are in eval
    eval_size = max(50, int(len(deduped) * 0.10))
    eval_set  = deduped[:eval_size]
    train_set = deduped[eval_size:]

    # Write files
    train_path = os.path.join(DATA_DIR, "normalize_train.jsonl")
    eval_path  = os.path.join(DATA_DIR, "normalize_eval.jsonl")

    with open(train_path, "w") as f:
        for ex in train_set:
            f.write(json.dumps(ex) + "\n")

    with open(eval_path, "w") as f:
        for ex in eval_set:
            f.write(json.dumps(ex) + "\n")

    # Stats
    stats = {
        "exported_at":    datetime.now(timezone.utc).isoformat(),
        "total":          len(deduped),
        "train":          len(train_set),
        "eval":           len(eval_set),
        "human":          human_count,
        "claude":         claude_count,
        "reviewed":       reviewed_count,
        "auto_high":      0,  # excluded from training
        "auto_negative":  sum(1 for e in deduped if e["source"] == "auto_negative"),
        "no_location_pct":round(no_loc_pct, 4),
        "feed_counts":    feed_counts,
        "all_gates_pass": all_pass,
    }
    stats_path = os.path.join(DATA_DIR, "export_stats.json")
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\n── Output ──")
    print(f"  {train_path}  ({len(train_set)} examples)")
    print(f"  {eval_path}   ({len(eval_set)} examples)")
    print(f"  {stats_path}")
    print(f"\nReady to train: {'YES' if all_pass else 'NOT YET — see gates above'}")


if __name__ == "__main__":
    export_normalize()