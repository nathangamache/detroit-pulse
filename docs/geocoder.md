# Local geocoder ($0, no network at lookup time)

`geocode/local/` resolves the extractor's structured location (intersection, block, address,
freeway crossing, landmark) against an index built from free public data. It is the first
provider in the chain (`GEOCODER_PROVIDERS=local,census,photon`); Census and Photon only see
what it can't answer. It is a port of the 2026-11-13 free-geocoder prototype, extended to
Washtenaw County and Oakland County address points.

## Build the index

```bash
nice -n 19 uv run python scripts/build_geocoder_index.py            # download + build
nice -n 19 uv run python scripts/build_geocoder_index.py --eval-data # + TIGER roads/landmarks for the eval
```

| | default | override |
|---|---|---|
| data directory | `$XDG_DATA_HOME/detroit-pulse/geocoder` = `~/.local/share/detroit-pulse/geocoder` | `--data-dir`, `GEOCODER_DATA_DIR` |
| index (what the worker loads) | `<data dir>/index` | `GEOCODER_LOCAL_INDEX` |
| area | `coverage_bbox` in `config/areas(.local).yaml`: Wayne + Oakland + Washtenaw + Macomb, `42.01,-84.16,42.92,-82.68` | `--bbox S,W,N,E` |
| TIGER counties | 26163 Wayne, 26125 Oakland, 26161 Washtenaw, 26099 Macomb | `--counties` |
| address-point layers | `detroit,oakland` | `--points` (add `annarbor` only after reading its licence note below) |
| OSM decoder threads | 4 | `--threads` |

The script refuses a data directory inside the repo, so nothing it downloads or builds can be
committed. Downloads younger than `--max-age-days` (25) are reused; `--refresh` re-fetches. A
build writes `index.new/` next to the live index and swaps it in; restart the extract worker
(or the `pulse-extract` container) to load the new one. Rebuild monthly.

Measured on the host (2026-11-14, `nice -n 19`, 4 decoder threads):

| | |
|---|---|
| full build, sources already downloaded except a fresh OSM extract | 3 min 29 s wall (OSM download 26 s, pass 1 52 s, pass 2 45 s, derive 46 s, address layer 33 s) |
| first build ever (adds the paged ArcGIS downloads) | + ~2.5 min Detroit points, + ~2.5 min Oakland points |
| build peak RSS | 2.85 GB (during the OSM parse) |
| downloads | 344 MB (OSM 318 MB, TIGER 18 MB, points 9 MB gz) |
| index | 44 MB (`osm.pkl.gz` 26 MB, `addr.pkl.gz` 19 MB) |
| loaded in the worker | 2.9 s, 563 MB RSS |
| lookups | median 0.26 ms; landmarks up to ~250 ms |

What it contains: 149k named OSM road ways (43k street keys, 119k intersection pairs), 75k POI
names, 197 city/township polygons, 1.09M unique address points (Detroit 486k, Oakland 560k,
OSM 196k rows) and 374k TIGER range sides.

## How lookups work

* **Names** (`normalize.py`): case, punctuation, `&`, directionals and street types are
  dropped into a *core key* (`W 7 Mile Rd` and `Seven Mile` -> `7 mile`); spoken numbers and
  ordinals become digits; `St`/`Mt` at the start mean Saint/Mount; freeway names map to route
  numbers (Lodge M-10, Southfield Fwy M-39, Jeffries I-96, Fisher/Chrysler I-75, Ford I-94,
  Davison M-8, Reuther I-696); corridor aliases (6 Mile = McNichols, 15 Mile = Maple,
  16 Mile = Big Beaver/Metropolitan, 17 Mile = Wattles, 18 Mile = Long Lake, 19 Mile =
  Square Lake, M-1 = Woodward, ...); common Whisper spellings (Grashit, Livernoise, Shoenherr,
  Washtenah). Misspellings beyond the table fall back to fuzzy matching (rapidfuzz, ratio >= 80;
  never HIGH confidence).
