"""Street-name normalization for scanner-radio geocoding in Metro Detroit and Washtenaw.

    core_key("W 7 Mile Rd")             -> "7 mile"
    core_key("Seven Mile")              -> "7 mile"
    core_key("East Jefferson Avenue")   -> "jefferson"
    core_key("I-94")                    -> "i 94"
    core_key("the Lodge")               -> "m 10"       (spoken freeway name)
    core_key("Edsel Ford Freeway")      -> "i 94"
    core_key("Ann Arbor Trail")         -> "ann arbor"  full_key(...) -> "ann arbor trail"

The core key drops directionals and the street-type suffix, so "Gratiot", "Gratiot Ave" and
"Gratiot Avenue" match. Where two different roads share a core key in the index (Plymouth has
both Ann Arbor Trail and Ann Arbor Road), the builder also indexes the full key, and a query
that names the suffix uses it (see LocalGeocoder.resolve_keys).

Every table here holds public road names only.
"""
from __future__ import annotations

import re

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty one": 21,
    "twenty two": 22, "twenty three": 23, "twenty four": 24, "twenty five": 25,
    "twenty six": 26, "twenty seven": 27, "twenty eight": 28, "twenty nine": 29,
    "thirty": 30, "thirty one": 31, "thirty two": 32,
}
ORDINAL_WORDS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
    "thirteenth": "13th", "fourteenth": "14th", "fifteenth": "15th",
    "sixteenth": "16th", "eighteenth": "18th", "twentieth": "20th",
}
DIRECTIONALS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "n": "n", "s": "s", "e": "e", "w": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "ne": "ne", "nw": "nw", "se": "se", "sw": "sw",
}
# street-type suffixes stripped from the core key (kept in the full key)
SUFFIXES = {
    "street", "st", "avenue", "ave", "av", "road", "rd", "boulevard", "blvd",
    "drive", "dr", "lane", "ln", "court", "ct", "place", "pl", "parkway",
    "pkwy", "highway", "hwy", "freeway", "fwy", "expressway", "expy", "terrace",
    "ter", "circle", "cir", "way", "trail", "trl", "square", "sq", "row",
    "alley", "aly", "crescent", "service", "svc", "frontage",
}
# full freeway names (canonical tokens) -> route key; checked at every strip step, so
# "ford freeway" != "ford road"
FREEWAY_ALIASES = {
    "lodge freeway": "m 10", "john c lodge freeway": "m 10", "john c lodge": "m 10",
    "southfield freeway": "m 39",
    "jeffries freeway": "i 96",
    "fisher freeway": "i 75", "chrysler freeway": "i 75",
    "walter p chrysler freeway": "i 75",
    "ford freeway": "i 94", "edsel ford freeway": "i 94", "edsel ford": "i 94",
    "davison freeway": "m 8",
    "reuther freeway": "i 696", "walter p reuther freeway": "i 696",
    "van dyke freeway": "m 53",
    "interstate 94": "i 94", "interstate 96": "i 96", "interstate 75": "i 75",
    "interstate 375": "i 375", "interstate 696": "i 696", "interstate 275": "i 275",
    "interstate 96 business": "i 96",
}
# bare spoken freeway names: only when the WHOLE input is this (so "Fisher Rd" in Grosse
# Pointe stays "fisher", but "the Fisher" -> I-75)
BARE_FREEWAY = {
    "lodge": "m 10", "jeffries": "i 96", "fisher": "i 75", "chrysler": "i 75",
    "reuther": "i 696", "ford freeway": "i 94", "696": "i 696", "275": "i 275",
    "94": "i 94", "96": "i 96", "75": "i 75", "375": "i 375", "23": "us 23",
}
# corridor aliases (one physical road, several spoken names), applied after suffix
# stripping. Each key resolves to the keys tried, in order.
CORRIDOR_ALIASES = {
    "6 mile": ["mcnichols", "6 mile"],
    "mcnichols": ["mcnichols", "6 mile"],
    # Oakland County mile roads that carry a name instead of a number
    "15 mile": ["15 mile", "maple"],
    "16 mile": ["big beaver", "metropolitan", "16 mile"],
    "17 mile": ["17 mile", "wattles"],
    "18 mile": ["18 mile", "long lake"],
    "19 mile": ["19 mile", "square lake"],
    "baseline": ["8 mile"],
    "base line": ["8 mile"],
    "grand blvd": ["grand"],
    "the boulevard": ["grand"],
    "m 1": ["woodward"], "m 3": ["gratiot"], "m 102": ["8 mile"],
    "m 153": ["ford"], "m 85": ["fort"], "us 24": ["telegraph"], "us 12": ["michigan"],
    "m 97": ["groesbeck"], "m 5": ["grand river"],
    # Detroit freeway corridors whose service drives scanners call by name
    "southfield": ["southfield", "m 39"], "davison": ["davison", "m 8"],
    "m 39": ["m 39", "southfield"], "m 8": ["m 8", "davison"],
    "lodge": ["lodge", "m 10"], "fisher": ["fisher", "i 75"],
}
# Whisper / pronunciation misspellings heard on scanner audio (public street names)
SPOKEN_FIXES = {
    "grashit": "gratiot", "grash it": "gratiot", "gratio": "gratiot",
    "grayshit": "gratiot", "de quindre": "dequindre", "dequinder": "dequindre",
    "shoenherr": "schoenherr", "shoenher": "schoenherr", "schoner": "schoenherr",
    "laser": "lahser", "livernoise": "livernois", "liver noise": "livernois",
    "mcnicholas": "mcnichols", "mc nichols": "mcnichols",
    "kirchival": "kercheval", "conners": "conner",
    "vandyke": "van dyke", "johnr": "john r", "john are": "john r",
    "tiremen": "tireman", "middle belt": "middlebelt",
    "seven mi": "7 mile", "eisenhauer": "eisenhower", "washtenah": "washtenaw",
    "washtenau": "washtenaw", "packer": "packard", "huron park way": "huron parkway",
}
TOKEN_CANON = {
    "fwy": "freeway", "frwy": "freeway", "expy": "freeway",
    "expressway": "freeway", "xway": "freeway", "svc": "service", "srvc": "service",
    "serv": "service", "dr": "drive", "rd": "road", "st": "street", "ave": "avenue",
    "av": "avenue", "blvd": "boulevard", "hwy": "highway", "pkwy": "parkway",
    "ln": "lane", "ct": "court", "pl": "place", "ter": "terrace", "cir": "circle",
    "trl": "trail", "sq": "square", "aly": "alley",
}
_TRAILING_DIRS = ("n", "s", "e", "w", "north", "south", "east", "west")

