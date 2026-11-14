#!/usr/bin/env python3
"""Measure the prefilter: how many location-bearing transmissions does it keep for Claude?

    uv run python scripts/measure_prefilter.py \
        --train /path/to/normalize_train.jsonl [--archive-db detroitpulse_archive] [--show-misses 20]

Location-bearing ground truth:
  train      rows whose gold label is not NO_LOCATION
  archive-A  Claude-reviewed rows (review_source='claude') whose review_label is not NO_LOCATION
  archive-B  rows where the old pipeline extracted an address that geocoded HIGH/MEDIUM
Also reports the share of all rows the prefilter skips (drop rate).

The data stays local: this prints aggregate numbers and, with --show-misses, the
missed transcripts to the terminal only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extract.prefilter import classify  # noqa: E402


def transcript_of(text: str) -> str:
    return text.split("<|im_start|>user\n")[1].split("<|im_end|>")[0].replace("Transmission: ", "").strip()


def summarize(name: str, rows: list[tuple[str, bool]], show: int) -> dict:
    loc = [(t, lab) for t, lab in rows if lab]
    kept_loc = 0
    misses = []
    skipped_all = 0
    labels: dict[str, int] = {}
    for t, is_loc in rows:
        r = classify(t)
        labels[r.label] = labels.get(r.label, 0) + 1
        if r.skip_llm:
            skipped_all += 1
            if is_loc:
                misses.append((r.label, t))
        elif is_loc:
            kept_loc += 1
    out = {
        "set": name, "rows": len(rows), "location_rows": len(loc),
        "location_kept": kept_loc,
        "recall": round(kept_loc / len(loc), 4) if loc else None,
        "skipped_rows": skipped_all, "drop_rate": round(skipped_all / len(rows), 4) if rows else None,
        "labels": labels,
    }
    print(json.dumps(out))
    for lab, t in misses[:show]:
        print(f"   MISS [{lab}] {t[:160]}")
    return out


_SENT = re.compile(r"(?<=[.?!])\s+")


def street_token(label: str) -> str | None:
    """Longest alphabetic word of the gold label's street part ('7 Mile Road and Evergreen Road' -> 'evergreen')."""
    head = label.split(",")[0].lower()
    words = [w for w in re.findall(r"[a-z]+", head)
             if w not in {"road", "avenue", "street", "drive", "boulevard", "and", "mile", "east", "west", "north",
                          "south", "highway", "lane", "court", "interstate", "freeway", "westbound", "eastbound",
                          "northbound", "southbound", "place", "parkway"}]
    return max(words, key=len) if words else None


def sentence_split(name: str, rows: list[tuple[str, str]]) -> dict:
    """Simulate one-file-per-transmission input by splitting 25 s chunks into sentences.

    drop rate  = share of sentences skipped
    recall     = share of location-bearing chunks where at least one sentence that contains the
                 gold street token still passes (proxy for per-transmission recall)
    """
    sents = 0
    skipped = 0
    loc_total = 0
    loc_kept = 0
    for t, label in rows:
        parts = [p for p in _SENT.split(t) if p.strip()]
        verdict = [(p, classify(p).skip_llm) for p in parts]
        sents += len(parts)
        skipped += sum(1 for _, sk in verdict if sk)
        tok = street_token(label) if label and label != "NO_LOCATION" else None
        if tok:
            with_tok = [(p, sk) for p, sk in verdict if tok[:5] in p.lower()]
            if with_tok:
                loc_total += 1
                loc_kept += any(not sk for _, sk in with_tok)
    out = {"set": name + " (sentence-split simulation)", "sentences": sents, "skipped_sentences": skipped,
           "drop_rate": round(skipped / sents, 4) if sents else None,
           "location_chunks_checked": loc_total,
           "recall": round(loc_kept / loc_total, 4) if loc_total else None}
    print(json.dumps(out))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--archive-db", default=None)
    ap.add_argument("--show-misses", type=int, default=0)
    a = ap.parse_args()

    rows = []
    labelled = []
    for line in open(a.train):
        r = json.loads(line)
        rows.append((transcript_of(r["text"]), r["label"] != "NO_LOCATION"))
        labelled.append((transcript_of(r["text"]), r["label"]))
    results = [summarize("train", rows, a.show_misses), sentence_split("train", labelled)]

    if a.archive_db:
        from sqlalchemy import create_engine, text
        from core.settings import get_settings
        eng = create_engine(get_settings().db_url_for(a.archive_db))
        with eng.connect() as c:
            data = c.execute(text("""
                SELECT raw_transcript, review_source, review_label, normalized_address, geocode_confidence
                FROM transcript_chunks WHERE raw_transcript IS NOT NULL""")).all()
        a_rows = [(d[0], d[2] not in (None, "NO_LOCATION")) for d in data if d[1] == "claude"]
        b_rows = [(d[0], d[3] not in (None, "NO_LOCATION") and d[4] in ("HIGH", "MEDIUM")) for d in data]
        results.append(summarize("archive-A (claude-reviewed)", a_rows, a.show_misses))
        results.append(summarize("archive-B (old pipeline, geocoded HIGH/MEDIUM)", b_rows, a.show_misses))
        results.append(sentence_split("archive-A", [(d[0], d[2]) for d in data if d[1] == "claude"]))
    print(json.dumps({"summary": results}, indent=1))


if __name__ == "__main__":
    main()
