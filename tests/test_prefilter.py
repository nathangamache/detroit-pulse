"""Prefilter: chatter/noise/detector skipping must keep location-bearing transmissions (recall >= 98%)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.config_files import Channel
from extract.prefilter import classify

RAIL = Channel(id="r", label="rail", kind="rail_road", railroad="CSX")

CHATTER = [
    "Engine 5 on scene.", "Copy, 10-4.", "Medic 49, 49, status on scene.", "Ladder 26 on scene. Ladder 26 on scene.",
    "Car 12-7, go ahead.", "Dispatch, Medic 63 en route.", "Thank you, sir.", "Squad 2 back in service, in quarters.",
    "Engine 41 to Central, show me available.", "Roger, Q326 copies.", "Chief 5, copy that, stand by.",
]
LOCATIONS = [
    "Engine 5 respond to Packard and State.", "16800 East 8 Mile, noise complaint.",
    "We're on 7 Mile Evergreen, accident blocking the road.", "Westbound 94 and livernoise, one response.",
    "Medic 9, 4-6-1-0-0 Grand River between Oakman and Joy.", "Respond to 1433.",
    "the Meijer on Eight Mile, retail fraud", "Box alarm, seven mile at Hartwell.",
    "Car 21, shots fired at the gas station on Michigan.", "two nine four zero one, medical",
    "Engine 4, Washtenaw and Huron Parkway, smoke in the area.", "Go to channel 2. Respond to Stadium and Main.",
]
NOISE = ["", "   ", "Thank you.", "Thanks for watching!", "[MUSIC]", "you"]
DETECTOR_OK = "CSX equipment detector, milepost 123.4, track 1. No defects. No defects. Total axles 412. Detector out."
DETECTOR_PARTIAL = "equipment detector ... defects ... out"


@pytest.mark.parametrize("t", CHATTER)
def test_chatter_skips(t):
    assert classify(t).label == "chatter", t


@pytest.mark.parametrize("t", LOCATIONS)
def test_locations_pass(t):
    assert classify(t).label == "pass", t


@pytest.mark.parametrize("t", NOISE)
def test_noise(t):
    assert classify(t).label == "whisper_noise", repr(t)


def test_noise_phrase_inside_real_transcript_is_not_noise():
    assert classify("Medic 26, 4400 Example Street, difficulty breathing. Thank you.").label == "pass"


def test_detector_complete_skips_partial_goes_to_llm():
    r = classify(DETECTOR_OK, RAIL)
    assert r.label == "defect_detector" and r.detector.milepost == 123.4 and r.detector.result == "no_defects"
    r2 = classify(DETECTOR_PARTIAL, RAIL)
    assert r2.label == "pass" and r2.detector is not None and not r2.detector.complete


def _train_path() -> Path | None:
    p = os.environ.get("PULSE_TRAIN_JSONL",
                       "lora/data/normalize_train.jsonl")
    return Path(p) if Path(p).exists() else None


@pytest.mark.skipif(_train_path() is None, reason="local TRAIN set not present (it is never committed)")
def test_recall_on_train_set_is_at_least_98pct():
    total = kept = 0
    for line in _train_path().read_text().splitlines():
        r = json.loads(line)
        if r["label"] == "NO_LOCATION":
            continue
        t = r["text"].split("<|im_start|>user\n")[1].split("<|im_end|>")[0].replace("Transmission: ", "").strip()
        total += 1
        kept += not classify(t).skip_llm
    assert total > 2000
    assert kept / total >= 0.98, (kept, total)