_punct = re.compile(r"[.,'’`\"()]")
_ws = re.compile(r"\s+")
_REF = re.compile(r"^(i|m|us) \d{1,3}$")
_FIXES = [(re.compile(rf"\b{k}\b"), v) for k, v in SPOKEN_FIXES.items()]
_NUMWORD_MILE = [(re.compile(rf"\b{w}\b(?=\s+mile\b)"), str(NUM_WORDS[w]))
                 for w in sorted(NUM_WORDS, key=len, reverse=True)]
_ORDINALS = [(re.compile(rf"\b{w}\b"), o) for w, o in ORDINAL_WORDS.items()]


def basic(s: str) -> str:
    """Lowercase, '&'->'and', drop punctuation, split refs ("i94" -> "i 94")."""
    s = (s or "").lower().strip()
    s = s.replace("&", " and ").replace("@", " at ")
    s = _punct.sub("", s)
    s = s.replace("-", " ").replace("/", " / ")
    s = re.sub(r"\b(i|m|us)(\d{1,3})\b", r"\1 \2", s)
    return _ws.sub(" ", s).strip()


def words_to_numbers(s: str) -> str:
    """"twenty one mile" -> "21 mile", "second" -> "2nd", "7 mi" -> "7 mile"."""
    for rx, n in _NUMWORD_MILE:
        s = rx.sub(n, s)
    for rx, o in _ORDINALS:
        s = rx.sub(o, s)
    if re.search(r"\d+\s+mi\b", s):
        s = re.sub(r"\bmi\b(?=\s|$)", "mile", s)
    return s


def _alias(s: str) -> str | None:
    if s in FREEWAY_ALIASES:
        return FREEWAY_ALIASES[s]
    if _REF.match(s):
        return s
    return None


