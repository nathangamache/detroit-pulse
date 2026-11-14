"""Container stack guards: compose hardening invariants and the rail.conf renderer (no Docker needed)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from core.config_files import Channel

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import render_rail_conf as R  # noqa: E402


def _compose() -> dict:
    return yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())


def test_every_service_is_hardened():
    svcs = _compose()["services"]
    assert {"pulse-api", "pulse-transcribe", "pulse-extract", "pulse-postgis", "pulse-redis"} <= set(svcs)
    for name, s in svcs.items():
        assert s.get("read_only") is True, name
        assert s.get("cap_drop") == ["ALL"] and not s.get("cap_add"), name
        assert "no-new-privileges:true" in s.get("security_opt", []), name
        user = str(s.get("user", ""))
        assert user and not user.startswith(("0", "root")), name
        assert s.get("mem_limit") and s.get("pids_limit") and s.get("cpus"), name
        assert not s.get("privileged"), name
        for v in s.get("volumes", []):
            src = str(v).split(":")[0]
            assert "docker.sock" not in src and src not in ("~", "/home", "${HOME}"), (name, v)
            if src.startswith(("/", "~", "..", "./", "$")) and not src.startswith("/dev/bus/usb"):
                assert str(v).endswith(":ro"), f"{name}: bind mount must be read-only: {v}"
        # nothing is published except the opt-in debug forwarder, and only on loopback
        for p in s.get("ports", []):
            assert name == "pulse-debug" and str(p).startswith("127.0.0.1:"), (name, p)


def test_networks_and_profiles():
    c = _compose()
    nets = c["networks"]
    assert nets["pulse_internal"]["internal"] is True
    assert nets["godseye_backend"] == {"external": True, "name": "godseye_backend"}
    svcs = c["services"]
    assert "godseye_backend" in svcs["pulse-api"]["networks"]
    assert svcs["pulse-api"]["networks"]["godseye_backend"]["aliases"] == ["pulse-api"]
    for db in ("pulse-postgis", "pulse-redis"):
        assert svcs[db]["networks"] == ["pulse_internal"]
    for sdr in ("rail-airband", "traintap-eot"):
        assert svcs[sdr]["profiles"] == ["sdr"] and svcs[sdr]["network_mode"] == "none"
        assert svcs[sdr]["device_cgroup_rules"] == ["c 189:* rmw"]
    assert svcs["pulse-debug"]["profiles"] == ["debug"] and svcs["pulse-frontend"]["profiles"] == ["frontend"]
    assert c["volumes"]["pulse_pgdata"]["external"] is True


def test_sample_rail_conf_has_no_channels_and_required_settings():
    text = (REPO / "deploy" / "sdr" / "rail.conf").read_text()
    for needle in ('serial = "RAIL160"', "centerfreq = 160.8975", "sample_rate = 2.56", R.MARKER):
        assert needle in text
    import re
    body = text.split("# Channel block")[0]
    assert not re.search(r"^\s*freq\s*=", body, re.M)    # no frequency is committed


def test_render_rail_conf_selects_in_band_rail_channels():
    template = (REPO / "deploy" / "sdr" / "rail.conf").read_text()
    chans = [
        Channel(id="road_a", label="a", kind="rail_road", freq_mhz=160.215, templates=["rail_road"]),
        Channel(id="det_a", label="d", kind="rail_detector", freq_mhz=160.215, templates=["rail_det"]),
        Channel(id="disp", label="b", kind="rail_dispatcher", freq_mhz=161.25),
        Channel(id="far", label="c", kind="rail_road", freq_mhz=452.9),
        Channel(id="fire", label="f", kind="fire", freq_mhz=160.5),
        Channel(id="off", label="o", kind="rail_yard", freq_mhz=160.9, enabled=False),
    ]
    out, used, skipped = R.render(template, chans)
    assert [u.split(":")[0] for u in used] == ["road_a", "disp"]
    assert any(s.startswith("det_a") for s in skipped) and any(s.startswith("far") for s in skipped)
    assert out.count("split_on_transmission = true;") == 3      # 2 channels + the documented example
    assert 'filename_template = "disp"' in out and "freq = 161.2500;" in out
    assert R.MARKER not in out.split("# Channel block")[0]
    empty, used0, _ = R.render(template, [])
    assert not used0 and R.MARKER in empty
