#!/usr/bin/env python3
"""Build config/fewshots.local.jsonl from the TRAIN split (never the eval split).

    uv run python scripts/build_fewshots.py --train /path/to/normalize_train.jsonl \
        [--eval /path/to/normalize_eval.jsonl]

Uses the same 15 hand-picked TRAIN rows as eval Config C (8 with a location, 7 without),
converts their gold strings to structured locations, and refuses to write if any
transcript also appears in the eval split. The output is gitignored: these are real
dispatch transcripts and stay on this machine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.settings import get_settings  # noqa: E402
from extract.legacy import parse_legacy  # noqa: E402

FEWSHOT_TRAIN_IDX = [322, 2912, 1138, 2975, 3195, 624, 3698, 494,   # location
                     955, 787, 763, 3128, 1618, 1543, 1427]         # NO_LOCATION


def transcript_of(text: str) -> str:
    return text.split("<|im_start|>user\n")[1].split("<|im_end|>")[0].replace("Transmission: ", "").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--eval", default=None)
    ap.add_argument("--out", default=str(get_settings().llm_fewshot_file))
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.train)]
    eval_tx = set()
    if a.eval:
        eval_tx = {transcript_of(json.loads(line)["text"]) for line in open(a.eval)}
    out = []
    for i in FEWSHOT_TRAIN_IDX:
        t = transcript_of(rows[i]["text"])
        if t in eval_tx:
            sys.exit(f"train[{i}] also appears in the eval split; refusing")
        loc = parse_legacy(rows[i]["label"])
        d = asdict(loc)
        if loc.kind == "none":
            d = {k: None for k in d} | {"kind": "none"}
        out.append({"train_idx": i, "transcript": t, "location": d})
    with open(a.out, "w") as f:
        for d in out:
            f.write(json.dumps(d) + "\n")
    os.chmod(a.out, 0o600)
    print(f"wrote {len(out)} few-shots to {a.out}")


if __name__ == "__main__":
    main()
