#!/usr/bin/env python3

import argparse
import json
import os
import re
import time
from datetime import datetime
from difflib import SequenceMatcher

import requests

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
BASE_MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b-instruct")


# ── Inference ─────────────────────────────────────────────────────────────────

def call_ollama(model: str, system: str, user: str) -> tuple[str, int]:
    """Returns (response_text, latency_ms)."""
    t0 = time.time()
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":    model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "stream":  False,
                "options": {"temperature": 0, "num_predict": 64},
            },
            timeout=30,
        )
        result = resp.json()["message"]["content"].strip()
        ms     = int((time.time() - t0) * 1000)
        return result, ms
    except Exception as e:
        return "ERROR", int((time.time() - t0) * 1000)


# ── Metrics ───────────────────────────────────────────────────────────────────

def exact_match(pred: str, label: str) -> bool:
    return pred.strip().lower() == label.strip().lower()


def street_match(pred: str, label: str) -> bool:
    """True if street number + primary street name both match (fuzzy)."""
    if pred == "NO_LOCATION" or label == "NO_LOCATION":
        return pred == label

    def parse(s: str):
        m = re.match(r"(\d+)\s+(.+?)(?:,|$)", s.strip(), re.I)
        if not m:
            return None, s.lower()
        return m.group(1), m.group(2).lower().strip()

    pnum, pstreet = parse(pred)
    lnum, lstreet = parse(label)

    # Numbers must match exactly if both present
    if pnum and lnum and pnum != lnum:
        return False

    # Street name fuzzy match
    return SequenceMatcher(None, pstreet, lstreet).ratio() > 0.80


def city_match(pred: str, label: str) -> bool:
    """True if city component matches (last part before state)."""
    def extract_city(s: str) -> str:
        parts = [p.strip() for p in s.split(",")]
        # Format: "address, City, MI" — city is second-to-last
        if len(parts) >= 2:
            return parts[-2].lower().strip()
        return ""

    return extract_city(pred) == extract_city(label)


def no_location_metrics(preds: list, labels: list) -> dict:
    tp = sum(1 for p, l in zip(preds, labels) if p == "NO_LOCATION" and l == "NO_LOCATION")
    fp = sum(1 for p, l in zip(preds, labels) if p == "NO_LOCATION" and l != "NO_LOCATION")
    fn = sum(1 for p, l in zip(preds, labels) if p != "NO_LOCATION" and l == "NO_LOCATION")
    tn = sum(1 for p, l in zip(preds, labels) if p != "NO_LOCATION" and l != "NO_LOCATION")

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
    }


def hallucination_rate(preds: list, labels: list) -> float:
    """Fraction of chunks where label=NO_LOCATION but model predicted an address."""
    nl_labels = [l for l in labels if l == "NO_LOCATION"]
    if not nl_labels:
        return 0.0
    hallucinated = sum(
        1 for p, l in zip(preds, labels)
        if l == "NO_LOCATION" and p != "NO_LOCATION"
    )
    return hallucinated / len(nl_labels)


def worst_failures(preds: list, labels: list, examples: list, n: int = 10) -> list:
    """Return the n worst failures — predicted address when label was NO_LOCATION."""
    failures = [
        {
            "transcript": ex["text"].split("<|im_start|>user\n")[1].split("<|im_end|>")[0].replace("Transmission: ", ""),
            "pred":       p,
            "label":      l,
            "feed_id":    ex.get("feed_id", ""),
        }
        for ex, p, l in zip(examples, preds, labels)
        if l == "NO_LOCATION" and p != "NO_LOCATION"
    ]
    return failures[:n]


# ── Main evaluation loop ──────────────────────────────────────────────────────