* **Street types matter when two roads share a core key.** Plymouth has Ann Arbor *Trail* and
  Ann Arbor *Road*; Ann Arbor has Huron *Street* and Huron *Parkway*. The builder finds every
  core key used with two or more types (6,987 of them) and records the typed keys present at
  each intersection; a query that names the type ("Main and Ann Arbor Road") only matches
  those. Without a type, the crossing nearest the channel's area wins.
* **Intersections**: nodes the two streets share in OSM, clustered within 250 m (divided roads
  collapse to one point); otherwise their closest approach within 60 m (120 m for freeway
  crossings, which are grade separated). Duplicates are picked by named city (OSM boundary
  polygons), then exact-over-fuzzy, then distance to the channel's bias point. More than 60 km
  from the bias with no matching city -> no answer.
* **Business routes** (`US 23 BR`, `BL I-94`) are not indexed as the route itself, so "US-23 at
  Washtenaw" finds the freeway interchange, not Washtenaw Avenue.
* **House numbers**: candidates from every layer compete. Official points (exact number, or
  the mean of N..N+99 for a block, snapped onto the street centreline because parcel centres
  can sit 200 m back), OSM `addr:*` points, interpolation between neighbouring points on the
  same side, and TIGER range interpolation. City match first, then everything within 5 km of
  the nearest candidate, then layer quality. An unknown number never invents a house: it
  returns the street's nearest point to the channel (LOW).
* **Landmarks**: curated aliases (`index.py: LANDMARK_ALIASES`, plus `geocode/aliases.py` before
  the chain), then OSM POI names scored by weighted token coverage (spoken abbreviations like
  "Tech" match "Technological"); bus stops, parking and rentals never answer; spoken cross
  streets anchor the search to 1.5 km. Detroit precinct numbers have their own pattern.
* **Confidence**: HIGH = shared-node intersection, official/OSM address point, block mean;
  MEDIUM = closest approach, interpolation, fuzzy name, unmatched landmark at its cross streets;
  LOW = street point. The chain stops at the first MEDIUM-or-better answer.

## Sources and licences

| Source | Used for | Licence / terms | Status |
|---|---|---|---|
| OpenStreetMap, Geofabrik Michigan extract (`download.geofabrik.de/north-america/us/michigan-latest.osm.pbf`, md5 checked) | streets, intersections, POIs, city polygons, `addr:*` points | **ODbL 1.0**. Attribution "© OpenStreetMap contributors" is required wherever results are shown (the Pulse map now carries it). The index is a derivative database: keep it private (it is never committed or served), or publish it under the ODbL. | used |
| US Census TIGER/Line 2026 ADDRFEAT (`www2.census.gov/geo/tiger/TIGER2026/ADDRFEAT/`) | address ranges, 4 counties | US government work, public domain | used |
| TIGER/Line 2026 ROADS, AREALM | eval ground truth only | public domain | `--eval-data` |
| City of Detroit Base Units: Addresses (ArcGIS item `2b9ab7687289457c9793a4a92d7c4eb9`, layer 0) | Detroit address points | No licence on the City item; OpenAddresses lists it as public domain, no attribution | used |
| Oakland County "OC Site Address" (item `32a0c4826b8f44bd9a33712320626ef3`) | Oakland address points (parcel centres) | Oakland County Open Data Terms of Use: "You may retrieve, download, copy, modify, translate, adapt, distribute, sort, search, and reuse Open Data for any lawful purpose." As-is disclaimer and an indemnification clause; downloading accepts the terms. | used |
| City of Ann Arbor Address Points (`ckan.a2gov.org`, dataset `address-points`) | would add Ann Arbor points | a2gov.org terms: City data is public domain unless stated, **but** anyone who modifies, uses or presents it "in a manner other than originally presented" must notify the City IT Director (734.794.6550) and include the City's modified-data disclaimer. | **off**; enable with `--points detroit,oakland,annarbor` after giving that notice |
| Washtenaw County GIS | — | Open-data portal answers 401 (private ArcGIS org); the old ParcelPublish service is gone (404); no licence found | not used: TIGER + OSM |
| Wayne County parcels | — | No licence statement; the layer carries owner names | not used: TIGER + OSM |
| SEMCOG buildings/addresses | — | "Reproduction or Use Without Permission is Prohibited" | not used |
| OpenAddresses mirrors | — | downloads need a login (HTTP 403) | not used (source files read for licence metadata only) |

