#!/usr/bin/env python3
"""Evaluate the local geocoder index against public phrases with independent ground truth.

    uv run python scripts/eval_geocoder.py                       # run every phrase file, print a report
    uv run python scripts/eval_geocoder.py --json /tmp/out.json  # + per-phrase results
    uv run python scripts/eval_geocoder.py truth scripts/geocoder_eval/phrases_suburbs.json
        # (re)compute ground truth for phrases that carry a truth_spec, from TIGER files in the
        # data dir (fetch them with: build_geocoder_index.py --eval-data --download-only)

Phrase files (scripts/geocoder_eval/*.json) hold PUBLIC places only: major intersections, freeway
crossings, public buildings and parks, and phrases already public in this repo's history. No
transcripts or personal locations. Each phrase: id, phrase, area, bias [lat, lon], structured
(what the extractor emits), truth {lat, lon} or null, truth_source, tolerance_m.

Truth sources and how independent they are from the index:
  detroit_centerline_*        City of Detroit street centerlines      independent of OSM
  tiger_intersection/roads    Census TIGER/Line shared vertex         independent of OSM
  tiger_addr_interp           TIGER range interpolation               NOT independent where the
                                                                      geocoder also used TIGER
  detroit_address_pts         City of Detroit points                  same data as the index
  census_geocoder             Census (TIGER based)                    partly circular
  tiger_arealm                TIGER area landmark polygon centroid    independent of OSM
  knowledge                   hand-entered approximate coordinate     weakest
Run it after every index rebuild; it never touches the network.
"""
from __future__ import annotations

import argparse
import json
import math
import resource
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
EVAL_DIR = REPO / "scripts" / "geocoder_eval"
AREA_ORDER = ["detroit", "wayne_west", "wayne_other", "oakland", "washtenaw", "macomb"]
AREA_LABEL = {"detroit": "Detroit", "wayne_west": "Plymouth/Northville/Canton", "wayne_other": "other Wayne",
              "oakland": "Oakland", "washtenaw": "Ann Arbor/Washtenaw", "macomb": "Macomb"}


def hav(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(d))


def pct(v: list[float], p: float):
    return round(sorted(v)[int(p * (len(v) - 1))], 1) if v else None