def evaluate(
    model_name: str,
    examples: list,
    label: str = "base",
    run_id: str = "",
) -> dict:
    SYSTEM_PROMPT = """You are a Detroit metro area dispatch address normalizer.
Convert dispatch shorthand into full, geocodable location strings.
If no address is detectable, return exactly: NO_LOCATION
Return ONLY the address string. No explanation."""

    preds   = []
    latencies = []
    n = len(examples)

    print(f"\nEvaluating [{label}] on {n} examples...")

    for i, ex in enumerate(examples):
        # Extract transcript from the formatted chat text
        try:
            transcript = (
                ex["text"]
                .split("<|im_start|>user\n")[1]
                .split("<|im_end|>")[0]
                .replace("Transmission: ", "")
                .strip()
            )
        except Exception:
            transcript = ex.get("label", "")

        pred, ms = call_ollama(model_name, SYSTEM_PROMPT, f"Transmission: {transcript}")
        preds.append(pred)
        latencies.append(ms)

        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{n}  avg {sum(latencies)//len(latencies)}ms/call")

    labels_list = [ex["label"] for ex in examples]

    em   = sum(exact_match(p, l)  for p, l in zip(preds, labels_list)) / n
    sm   = sum(street_match(p, l) for p, l in zip(preds, labels_list)) / n
    cm   = sum(city_match(p, l)   for p, l in zip(preds, labels_list)) / n
    nl   = no_location_metrics(preds, labels_list)
    hall = hallucination_rate(preds, labels_list)
    avg_ms = sum(latencies) // len(latencies)

    no_loc_pct = sum(1 for l in labels_list if l == "NO_LOCATION") / n

    result = {
        "run_id":              run_id or datetime.utcnow().strftime("%Y%m%d-%H%M%S"),
        "model":               label,
        "model_name":          model_name,
        "evaluated_at":        datetime.utcnow().isoformat(),
        "n":                   n,
        "exact_match":         round(em, 4),
        "street_match":        round(sm, 4),
        "city_match":          round(cm, 4),
        "no_location_f1":      round(nl["f1"], 4),
        "no_location_precision": round(nl["precision"], 4),
        "no_location_recall":  round(nl["recall"], 4),
        "hallucination_rate":  round(hall, 4),
        "avg_latency_ms":      avg_ms,
        "eval_no_loc_pct":     round(no_loc_pct, 4),
        "no_location_detail":  nl,
        "worst_failures":      worst_failures(preds, labels_list, examples),
    }

    # Print summary
    print(f"\n  ── Results: {label} ──")
    print(f"  Exact match:          {em:.1%}")
    print(f"  Street name match:    {sm:.1%}")
    print(f"  City match:           {cm:.1%}")
    print(f"  NO_LOCATION F1:       {nl['f1']:.1%}  (P={nl['precision']:.1%}  R={nl['recall']:.1%})")
    print(f"  Hallucination rate:   {hall:.1%}")
    print(f"  Avg latency:          {avg_ms}ms")

    return result


def print_comparison(baseline: dict, lora: dict):
    print(f"\n{'='*60}")
    print(f"  COMPARISON: {baseline['model']} vs {lora['model']}")
    print(f"{'='*60}")

    metrics = [
        ("Exact match",       "exact_match",       True,  0.05),
        ("Street match",      "street_match",       True,  0.03),
        ("NO_LOCATION F1",    "no_location_f1",     True,  0.00),
        ("Hallucination rate","hallucination_rate",  False, 0.00),
        ("Avg latency (ms)",  "avg_latency_ms",      False, None),
    ]

    all_gates_pass = True
    for label, key, higher_is_better, min_improvement in metrics:
        b_val = baseline[key]
        l_val = lora[key]
        delta = l_val - b_val
        pct   = f"{delta:+.1%}" if isinstance(b_val, float) else f"{delta:+d}"

        if higher_is_better:
            better = delta > 0
            gate   = delta >= min_improvement if min_improvement is not None else True
        else:
            better = delta < 0
            gate   = delta <= 0 if key == "hallucination_rate" else True

        symbol = "✓" if gate else "✗"
        arrow  = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        color  = "\033[92m" if (better and gate) else ("\033[91m" if not gate else "\033[93m")
        reset  = "\033[0m"

        if not gate:
            all_gates_pass = False

        if isinstance(b_val, float):
            print(f"  [{symbol}] {label:<25} {b_val:.1%} → {l_val:.1%}  {color}{arrow}{pct}{reset}")
        else:
            print(f"  [{symbol}] {label:<25} {b_val} → {l_val}  {color}{arrow}{pct}{reset}")

    print(f"\n  Overall gate: {'✓ PASS — deploy LoRA' if all_gates_pass else '✗ FAIL — do not deploy'}")
    print(f"{'='*60}\n")

    return all_gates_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lora-model",  default=None,
                        help="Ollama model name for LoRA (e.g. detroit-normalize-v1)")
    parser.add_argument("--base-model",  default=BASE_MODEL)
    parser.add_argument("--eval-file",   default=os.path.join(DATA_DIR, "normalize_eval.jsonl"))
    parser.add_argument("--run-id",      default="")
    args = parser.parse_args()

    if not os.path.exists(args.eval_file):
        print(f"Eval file not found: {args.eval_file}")
        print("Run lora/scripts/export_dataset.py first.")
        return

    with open(args.eval_file) as f:
        examples = [json.loads(l) for l in f]
    print(f"Loaded {len(examples)} eval examples from {args.eval_file}")

    results_path = os.path.join(DATA_DIR, "eval_results.jsonl")
    results      = []

    # Always run base model
    base_result = evaluate(
        args.base_model, examples, label="base", run_id=args.run_id
    )
    results.append(base_result)

    # Optionally run LoRA
    lora_result = None
    if args.lora_model:
        lora_result = evaluate(
            args.lora_model, examples,
            label=f"lora:{args.lora_model}",
            run_id=args.run_id,
        )
        results.append(lora_result)
        print_comparison(base_result, lora_result)

    # Append to results log
    with open(results_path, "a") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"Results appended to {results_path}")

    if lora_result:
        passed = print_comparison(base_result, lora_result)
        if passed:
            print("Next step: enable shadow mode in .env")
            print("  LORA_SHADOW_MODE=true")
            print(f"  LORA_NORMALIZE_MODEL={args.lora_model}")
        else:
            print("Next step: collect more human labels and retrain")


if __name__ == "__main__":
    main()