The same table is written into every index's `manifest.json` (`sources`).

## Evaluation

```bash
uv run python scripts/eval_geocoder.py            # all files in scripts/geocoder_eval/
uv run python scripts/eval_geocoder.py truth scripts/geocoder_eval/phrases_suburbs.json   # recompute TIGER truth
```

* `phrases_metro.json`: the prototype's 80 phrases (74 scored; 6 have no truth). All public:
  generic scanner-style Detroit intersections and landmarks, plus phrases that were already
  public in this repo's initial commit (`afb9a39`). Checked against the gitignored few-shot file:
  no phrase or street pair comes from it.
* `phrases_suburbs.json`: 31 new public places in Plymouth/Northville/Canton, Ferndale/Royal Oak/
  Southfield and Ann Arbor (major intersections, freeway crossings, main-street blocks, public
  landmarks), with truth from TIGER/Line (independent of OSM), except two landmarks whose truth
  is an approximate public coordinate.

Results on the 2026-11-14 index (hit = within the phrase's tolerance, 150 m for intersections):

| area | scored | hits | median error | p90 |
|---|---|---|---|---|
| Detroit | 58 | 58 | 10.0 m | 132 m |
| Plymouth / Northville / Canton | 16 | 16 | 1.8 m | 57 m |
| other Wayne (Dearborn, Taylor, Wayne) | 4 | 4 | 76 m | 132 m |
| Oakland | 16 | 16 | 8.1 m | 131 m |
| Ann Arbor / Washtenaw | 11 | 11 | 5.4 m | 41 m |
| **all** | **105** | **105** | 5.4 m | 132 m |

By kind: intersections 57/57 (median 1.5 m), freeway crossings 13/13 (24 m), addresses 5/5,
blocks 8/8 (32 m: block points are snapped onto the street, Detroit's truth is the building
mean), landmarks 22/22 (119 m). Caveats:

* The first run of this code scored 99/105. Four generic fixes came from those misses
  (business-route refs, abbreviated landmark words, a University of Michigan hospital alias,
  snapping blocks to the street), so these numbers are partly tuned. The prototype's 28
  held-out landmarks (not in either file) score 22/28, the same as the prototype.
* One prototype truth was wrong and is corrected in the file (`s03`, "Sheldon and Ann Arbor
  Road"): the prototype matched TIGER by core key, which merges Ann Arbor Trail with Ann Arbor
  Road, and took the Trail crossing 1.45 km north. The prototype geocoder made the same merge,
  which is why it "hit".
* Detroit addresses are scored against the City points the index also uses (circular); suburban
  blocks are scored against TIGER interpolation, which is independent where the Oakland points
  answered and circular where TIGER did.
* All of this assumes the extractor split the location correctly; real-world accuracy is
  bounded by Whisper and the extraction step.

## Operations

* The extract worker loads the index on its first lookup (~560 MB). Only that process geocodes.
* In Docker the index directory is bind-mounted read-only and `GEOCODER_LOCAL_INDEX` points at it
  (`deploy/docker-compose.yml`).
* Index files are gzip'd pickles read with an allowlist unpickler (containers and numpy arrays
  only), so a tampered file can't execute code; still, only load indexes you built.
* If the index is missing, the provider logs `local geocoder index unavailable` (and says so in
  `Geocoder.status()`), retries every 10 minutes, and the chain falls through to Census/Photon.
