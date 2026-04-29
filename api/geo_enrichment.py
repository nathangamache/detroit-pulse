import json
import math
import structlog
from pathlib import Path

log      = structlog.get_logger()
DATA_DIR = Path(__file__).parent / "data"


# ── Load GeoJSON files once at module import ──────────────────────────────

def _load(filename: str) -> list[dict]:
    path = DATA_DIR / filename
    if not path.exists():
        log.error("GeoJSON file not found", path=str(path))
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("features", [])


_BATTALIONS = _load("DFD_Battalions.geojson")
_PRECINCTS  = _load("DPD_Precincts.geojson")
_STATIONS   = _load("DFD_Fire_Station_Locations.geojson")

log.info("Geo enrichment data loaded",
         battalions = len(_BATTALIONS),
         precincts  = len(_PRECINCTS),
         stations   = len(_STATIONS))


# ── Point-in-polygon ──────────────────────────────────────────────────────

def _point_in_polygon(lng: float, lat: float,
                       ring: list) -> bool:
    """Ray casting algorithm. Coordinates are [lng, lat] GeoJSON order."""
    x, y   = lng, lat
    inside = False
    n      = len(ring)
    j      = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_feature(lng: float, lat: float, feature: dict) -> bool:
    """Test if point is inside a Polygon or MultiPolygon feature."""
    geom = feature.get("geometry", {})
    if not geom:
        return False
    gtype  = geom.get("type")
    coords = geom.get("coordinates", [])

    if gtype == "Polygon":
        return _point_in_polygon(lng, lat, coords[0])
    elif gtype == "MultiPolygon":
        return any(_point_in_polygon(lng, lat, poly[0]) for poly in coords)
    return False


