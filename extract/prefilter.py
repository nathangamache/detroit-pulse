"""Rule-based prefilter: decide which transmissions are worth a Claude call.

Skipped (stored, never dropped):
  whisper_noise    empty/punctuation or a known Whisper hallucination ("Thank you.")
  chatter          only acknowledgements, status phrases, unit IDs, codes and short numbers:
                   "Engine 5 on scene", "copy, 10-4", "Medic 49, 49, status"
  defect_detector  a wayside detector readout the rules parser fully understood
Everything else is `pass` and goes to the batched Claude call.

The design target is recall: a transmission is only called chatter when NOTHING
in it could be a place. Any leftover word that isn't in the chatter lexicon, any
digit group of 3+ digits (a possible house number) and any run of 3+ number words
keeps it. scripts/measure_prefilter.py checks that >= 98% of location-bearing
transmissions in the TRAIN set and the archive DB still pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.config_files import Channel
from rail import detector as det
from rail.numbers import NUMBER_WORDS

# multi-word status/ack phrases are removed first, so "on"/"at" left over still count as cues
CHATTER_PHRASES = [
    r"10[\s-]?4", r"ten[\s-]?four", r"10[\s-]?\d{1,2}", r"code\s+\w+", r"signal\s+\d+", r"on\s+scene", r"on\s+location",
    r"on\s+the\s+air", r"en\s*route", r"in\s+route", r"in\s+service", r"out\s+of\s+service", r"back\s+in\s+service",
    r"in\s+quarters", r"returning\s+to\s+quarters", r"go\s+ahead", r"stand\s*by", r"show\s+me", r"be\s+advised",
    r"radio\s+check", r"how\s+(?:do\s+you\s+)?copy", r"loud\s+and\s+clear", r"thank\s+you", r"no\s+further",
    r"good\s+copy", r"copy\s+that", r"over\s+and\s+out", r"you're\s+clear", r"you\s+are\s+clear",
    r"all\s+units", r"come\s+in", r"say\s+again", r"switch(?:ing)?\s+(?:to\s+)?(?:channel|tac|fire\s*ground)\s*\w*",
    r"go\s+to\s+(?:channel|tac)\s*\w*", r"one\s+moment", r"just\s+a\s+(?:sec|second|moment)",
]
CHATTER_WORDS = {
    # acknowledgements / status
    "copy", "copies", "copied", "received", "roger", "affirmative", "negative", "ok", "okay", "alright", "right",
    "thanks", "thank", "you", "yes", "yeah", "yep", "no", "nope", "sir", "maam", "ma'am", "please", "uh", "um",
    "clear", "clearing", "cleared", "available", "status", "check", "checking", "responding", "arrived",
    "arriving", "enroute", "staging", "staged", "busy", "out", "over", "again", "repeat", "standing", "stand",
    "by", "advise", "advised", "go", "ahead", "air", "radio", "service", "quarters", "returning", "return",
    "transport", "transporting", "en", "route", "scene", "back", "time", "minutes", "minute", "eta", "the", "a", "an",
    "is", "are", "was", "be", "i", "i'm", "im", "we", "we're", "were", "i'll", "we'll", "will", "and", "to", "for",
    "with", "me", "us", "your", "you're", "my", "our", "it", "that", "this", "just", "also", "still", "now",
    "got", "have", "has", "had", "here", "there", "all", "units", "unit", "dispatch", "central", "command",
    "base", "county", "copying", "good", "fine", "correct", "affirm", "ten", "four",
    # agency / unit words
    "engine", "medic", "medics", "ladder", "truck", "squad", "rescue", "tower", "chief", "battalion", "car",
    "ambulance", "ems", "fire", "police", "pd", "fd", "sheriff", "deputy", "sergeant", "sgt", "lieutenant",
    "captain", "officer", "patrol", "station", "dispatcher", "crew", "brush", "tanker", "rig",
    # railroad acknowledgements
    "dispatchers", "conductor", "engineer", "train", "copies", "roger", "ack",
}
# Phrases Whisper hallucinates on silence/noise. A transmission is noise only if NOTHING else is left
# after removing them (they also appear inside real transcripts, e.g. "...breathing. Thank you.").
HALLUCINATIONS = [
    r"thank\s*you(?:\s+(?:so\s+much|very\s+much))?(?:\s+for\s+watching)?", r"thanks\s+for\s+watching",
    r"(?:please\s+)?(?:like\s+and\s+)?subscribe(?:\s+to\s+(?:my|the|our)\s+channel)?", r"see\s+you\s+next\s+time",
    r"\[?(?:music|silence|blank_audio|inaudible|noise|applause)\]?", r"\bbye\b", r"\byou\b", r"\bso\b", r"\bokay\b",
]
_ALNUM_ID = re.compile(r"^(?!i\d|m\d|us\d)[a-z]{1,2}\d{2,4}[a-z]?$")   # train symbols / unit ids, not I-94/M-14


@dataclass
class PrefilterResult:
    label: str                       # pass | chatter | whisper_noise | defect_detector
    reason: str
    detector: det.DetectorReading | None = None

    @property
    def skip_llm(self) -> bool:
        return self.label != "pass"


def _norm(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[‘’]", "'", t)
    return t


def _tokens(t: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", t)


def is_chatter(text: str) -> tuple[bool, str]:
    t = _norm(text)
    for p in CHATTER_PHRASES:
        t = re.sub(r"\b" + p + r"\b", " ", t)
    toks = _tokens(t)
    if not toks:
        return True, "only status phrases"
    leftovers = []
    num_run = 0
    for tok in toks:
        if tok.isdigit():
            if len(tok) >= 3:
                return False, f"number {tok} could be an address"
            num_run += 1
        elif tok in NUMBER_WORDS:
            num_run += 1
        else:
            num_run = 0
            if tok not in CHATTER_WORDS and not _ALNUM_ID.match(tok):
                leftovers.append(tok)
        if num_run >= 3:
            return False, "spoken digit run could be an address"
    if leftovers:
        return False, f"content words: {' '.join(leftovers[:5])}"
    return True, "acknowledgement/status/unit ids only"


def classify(text: str, channel: Channel | None = None) -> PrefilterResult:
    raw = (text or "").strip()
    low = _norm(raw)
    rest = low
    for p in HALLUCINATIONS:
        rest = re.sub(p, " ", rest)
    if not re.search(r"[a-z0-9]", rest):
        return PrefilterResult("whisper_noise", "empty or only Whisper hallucination phrases")
    if det.looks_like_detector(raw) and (channel is None or channel.is_rail or "detector" in low or "defect" in low):
        reading = det.parse(raw)
        if reading.complete:
            return PrefilterResult("defect_detector", f"detector readout ({reading.result})", reading)
        return PrefilterResult("pass", "detector readout the rules couldn't complete; LLM fallback", reading)
    chatter, why = is_chatter(raw)
    if chatter:
        return PrefilterResult("chatter", why)
    return PrefilterResult("pass", why)
