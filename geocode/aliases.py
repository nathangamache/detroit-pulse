"""Spoken nicknames -> canonical public landmark names (ported from the old gazetteer).

Only names, no coordinates: the canonical name is what gets sent to the local index /
Photon / Nominatim. All entries are public places.
"""
from __future__ import annotations

ALIASES = {
    "mgm": "MGM Grand Detroit", "mgm casino": "MGM Grand Detroit", "mgm grand": "MGM Grand Detroit",
    "greektown": "Hollywood Casino at Greektown", "greektown casino": "Hollywood Casino at Greektown",
    "motorcity casino": "MotorCity Casino", "motor city casino": "MotorCity Casino",
    "dmc": "Detroit Medical Center", "harper": "Harper University Hospital", "sinai": "Sinai-Grace Hospital",
    "sinai grace": "Sinai-Grace Hospital", "receiving": "Detroit Receiving Hospital",
    "henry ford": "Henry Ford Hospital", "childrens": "Children's Hospital of Michigan",
    "beaumont royal oak": "Corewell Health William Beaumont University Hospital",
    "u of m hospital": "University of Michigan Hospital", "mott": "C.S. Mott Children's Hospital",
    "st joe's": "Trinity Health Ann Arbor Hospital", "st joes": "Trinity Health Ann Arbor Hospital",
    "ford field": "Ford Field", "little caesars arena": "Little Caesars Arena", "lca": "Little Caesars Arena",
    "comerica": "Comerica Park", "comerica park": "Comerica Park", "the big house": "Michigan Stadium",
    "michigan stadium": "Michigan Stadium", "crisler": "Crisler Center",
    "metro airport": "Detroit Metropolitan Wayne County Airport", "dtw": "Detroit Metropolitan Wayne County Airport",
    "coleman young": "Coleman A. Young International Airport", "willow run": "Willow Run Airport",
    "belle isle": "Belle Isle Park", "hart plaza": "Hart Plaza", "cobo": "Huntington Place",
    "ren cen": "Renaissance Center", "rencen": "Renaissance Center", "renaissance center": "Renaissance Center",
    "fairlane": "Fairlane Town Center", "somerset": "Somerset Collection", "twelve oaks": "Twelve Oaks Mall",
    "briarwood": "Briarwood Mall", "arborland": "Arborland Center", "wayne state": "Wayne State University",
    "u of d": "University of Detroit Mercy", "emu": "Eastern Michigan University",
    "diag": "The Diag, University of Michigan",
}


def canonical_landmark(name: str) -> str:
    key = name.strip().lower().rstrip(".,")
    if key in ALIASES:
        return ALIASES[key]
    if key.startswith("the ") and key[4:] in ALIASES:
        return ALIASES[key[4:]]
    return name