# ── Haversine distance ────────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float,
                   lat2: float, lng2: float) -> float:
    """Distance in km between two lat/lng coordinates."""
    R    = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a    = (math.sin(dlat / 2) ** 2 +
            math.cos(math.radians(lat1)) *
            math.cos(math.radians(lat2)) *
            math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── Public API — point-in-polygon lookups ────────────────────────────────

def get_precinct(lat: float, lng: float):
    """
    Return the DPD precinct number (e.g. '09') for a coordinate,
    or None if outside all precincts.
    """
    if lat is None or lng is None:
        return None
    for feat in _PRECINCTS:
        if _in_feature(lng, lat, feat):
            return feat["properties"].get("name")
    return None


def get_battalion(lat: float, lng: float):
    """
    Return the DFD battalion number (e.g. '04') for a coordinate,
    or None if outside all battalions.
    """
    if lat is None or lng is None:
        return None
    for feat in _BATTALIONS:
        if _in_feature(lng, lat, feat):
            return feat["properties"].get("Battalion")
    return None


def get_nearest_stations(lat: float, lng: float, n: int = 3) -> list:
    """
    Return the N nearest fire stations to a coordinate, sorted by distance.
    Each result includes name, address, battalion, and distance_km.
    """
    if lat is None or lng is None:
        return []

    results = []
    for feat in _STATIONS:
        coords = feat["geometry"]["coordinates"]
        slng, slat = coords[0], coords[1]
        dist = _haversine_km(lat, lng, slat, slng)
        p    = feat["properties"]
        results.append({
            "name":        p.get("Firehouse") or "",
            "short":       p.get("FH_Short") or "",
            "address":     p.get("Address", ""),
            "battalion":   p.get("Battalion", ""),
            "lat":         slat,
            "lng":         slng,
            "distance_km": round(dist, 2),
        })

    return sorted(results, key=lambda x: x["distance_km"])[:n]


def enrich_incident(incident: dict) -> dict:
    """
    Add precinct, battalion, and nearest stations to an incident dict.
    Returns a new dict with enrichment fields added — non-destructive.
    Silently skips if no coordinates are available.

    Nearest stations only computed for fire-related incident types.
    """
    lat = incident.get("lat")
    lng = incident.get("lng")

    if not lat or not lng:
        return incident

    enriched = dict(incident)

    precinct = get_precinct(lat, lng)
    if precinct:
        enriched["precinct"] = precinct

    battalion = get_battalion(lat, lng)
    if battalion:
        enriched["battalion"] = battalion

    inc_type = incident.get("incident_type", "")
    if inc_type in ("STRUCTURE_FIRE", "VEHICLE_FIRE", "HAZMAT"):
        stations = get_nearest_stations(lat, lng, n=3)
        if stations:
            enriched["nearest_stations"] = stations

    return enriched


# ── Unit-to-station index ─────────────────────────────────────────────────

_ADDRESS_INDICATORS = {
    "avenue", "ave", "street", "st", "road", "rd", "boulevard", "blvd",
    "drive", "dr", "lane", "ln", "court", "ct", "place", "pl", "way",
    "highway", "hwy", "expressway", "freeway",
    "dexter", "nashville", "gratiot", "mound", "woodward", "jefferson",
    "michigan", "warren", "mcnichols", "fenkell", "joy", "schoolcraft",
    "plymouth", "telegraph", "livernois", "wyoming", "greenfield",
    "evergreen", "southfield", "lahser", "inkster", "middlebelt",
    "merriman", "newburgh", "beech", "haggerty", "napier", "sheldon",
    "beck", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen",
}


def _build_unit_index() -> dict:
    index = {}

    for feat in _STATIONS:
        p    = feat["properties"]
        c    = feat["geometry"]["coordinates"]
        slng = c[0]
        slat = c[1]

        # Guard against null fields — one station record has all nulls
        raw_name = (p.get("Firehouse") or "").upper().strip()
        short    = (p.get("FH_Short")  or "").upper().strip()

        # Skip entirely invalid records (no coordinates or name)
        if not raw_name or slat is None or slng is None:
            continue

        station = {
            "name":      raw_name,
            "short":     short,
            "address":   p.get("Address") or "",
            "battalion": p.get("Battalion") or "",
            "lat":       slat,
            "lng":       slng,
        }

        variants = set()

        if raw_name:
            variants.add(raw_name)
            variants.add(raw_name.replace(" ", ""))

        if short:
            variants.add(short)
            variants.add(short.replace("-", ""))
            variants.add(short.replace("-", " "))

        if raw_name.startswith("ENGINE "):
            num = raw_name[7:]
            variants.update({
                f"ENG {num}", f"ENG{num}", f"ENGINE{num}",
            })

        if raw_name.startswith("LADDER "):
            num = raw_name[7:]
            variants.update({
                f"LAD {num}", f"LAD{num}", f"LADDER{num}",
                f"L {num}",   f"L{num}",
            })

        if raw_name == "FIREBOAT":
            variants.update({"FB", "FIRE BOAT"})

        for v in variants:
            if not v:
                continue
            v_lower = v.lower()
            if any(ind in v_lower for ind in _ADDRESS_INDICATORS):
                continue
            index[v] = station

    return index


_UNIT_STATION_INDEX: dict = _build_unit_index()

log.info("Unit-to-station index built", entries=len(_UNIT_STATION_INDEX))


def get_station_for_unit(unit_id: str):
    """
    Look up the home station for a unit ID as it would appear in
    dispatch audio transcription. Case-insensitive.
    Returns station dict or None if not a known DFD unit.
    """
    if not unit_id:
        return None
    return _UNIT_STATION_INDEX.get(unit_id.upper().strip())


# ── Unit-location validation ──────────────────────────────────────────────

def validate_unit_location(
    unit_ids:        list,
    incident_lat,
    incident_lng,
    max_distance_km: float = 15.0,
) -> dict:
    """
    Validate that DFD units are plausibly located near the incident address.

    Normal response range:  <8km   -> plausible (routine call)
    Possible mutual aid:    8-15km -> valid but flagged
    Likely geocoding error: >15km  -> invalid, coordinates should be dropped

    Returns:
    {
        "valid":            bool,
        "plausible":        bool,
        "nearest_station":  dict | None,
        "min_distance_km":  float | None,
        "inferred_area":    str | None,   battalion number
        "warning":          str | None,
    }
    """
    matched = []
    for uid in unit_ids:
        station = get_station_for_unit(uid)
        if station:
            matched.append((uid, station))

    if not matched:
        return {
            "valid":           True,
            "plausible":       True,
            "nearest_station": None,
            "min_distance_km": None,
            "inferred_area":   None,
            "warning":         None,
        }

    if incident_lat is None or incident_lng is None:
        battalions = [s["battalion"] for _, s in matched]
        inferred   = max(set(battalions), key=battalions.count)
        return {
            "valid":           True,
            "plausible":       True,
            "nearest_station": matched[0][1],
            "min_distance_km": None,
            "inferred_area":   inferred,
            "warning":         None,
        }

    distances = [
        (_haversine_km(incident_lat, incident_lng,
                       station["lat"], station["lng"]), uid, station)
        for uid, station in matched
    ]
    distances.sort(key=lambda x: x[0])
    min_dist, closest_uid, closest_station = distances[0]

    plausible = min_dist <= 8.0
    valid     = min_dist <= max_distance_km

    if not valid:
        warning = (
            f"{closest_uid} home station is {min_dist:.1f}km from geocoded "
            f"address — likely geocoding error or wrong address extracted"
        )
    elif not plausible:
        warning = (
            f"{closest_uid} home station is {min_dist:.1f}km from incident "
            f"— possible mutual aid response or major incident"
        )
    else:
        warning = None

    return {
        "valid":           valid,
        "plausible":       plausible,
        "nearest_station": closest_station,
        "min_distance_km": round(min_dist, 2),
        "inferred_area":   closest_station["battalion"],
        "warning":         warning,
    }


def infer_location_from_units(unit_ids: list):
    """
    Infer a rough geographic area from responding units' home stations
    when geocoding has failed entirely.

    Returns dict with lat/lng centroid, battalion, confidence=LOW,
    and the matched station names. Returns None if no DFD units matched.

    IMPORTANT: Always LOW confidence. Use only for proximity search
    and initial map placement — never as a definitive address.
    The UI should show the original transcript address, not the
    inferred station coordinates.
    """
    matched = []
    for uid in unit_ids:
        station = get_station_for_unit(uid)
        if station:
            matched.append(station)

    if not matched:
        return None

    avg_lat    = sum(s["lat"] for s in matched) / len(matched)
    avg_lng    = sum(s["lng"] for s in matched) / len(matched)
    battalions = [s["battalion"] for s in matched]
    battalion  = max(set(battalions), key=battalions.count)

    return {
        "lat":        avg_lat,
        "lng":        avg_lng,
        "battalion":  battalion,
        "confidence": "LOW",
        "source":     "unit_inference",
        "stations":   [s["name"] for s in matched],
    }