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


# Pinned for the godseye host firewall (godseye-detroit-connector deploy/host-firewall/), which
# names these subnets; the connector pins its tailscale sidecar at 172.30.80.20 on godseye_backend.
PINNED = {"pulse_internal": "172.30.81.0/24", "pulse_egress": "172.30.82.0/24", "pulse_debug": "172.30.83.0/24"}
BACKEND = ("172.30.80.0/24", "172.30.80.1")


def test_pulse_networks_have_pinned_subnets():
    import ipaddress
    c = _compose()
    nets = c["networks"]
    seen = [ipaddress.ip_network(BACKEND[0])]
    for name, subnet in PINNED.items():
        cfg = nets[name]["ipam"]["config"]
        assert cfg == [{"subnet": subnet, "gateway": str(ipaddress.ip_network(subnet)[1])}], name
        net = ipaddress.ip_network(subnet)
        assert net.subnet_of(ipaddress.ip_network("172.30.0.0/16")) and net.prefixlen == 24, name
        assert not any(net.overlaps(o) for o in seen), name
        seen.append(net)
    assert nets["pulse_internal"]["internal"] is True
    assert not nets["pulse_egress"].get("internal") and not nets["pulse_debug"].get("internal")
    # only the two workers that call out (Anthropic, geocoders, Whisper download) get pulse_egress
    on_egress = {n for n, s in c["services"].items() if "pulse_egress" in (s.get("networks") or [])}
    assert on_egress == {"pulse-transcribe", "pulse-extract"}
    on_debug = {n for n, s in c["services"].items() if "pulse_debug" in (s.get("networks") or [])}
    assert on_debug == {"pulse-debug"}


def test_every_service_uses_public_dns_without_search_domain():
    for name, s in _compose()["services"].items():
        assert s.get("dns") == ["${PULSE_DNS_1:-1.1.1.1}", "${PULSE_DNS_2:-9.9.9.9}"], name
        assert s.get("dns_search") == ["."], name


def test_setup_pins_and_checks_godseye_backend():
    text = (REPO / "deploy" / "setup.sh").read_text()
    assert f'NET_SUBNET="${{GODSEYE_BACKEND_SUBNET:-{BACKEND[0]}}}"' in text
    assert f'NET_GATEWAY="${{GODSEYE_BACKEND_GATEWAY:-{BACKEND[1]}}}"' in text
    assert '--internal --subnet "$NET_SUBNET" --gateway "$NET_GATEWAY"' in text
    # an existing network with another subnet (or not internal) stops setup.sh
    assert '"$have" != "$NET_SUBNET $NET_GATEWAY" || "$internal" != "true"' in text
    # the subnets it warns about are the ones compose pins
    import re
    specs = dict(x.split("=") for x in re.search(r"for spec in ([^;]+);", text).group(1).split())
    assert specs == PINNED
    # the few-shot hint never changes the file's mode
    assert "setfacl -m u:10010:r" in text
    assert not re.search(r"^\s*(chmod|chown|setfacl)\b.*FEWSHOTS", text, re.M)


def test_receiver_heartbeats_reach_the_api_read_only_without_network():
    """rtl_airband's stats file and traintap's heartbeat land in pulse_receivers, which pulse-api
    reads (core/receivers.py) and pulse-transcribe's ingest stage records as history
    (core/liveness.py), both read-only. Nothing gains a network or a port."""
    c = _compose()
    svcs = c["services"]
    assert c["volumes"]["pulse_receivers"] == {"name": "pulse_receivers"}
    api_env = svcs["pulse-api"]["environment"]
    assert "pulse_receivers:/data/receivers:ro" in svcs["pulse-api"]["volumes"]
    assert api_env["RECEIVER_STATUS_DIR"] == "/data/receivers"
    # the ingest stage (sdr_files, traintap) records the receivers' heartbeats with its own
    tr = svcs["pulse-transcribe"]
    assert "pulse_receivers:/data/receivers:ro" in tr["volumes"]
    assert tr["environment"]["RECEIVER_STATUS_DIR"] == "/data/receivers"
    assert "ingest" in tr["command"][tr["command"].index("--stages") + 1].split(",")
    assert "pulse_receivers:/status" in svcs["rail-airband"]["volumes"]
    assert "pulse_receivers:/status" in svcs["traintap-eot"]["volumes"]
    cmd = svcs["traintap-eot"]["command"]
    assert cmd[cmd.index("--heartbeat") + 1] == "/status/traintap-eot.heartbeat"
    assert 'stats_filepath = "/status/rail-airband.prom";' in (REPO / "deploy" / "sdr" / "rail.conf").read_text()
    # only the two readers (read-only) and the two SDR writers mount it; the SDR side stays offline
    users = {n for n, s in svcs.items() if any(str(v).startswith("pulse_receivers:") for v in s.get("volumes", []))}
    assert users == {"pulse-api", "pulse-transcribe", "rail-airband", "traintap-eot"}
    for reader in ("pulse-api", "pulse-transcribe"):
        assert [v for v in svcs[reader]["volumes"] if str(v).startswith("pulse_receivers:")] == [
            "pulse_receivers:/data/receivers:ro"], reader
    for sdr in ("rail-airband", "traintap-eot"):
        assert svcs[sdr]["network_mode"] == "none" and not svcs[sdr].get("ports")
    # the image creates the mount point the volume copies its owner/mode from
    docker = (REPO / "deploy" / "docker" / "Dockerfile").read_text()
    assert "/data/receivers" in docker and "chmod 2775 /data/sdr /data/traintap /data/receivers" in docker


def test_sample_rail_conf_has_no_channels_and_required_settings():
    text = (REPO / "deploy" / "sdr" / "rail.conf").read_text()
    for needle in ('serial = "RAIL160"', "centerfreq = 160.8975", "sample_rate = 2.56", R.MARKER,
                   'stats_filepath = "/status/rail-airband.prom";'):
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
        Channel(id="cand", label="v", kind="rail_voice", freq_mhz=160.65, templates=["cand_t"]),
    ]
    out, used, skipped = R.render(template, chans)
    assert [u.split(":")[0] for u in used] == ["road_a", "disp", "cand"]
    assert any(s.startswith("det_a") for s in skipped) and any(s.startswith("far") for s in skipped)
    assert out.count("split_on_transmission = true;") == 4      # 3 channels + the documented example
    assert 'filename_template = "disp"' in out and "freq = 161.2500;" in out
    assert 'label = "cand";' in out                             # label = channel id: the heartbeat's channel list
    assert out.count('stats_filepath = "/status/rail-airband.prom";') == 1
    assert R.MARKER not in out.split("# Channel block")[0]
    empty, used0, _ = R.render(template, [])
    assert not used0 and R.MARKER in empty