def load_phrases(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        data = json.loads(p.read_text())
        for r in data["phrases"]:
            rows.append({**r, "file": p.name})
    return rows


# ── run ─────────────────────────────────────────────────────────────────────

def run(index_dir: Path, paths: list[Path], out_json: Path | None, verbose: bool) -> dict:
    from geocode.local import LocalGeocoder
    g = LocalGeocoder(index_dir)
    rss_after_load = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    rows = load_phrases(paths)
    res, lat_ms = [], []
    for r in rows:
        st = r["structured"]
        if st.get("kind") == "none":
            res.append({**r, "result": None, "verdict": "skipped_no_location", "error_m": None})
            continue
        t0 = time.perf_counter()
        out = g.geocode(st, bias=tuple(r["bias"]))
        lat_ms.append((time.perf_counter() - t0) * 1000)
        err, verdict = None, ("no_result" if out is None else "no_truth")
        if out and r.get("truth"):
            err = hav((out["lat"], out["lng"]), (r["truth"]["lat"], r["truth"]["lon"]))
            verdict = "hit" if err <= r["tolerance_m"] else "miss"
        elif out is None and not r.get("truth"):
            verdict = "no_result_no_truth"
        res.append({**r, "result": out, "error_m": None if err is None else round(err, 1),
                    "latency_ms": round(lat_ms[-1], 2), "verdict": verdict})

    def summarize(sub: list[dict]) -> dict:
        scored = [x for x in sub if x.get("truth") and x["structured"].get("kind") != "none"]
        errs = [x["error_m"] for x in scored if x["error_m"] is not None]
        hits = sum(x["verdict"] == "hit" for x in scored)
        return {"n": len(scored), "hits": hits, "hit_rate": round(hits / len(scored), 3) if scored else None,
                "no_result": sum(x["verdict"] == "no_result" for x in scored),
                "median_err_m": round(statistics.median(errs), 1) if errs else None, "p90_err_m": pct(errs, 0.9)}

    by_area = {a: summarize([x for x in res if x.get("area") == a]) for a in AREA_ORDER
               if any(x.get("area") == a for x in res)}
    by_kind = {k: summarize([x for x in res if x["structured"].get("kind") == k])
               for k in sorted({x["structured"].get("kind") for x in res} - {"none"})}
    by_file = {f: summarize([x for x in res if x["file"] == f]) for f in sorted({x["file"] for x in res})}
    inter = [x["error_m"] for x in res if x["structured"].get("kind") == "intersection" and x["error_m"] is not None
             and x["verdict"] == "hit"]
    summary = {
        "index": g.info(), "rss_mb_after_load": round(rss_after_load),
        "rss_mb_peak": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "all": summarize(res), "by_area": by_area, "by_kind": by_kind, "by_file": by_file,
        "intersection_median_err_m": round(statistics.median(inter), 1) if inter else None,
        "latency_ms": {"median": round(statistics.median(lat_ms), 2), "p95": pct(lat_ms, 0.95),
                       "max": round(max(lat_ms), 1)} if lat_ms else None,
        "negatives_skipped": sum(x["verdict"] == "skipped_no_location" for x in res),
        "no_truth_results": [{"id": x["id"], "verdict": x["verdict"],
                              "got": (x["result"] or {}).get("source")} for x in res
                             if not x.get("truth") and x["structured"].get("kind") != "none"],
    }
    print(f"index: {summary['index']}")
    print(f"RSS after load {summary['rss_mb_after_load']} MB, peak {summary['rss_mb_peak']} MB; "
          f"latency {summary['latency_ms']}")
    print(f"\n{'area':28} {'n':>3} {'hits':>5} {'rate':>6} {'median m':>9} {'p90 m':>8}")
    for a, s in by_area.items():
        print(f"{AREA_LABEL.get(a, a):28} {s['n']:3} {s['hits']:5} {s['hit_rate'] or 0:6.1%} "
              f"{s['median_err_m'] if s['median_err_m'] is not None else '-':>9} {s['p90_err_m'] or '-':>8}")
    s = summary["all"]
    print(f"{'ALL':28} {s['n']:3} {s['hits']:5} {s['hit_rate'] or 0:6.1%} {s['median_err_m']:>9} {s['p90_err_m']:>8}")
    print(f"\nby kind: " + ", ".join(f"{k} {v['hits']}/{v['n']} (median {v['median_err_m']} m)"
                                   for k, v in by_kind.items()))
    print(f"by file: " + ", ".join(f"{k} {v['hits']}/{v['n']}" for k, v in by_file.items()))
    print(f"intersection median error (hits): {summary['intersection_median_err_m']} m")
    if verbose or any(x["verdict"] in ("miss", "no_result") for x in res):
        print()
        for x in res:
            if verbose or x["verdict"] in ("miss", "no_result"):
                o = x.get("result") or {}
                e = x.get("error_m")
                print(f"{x['id']:5} {x['verdict']:18} {'' if e is None else f'{e:7.0f}m':8} "
                      f"{str(o.get('source', ''))[:28]:28} {o.get('confidence', ''):6} | {x['phrase'][:42]:42} "
                      f"-> {str(o.get('formatted', ''))[:48]}")
    if out_json:
        Path(out_json).write_text(json.dumps({"summary": summary, "results": res}, indent=1, default=float))
    return summary


# ── truth (TIGER, independent of OSM) ──────────────────────────────────────

def _read_lines(zips: list[Path]) -> dict:
    """TIGER lines grouped by core key: core -> [(full key, [(lat, lon)])]."""
    import shapefile

    from geocode.local.normalize import core_key, full_key
    out: dict = defaultdict(list)
    keys: dict = {}
    for z in zips:
        with shapefile.Reader(str(z)) as r:
            for sr in r.iterShapeRecords():
                fn = sr.record.as_dict().get("FULLNAME")
                if fn and sr.shape.points:
                    if fn not in keys:
                        keys[fn] = (core_key(fn), full_key(fn))
                    ck, fk = keys[fn]
                    out[ck].append((fk, [(y, x) for x, y in sr.shape.points]))
    return out


def _segs_for(lines: dict, name: str):
    from geocode.local.normalize import core_key, full_key
    ck, fk = core_key(name), full_key(name)
    segs = lines.get(ck, [])
    exact = [pts for f, pts in segs if fk and f == fk]
    return exact or [pts for _f, pts in segs]


def tiger_crossing(lines, a: str, b: str, near, approach_m: float | None = None):
    """Shared vertex of streets a and b in TIGER (or closest approach within approach_m)."""
    import numpy as np
    from scipy.spatial import cKDTree
    sa, sb = _segs_for(lines, a), _segs_for(lines, b)
    if not sa or not sb:
        return None
    hits = []
    if approach_m is None:
        va = {(round(p[0], 6), round(p[1], 6)) for pts in sa for p in pts}
        hits = [(round(p[0], 6), round(p[1], 6)) for pts in sb for p in pts
                if (round(p[0], 6), round(p[1], 6)) in va]
    if not hits:
        m = 111320 * math.cos(math.radians(42.4))
        pa = np.array([p for pts in sa for p in pts])
        pb = np.array([p for pts in sb for p in pts])
        d, j = cKDTree(np.c_[pb[:, 1] * m, pb[:, 0] * 110540]).query(np.c_[pa[:, 1] * m, pa[:, 0] * 110540],
                                                                      distance_upper_bound=approach_m or 30)
        hits = [((pa[i][0] + pb[j[i]][0]) / 2, (pa[i][1] + pb[j[i]][1]) / 2) for i in range(len(pa))
                if np.isfinite(d[i])]
    if not hits:
        return None
    clusters: list[list] = []
    for h in set(hits):
        for c in clusters:
            if hav(h, c[0]) < 300:
                c.append(h)
                break
        else:
            clusters.append([h])
    cents = sorted(((sum(p[0] for p in c) / len(c), sum(p[1] for p in c) / len(c)) for c in clusters),
                   key=lambda c: hav(c, near))
    return cents[0] if hav(cents[0], near) < 25000 else None


def tiger_addr(segs: dict, number: int, street: str, near, block: bool):
    from geocode.local.index import along
    from geocode.local.normalize import core_key, full_key
    target = number + 50 if block else number
    fk = full_key(street)
    best = None
    for frm, to, _p, coords, _z, fn, _s in segs.get(core_key(street), []):
        if fk and full_key(fn) != fk:
            continue
        lo, hi = min(frm, to), max(frm, to)
        if lo <= target <= hi:
            f = 0.5 if hi == lo else (target - frm) / (to - frm)
            pt = along(coords, f)
            if best is None or hav(pt, near) < hav(best, near):
                best = pt
    return best if best and hav(best, near) < 25000 else None


def tiger_arealm(zip_path: Path, name: str, near):
    import shapefile
    from shapely.geometry import shape
    want = name.lower()
    best = None
    with shapefile.Reader(str(zip_path)) as r:
        for sr in r.iterShapeRecords():
            fn = (sr.record.as_dict().get("FULLNAME") or "").lower()
            if fn == want:
                g = shape(sr.shape.__geo_interface__)
                c = g.centroid if g.contains(g.centroid) else g.representative_point()
                pt = (c.y, c.x)
                size_m = math.sqrt(g.area) * 111000 * 0.85
                if best is None or hav(pt, near) < hav(best[0], near):
                    best = (pt, size_m)
    return best if best and hav(best[0], near) < 25000 else None


def truth(paths: list[Path], data_dir: Path) -> None:
    tdir = data_dir / "downloads" / "tiger"
    roads = sorted(tdir.glob("tl_*_roads.zip"))
    addrfeat = sorted(tdir.glob("tl_*_addrfeat.zip"))
    arealm = next(iter(sorted(tdir.glob("tl_*_26_arealm.zip"))), None)
    if not roads:
        sys.exit(f"no TIGER roads in {tdir}: run build_geocoder_index.py --eval-data --download-only")
    lines = _read_lines(roads)
    from geocode.local.build_addr import tiger_ranges
    ranges, _ = tiger_ranges(addrfeat, log=lambda s: None)
    for p in paths:
        data = json.loads(p.read_text())
        for r in data["phrases"]:
            spec = r.get("truth_spec")
            if not spec:
                continue
            near = tuple(r["bias"])
            t, src, tol, note = None, None, None, None
            if spec[0] in ("troads", "troads_hwy"):
                hw = spec[0] == "troads_hwy"
                t = None if hw else tiger_crossing(lines, spec[1], spec[2], near)
                how = "shared vertex"
                if t is None:
                    t = tiger_crossing(lines, spec[1], spec[2], near, approach_m=60)
                    how = "closest approach <60 m"
                src, tol = "tiger_roads_crossing", (250 if hw else 150)
                note = f"TIGER {data.get('tiger_year', 2026)} all-roads: {spec[1]} x {spec[2]} ({how})"
            elif spec[0] == "tiger_addr":
                t = tiger_addr(ranges, int(spec[1]), spec[2], near, bool(spec[3]))
                src, tol = "tiger_addr_interp", 200
                note = f"TIGER ADDRFEAT interpolation of {spec[1]}{' block' if spec[3] else ''} {spec[2]}"
            elif spec[0] == "arealm" and arealm:
                hit = tiger_arealm(arealm, spec[1], near)
                if hit:
                    t, size = hit
                    src, tol = "tiger_arealm", int(max(400, min(1500, size)))
                    note = f"TIGER area landmark '{spec[1]}' centroid (feature ~{size:.0f} m across)"
            elif spec[0] == "k":
                t, src, tol, note = (spec[1], spec[2]), "knowledge", spec[3], "approximate public coordinate"
            if t is None:
                print(f"  no truth for {r['id']} {r['phrase']!r} ({spec})")
                r.update({"truth": None, "truth_source": None, "tolerance_m": None, "truth_note": None})
                continue
            r.update({"truth": {"lat": round(t[0], 6), "lon": round(t[1], 6)}, "truth_source": src,
                      "tolerance_m": tol, "truth_note": note})
        p.write_text(json.dumps(data, indent=1) + "\n")
        print(f"{p.name}: {sum(1 for r in data['phrases'] if r.get('truth'))}/{len(data['phrases'])} with truth")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "truth"])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--index", type=Path, default=None, help="index dir (default GEOCODER_LOCAL_INDEX)")
    ap.add_argument("--data-dir", type=Path, default=None, help="data dir for `truth` (default GEOCODER_DATA_DIR)")
    ap.add_argument("--json", type=Path, default=None, help="write per-phrase results here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    from core.settings import get_settings
    s = get_settings()
    files = a.files or sorted(EVAL_DIR.glob("*.json"))
    if a.cmd == "truth":
        truth(files, (a.data_dir or s.geocoder_data_dir).expanduser())
    else:
        run((a.index or s.geocoder_index_dir).expanduser(), files, a.json, a.verbose)


if __name__ == "__main__":
    main()
