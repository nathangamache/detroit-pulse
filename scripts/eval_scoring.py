"""Location-normalization scorer, copied VERBATIM from lora/eval_baseline.py
(exact_match 46-47, street_match 50-69, city_match 72-81, no_location_metrics 84-96,
hallucination_rate 99-108), so new results are comparable with the LoRA/Qwen baselines and
the 2026-11-14 Claude eval. Only `aggregate` below is new (mirrors evaluate() 165-190).
"""
import re
from difflib import SequenceMatcher


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


def aggregate(preds: list, labels_list: list) -> dict:
    n = len(preds)
    nl = no_location_metrics(preds, labels_list)
    return {
        "n": n,
        "exact_match": round(sum(exact_match(p, l) for p, l in zip(preds, labels_list)) / n, 4),
        "street_match": round(sum(street_match(p, l) for p, l in zip(preds, labels_list)) / n, 4),
        "city_match": round(sum(city_match(p, l) for p, l in zip(preds, labels_list)) / n, 4),
        "no_location_precision": round(nl["precision"], 4),
        "no_location_recall": round(nl["recall"], 4),
        "no_location_f1": round(nl["f1"], 4),
        "hallucination_rate": round(hallucination_rate(preds, labels_list), 4),
    }
