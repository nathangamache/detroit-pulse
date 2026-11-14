#!/usr/bin/env python3
"""Fill deploy/sdr/rail.conf's empty channel list from config/channels(.local).yaml.

    uv run python scripts/render_rail_conf.py                 # -> deploy/sdr/rail.local.conf (gitignored)
    uv run python scripts/render_rail_conf.py --check         # print what would be included

Takes every enabled rail_road / rail_dispatcher / rail_yard / rail_detector / rail_voice channel with a freq_mhz
inside the dongle's usable band (centre +- (sample_rate/2 - 0.08) MHz; 160.8975 MHz at 2.56 MS/s by
default, read from the template) and writes one NFM channel per frequency with a file output into
/recordings. Channels sharing a frequency (a detector on the road channel) are written once, with
the first channel's template. Frequencies never go into the committed template. The template's
global `stats_filepath` (rtl_airband's stats file, Pulse's receiver heartbeat) is kept as is; each
channel's `label` is its Pulse channel id, which is how /v1/feeds/status tells which feeds the
running receiver actually demodulates.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
VOICE_RAIL = {"rail_road", "rail_dispatcher", "rail_yard", "rail_detector", "rail_voice"}
MARKER = "# @CHANNELS@"

BLOCK = """      {{
        freq = {freq:.4f};
        label = "{label}";
        modulation = "nfm";
        outputs: (
          {{
            type = "file";
            directory = "/recordings";
            filename_template = "{template}";
            split_on_transmission = true;
            include_freq = true;
            dated_subdirectories = true;
            split_tx_drop_shorter_than = 0.5;
            split_max_file_time = 60;
          }}
        );
      }}"""


def _num(text: str, key: str) -> float:
    m = re.search(rf"^\s*{key}\s*=\s*([0-9.]+)\s*;", text, re.M)
    if not m:
        sys.exit(f"template has no `{key} = ...;`")
    return float(m.group(1))


def render(template: str, channels) -> tuple[str, list[str], list[str]]:
    center, rate = _num(template, "centerfreq"), _num(template, "sample_rate")
    half = rate / 2 - 0.08                    # keep clear of the band edges
    blocks, used, skipped, seen = [], [], [], set()
    for c in channels:
        if c.kind not in VOICE_RAIL or not c.enabled:
            continue
        if c.freq_mhz is None:
            skipped.append(f"{c.id}: no freq_mhz")
            continue
        if abs(c.freq_mhz - center) > half:
            skipped.append(f"{c.id}: {c.freq_mhz} MHz outside {center - half:.3f}-{center + half:.3f}")
            continue
        key = round(c.freq_mhz, 4)
        if key in seen:
            skipped.append(f"{c.id}: {c.freq_mhz} MHz already covered")
            continue
        seen.add(key)
        tmpl = (c.templates[0] if c.templates else c.id)
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", tmpl) or not re.fullmatch(r"[A-Za-z0-9_\-.]+", c.id):
            skipped.append(f"{c.id}: template/id must be [A-Za-z0-9_-]")
            continue
        blocks.append(BLOCK.format(freq=c.freq_mhz, label=c.id, template=tmpl))
        used.append(f"{c.id}: {c.freq_mhz:.4f} MHz -> {tmpl}")
    if MARKER not in template:
        sys.exit(f"template has no '{MARKER}' line")
    out = template.replace(f"      {MARKER}", ",\n".join(blocks) if blocks else f"      {MARKER}")
    return out, used, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", type=Path, default=REPO / "deploy" / "sdr" / "rail.conf")
    ap.add_argument("--out", type=Path, default=REPO / "deploy" / "sdr" / "rail.local.conf")
    ap.add_argument("--check", action="store_true", help="print the selection, write nothing")
    a = ap.parse_args()
    from core.config_files import load_channels
    text, used, skipped = render(a.template.read_text(), load_channels())
    for u in used:
        print("  +", u)
    for s in skipped:
        print("  -", s)
    if a.check:
        return
    if not used:
        print("WARNING: no rail channels in range; rtl_airband will refuse an empty channel list", file=sys.stderr)
    a.out.write_text(text)
    print(f"wrote {a.out} ({len(used)} channel(s))")


if __name__ == "__main__":
    main()
