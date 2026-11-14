"""Turn Whisper's renderings of spoken numbers back into numbers.

Detector voices and dispatchers read numbers several ways, and Whisper writes them
several more:  "123.4" | "1 2 3 point 4" | "1-2-3-point-4" | "one two three point four" |
"one hundred twenty three point four" | "forty-seven".
Digit-by-digit readings concatenate; readings with tens/hundreds add up.
"""
from __future__ import annotations

import re

UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
        "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000}
NUMBER_WORDS = set(UNITS) | set(TEENS) | set(TENS) | set(SCALES)
_POINT = {"point", "decimal", "dot"}

_TOKEN = re.compile(r"[a-z]+|\d+(?:\.\d+)?|\.", re.I)


def _words_value(tokens: list[str]) -> int | None:
    """Value of an integer spoken with number words and/or digit groups."""
    if not tokens:
        return None
    additive = any(t in TEENS or t in TENS or t in SCALES for t in tokens)
    if not additive:
        # digit-by-digit: "two eight five" / "2 8 5" / "28 5"
        out = ""
        for t in tokens:
            if t.isdigit():
                out += t
            elif t in UNITS:
                out += str(UNITS[t])
            else:
                return None
        return int(out) if out else None
    total, cur = 0, 0
    for t in tokens:
        if t.isdigit():
            cur += int(t)
        elif t in UNITS:
            cur += UNITS[t]
        elif t in TEENS:
            cur += TEENS[t]
        elif t in TENS:
            cur += TENS[t]
        elif t == "hundred":
            cur = (cur or 1) * 100
        elif t == "thousand":
            total += (cur or 1) * 1000
            cur = 0
        else:
            return None
    return total + cur


def parse_number(s: str) -> float | None:
    """Parse the leading spoken/written number in `s` (may include a decimal 'point')."""
    s = s.lower().replace("-", " ").replace(",", " ")
    toks = _TOKEN.findall(s)
    if not toks:
        return None
    # direct numeric token first: "123.4"
    if re.fullmatch(r"\d+\.\d+", toks[0]):
        return float(toks[0])
    int_part: list[str] = []
    frac_part: list[str] = []
    seen_point = False
    for t in toks:
        if t in _POINT or t == ".":
            if seen_point:
                break
            seen_point = True
            continue
        if t.isdigit() or t in NUMBER_WORDS:
            (frac_part if seen_point else int_part).append(t)
        else:
            break
    iv = _words_value(int_part)
    if iv is None:
        return None
    if seen_point and frac_part:
        digits = ""
        for t in frac_part:
            if t.isdigit():
                digits += t
            elif t in UNITS:
                digits += str(UNITS[t])
            else:
                fv = _words_value([t])
                digits += str(fv) if fv is not None else ""
        if digits:
            return float(f"{iv}.{digits}")
    return float(iv)


NUM_SPAN = r"(?:\d[\d.]*|(?:" + "|".join(sorted(NUMBER_WORDS | _POINT, key=len, reverse=True)) + r"))"
# a run of number-ish tokens separated by spaces/hyphens
NUM_RUN = rf"{NUM_SPAN}(?:[\s\-]+{NUM_SPAN})*"