def _tokens(name: str) -> list[str]:
    s = basic(name)
    if not s:
        return []
    s = re.sub(r"^the ", "", s)
    for rx, v in _FIXES:
        s = rx.sub(v, s)
    s = words_to_numbers(s)
    toks = s.split()
    # leading "St"/"Ste"/"Mt" = Saint/Sainte/Mount ("St Antoine" == "Saint Antoine"), also
    # after a directional ("E St Clair")
    for i in range(min(2, len(toks) - 1)):
        if toks[i] in ("st", "ste", "mt") and (i == 0 or toks[i - 1] in DIRECTIONALS):
            toks[i] = {"st": "saint", "ste": "sainte", "mt": "mount"}[toks[i]]
            break
    return [TOKEN_CANON.get(t, t) for t in toks]


def _split(name: str) -> tuple[str, str | None]:
    """-> (core key, canonical suffix or None). A route/freeway alias has no suffix."""
    toks = _tokens(name)
    if not toks:
        return "", None
    joined = " ".join(toks)
    # route numbers embedded in names: "jeffries freeway i 96 service"
    m = re.search(r"\b(i|m|us) (\d{1,3})\b", joined)
    if m and any(t in ("freeway", "service") for t in toks):
        return f"{m.group(1)} {m.group(2)}", None
    if joined in BARE_FREEWAY:
        return BARE_FREEWAY[joined], None
    a = _alias(joined)
    if a:
        return a, None
    # strip leading directionals (never to empty)
    while len(toks) > 1 and toks[0] in DIRECTIONALS:
        toks = toks[1:]
        a = _alias(" ".join(toks))
        if a:
            return a, None
    # strip trailing directionals / suffix tokens, checking aliases at each step
    suffix = None
    while len(toks) > 1 and (toks[-1] in SUFFIXES or toks[-1] in _TRAILING_DIRS):
        a = _alias(" ".join(toks))
        if a:
            return a, None
        if toks[-1] in SUFFIXES and suffix is None:
            suffix = toks[-1]
        toks = toks[:-1]
    core = " ".join(toks)
    a = _alias(core)
    return (a, None) if a else (core, suffix)


def core_key(name: str) -> str:
    """Normalize a street name to its comparable core key."""
    return _split(name)[0]


def full_key(name: str) -> str | None:
    """Core key plus the street type ("ann arbor trail"), or None when no type was given."""
    core, sfx = _split(name)
    return f"{core} {sfx}" if core and sfx else None


def street_suffix(name: str) -> str | None:
    return _split(name)[1]


_ALT_ROUTE = re.compile(r"\b(bus|business|br|bl|bs|alt|alternate|byp|bypass|conn|connector|spur|loop|truck)\b")


def ref_keys(ref: str) -> list[str]:
    """OSM ref "I 94;US 12" -> ["i 94", "us 12"]. Business/alternate routes are skipped:
    Washtenaw Ave is "US 23 BR", and must not answer for the US-23 freeway."""
    out = []
    for r in re.split(r"[;,]", ref or ""):
        b = basic(r)
        if _ALT_ROUTE.search(b):
            continue
        m = re.match(r"^(i|m|us)\s*(\d{1,3})\b", b)
        if m:
            out.append(f"{m.group(1)} {m.group(2)}")
    return out


def expand_aliases(key: str) -> list[str]:
    """Keys to try for a query key, in priority order."""
    keys = [key]
    for alt in CORRIDOR_ALIASES.get(key, []):
        if alt not in keys:
            keys.append(alt)
    return keys


def leading_dir(name: str) -> str | None:
    toks = basic(name).split()
    if toks and toks[0] in DIRECTIONALS and len(toks) > 1:
        return DIRECTIONALS[toks[0]]
    return None


_CITY_WORDS = ("charter township of", "charter township", "township of", "township",
               "city of", "village of", " city", " twp")


def city_norm(c: str | None) -> str:
    """'Charter Township of Canton' -> 'canton', 'Plymouth Twp' -> 'plymouth'."""
    c = (c or "").lower()
    for w in _CITY_WORDS:
        c = c.replace(w, "")
    return _ws.sub(" ", c).strip()


def city_match(want: str | None, have: str | None) -> bool:
    a, b = city_norm(want), city_norm(have)
    return bool(a) and bool(b) and (a in b or b in a)
