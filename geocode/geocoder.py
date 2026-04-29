import os
import re
import time
import threading
import urllib.request
import urllib.parse
import json
import structlog
import googlemaps
from dotenv import load_dotenv
from geocode.cache import get as cache_get, set as cache_set

load_dotenv()
log = structlog.get_logger()

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

BBOX = {
    "south": float(os.getenv("GEOCODE_BBOX_SOUTH", "41.9")),
    "north": float(os.getenv("GEOCODE_BBOX_NORTH", "43.0")),
    "west":  float(os.getenv("GEOCODE_BBOX_WEST",  "-84.1")),
    "east":  float(os.getenv("GEOCODE_BBOX_EAST",  "-82.5")),
}

PHOTON_URL = "https://photon.komoot.io/api/"

# Fix #5 — per-feed bias points for Photon
FEED_BIAS_POINTS = {
    "wayneco_detroit_police_fire":      (42.33, -83.05),
    "wayneco_detroit_police_dispatch":  (42.33, -83.05),
    "wayneco_detroit_fire":             (42.33, -83.05),
    "wayneco_detroit_ems":              (42.33, -83.05),
    "wayneco_downriver":                (42.19, -83.18),
    "wayneco_westland_gardencity":      (42.32, -83.40),
    "wayneco_dearborn":                 (42.32, -83.18),
    "wayneco_grossepointe":             (42.39, -82.92),
    "wayneco_plymouthnorthville":       (42.37, -83.47),
    "wayneco_southwestern":             (42.24, -83.52),
    "wayneco_public_safety":            (42.33, -83.20),
    "wayneco_romulus":                  (42.22, -83.37),
    "wayneco_northville_plymouth_city": (42.43, -83.48),
    "wayneco_franklin_bingham":         (42.52, -83.30),
    "oaklandco_royaloak_fire":          (42.49, -83.14),
    "washtenaw_metro":                  (42.28, -83.74),
    "washtenaw_livingston":             (42.60, -83.93),
}
PHOTON_BIAS_DEFAULT = (42.33, -83.05)

# Idea 1 — primary city/area for each feed used to validate Photon results
# and retry with city appended when result lands in wrong area
FEED_PRIMARY_CITY = {
    "wayneco_detroit_police_fire":      "Detroit",
    "wayneco_detroit_police_dispatch":  "Detroit",
    "wayneco_detroit_fire":             "Detroit",
    "wayneco_detroit_ems":              "Detroit",
    "wayneco_downriver":                None,  # multi-city, no single default
    "wayneco_westland_gardencity":      "Westland",
    "wayneco_dearborn":                 "Dearborn",
    "wayneco_grossepointe":             "Grosse Pointe",
    "wayneco_plymouthnorthville":       "Plymouth",
    "wayneco_southwestern":             "Van Buren Township",
    "wayneco_public_safety":            None,
    "wayneco_romulus":                  "Romulus",
    "wayneco_northville_plymouth_city": "Northville",
    "wayneco_franklin_bingham":         "Franklin",
    "oaklandco_royaloak_fire":          "Royal Oak",
    "washtenaw_metro":                  "Ann Arbor",
    "washtenaw_livingston":             "Howell",
}

# Idea 1 — radius (km) within which a Photon result is considered
# plausible for the feed's primary area. Results outside this radius
# from the feed's bias point trigger a city-qualified retry.
FEED_PLAUSIBLE_RADIUS_KM = {
    "wayneco_detroit_police_fire":      15.0,
    "wayneco_detroit_police_dispatch":  15.0,
    "wayneco_detroit_fire":             15.0,
    "wayneco_detroit_ems":              15.0,
    "wayneco_downriver":                20.0,
    "wayneco_westland_gardencity":      10.0,
    "wayneco_dearborn":                 10.0,
    "wayneco_grossepointe":             10.0,
    "wayneco_plymouthnorthville":       15.0,
    "wayneco_southwestern":             15.0,
    "wayneco_public_safety":            40.0,
    "wayneco_romulus":                  12.0,
    "wayneco_northville_plymouth_city": 12.0,
    "wayneco_franklin_bingham":         10.0,
    "oaklandco_royaloak_fire":          10.0,
    "washtenaw_metro":                  25.0,
    "washtenaw_livingston":             25.0,
}

# Fix #8 — token bucket rate limiter for Google API
_google_rate_lock   = threading.Lock()
_google_tokens      = 5.0
_google_max_tokens  = 5.0
_google_refill_rate = 0.5        # 1 call per 2 seconds sustained
_google_last_refill = time.monotonic()
_google_total_calls = 0


def _consume_google_token() -> bool:
    global _google_tokens, _google_last_refill, _google_total_calls
    with _google_rate_lock:
        now     = time.monotonic()
        elapsed = now - _google_last_refill
        _google_tokens = min(
            _google_max_tokens,
            _google_tokens + elapsed * _google_refill_rate,
        )
        _google_last_refill = now
        if _google_tokens >= 1.0:
            _google_tokens      -= 1.0
            _google_total_calls += 1
            return True
        log.warning("Google geocoding rate limited",
                    total_calls=_google_total_calls)
        return False


_gmaps: googlemaps.Client | None = None


def get_gmaps() -> googlemaps.Client | None:
    global _gmaps
    if _gmaps is None and GOOGLE_API_KEY:
        _gmaps = googlemaps.Client(key=GOOGLE_API_KEY)
    return _gmaps


GEOCODE_FAILURE = {
    "lat":        None,
    "lng":        None,
    "formatted":  None,
    "confidence": "FAILED",
    "source":     "none",
}


def _get_photon_bias(feed_id: str | None) -> tuple[float, float]:
    if feed_id and feed_id in FEED_BIAS_POINTS:
        return FEED_BIAS_POINTS[feed_id]
    return PHOTON_BIAS_DEFAULT


def _photon_result_in_feed_area(
    lat: float, lng: float, feed_id: str | None
) -> bool:
    """
    Idea 1 — check whether a Photon result falls within the plausible
    geographic area for the given feed. Results outside this radius
    are likely wrong-city matches and should trigger a city-qualified retry.
    """
    import math
    if not feed_id:
        return True
    bias = FEED_BIAS_POINTS.get(feed_id, PHOTON_BIAS_DEFAULT)
    radius = FEED_PLAUSIBLE_RADIUS_KM.get(feed_id, 40.0)
    bias_lat, bias_lng = bias
    dlat = math.radians(lat - bias_lat)
    dlng = math.radians(lng - bias_lng)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(bias_lat)) *
         math.cos(math.radians(lat)) *
         math.sin(dlng/2)**2)
    dist_km = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return dist_km <= radius


def _photon_query(address: str, feed_id: str | None = None) -> dict | None:
    query    = address.replace(" and ", " & ")
    bias_lat, bias_lng = _get_photon_bias(feed_id)

    params = urllib.parse.urlencode({
        "q":     query,
        "lat":   bias_lat,
        "lon":   bias_lng,
        "limit": 1,
        "lang":  "en",
    })

    try:
        req = urllib.request.Request(
            f"{PHOTON_URL}?{params}",
            headers={"User-Agent": "detroit-pulse/1.0 (geocoder)"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())

        features = data.get("features", [])
        if not features:
            return None

        f   = features[0]
        p   = f["properties"]
        c   = f["geometry"]["coordinates"]
        lng = c[0]
        lat = c[1]

        if not (BBOX["south"] <= lat <= BBOX["north"] and
                BBOX["west"]  <= lng <= BBOX["east"]):
            log.debug("Photon result outside bbox", lat=lat, lng=lng)
            return None

        parts = []
        if p.get("housenumber") and p.get("street"):
            parts.append(f"{p['housenumber']} {p['street']}")
        elif p.get("street"):
            parts.append(p["street"])
        elif p.get("name"):
            parts.append(p["name"])

        city  = p.get("city") or p.get("town") or p.get("village") or ""
        state = p.get("state", "MI")
        if city:
            parts.append(city)
        parts.append(state)
        formatted = ", ".join(parts) if parts else address

        osm_type  = p.get("type", "")
        osm_value = p.get("osm_value", "")

        if osm_type == "house" or (p.get("housenumber") and p.get("street")):
            confidence = "HIGH"
        elif osm_type in ("street", "locality") or p.get("street"):
            confidence = "HIGH"
        elif osm_value in ("bus_stop", "crossing"):
            confidence = "HIGH"
        elif osm_type in ("district", "neighbourhood"):
            confidence = "MEDIUM"
        else:
            confidence = "MEDIUM"

        return {
            "lat":        lat,
            "lng":        lng,
            "formatted":  formatted,
            "confidence": confidence,
            "source":     "photon",
            "_city":      city,
        }

    except Exception as e:
        log.warning("Photon geocoding error", address=address[:60], error=str(e))
        return None


def _geocode_google(address: str) -> dict | None:
    if not _consume_google_token():
        return None
    gmaps = get_gmaps()
    if gmaps is None:
        return None
    try:
        results = gmaps.geocode(
            address,
            bounds={"southwest": (BBOX["south"], BBOX["west"]),
                    "northeast": (BBOX["north"], BBOX["east"])},
            region="us",
        )
        if not results:
            return None
        result   = results[0]
        loc      = result["geometry"]["location"]
        lat, lng = loc["lat"], loc["lng"]
        if not (BBOX["south"] <= lat <= BBOX["north"] and
                BBOX["west"]  <= lng <= BBOX["east"]):
            return None
        location_type  = result["geometry"].get("location_type", "")
        formatted      = result["formatted_address"]
        # Upgrade APPROXIMATE to MEDIUM when the query had a house number —
        # Google returns APPROXIMATE for real addresses it doesn't have
        # rooftop data for, not because the address is wrong.
        has_house_number = bool(re.match(r"^\d+\s", address.strip()))
        confidence = {"ROOFTOP": "HIGH", "RANGE_INTERPOLATED": "HIGH",
                      "GEOMETRIC_CENTER": "MEDIUM",
                      "APPROXIMATE": "MEDIUM" if has_house_number else "LOW",
                      }.get(location_type, "MEDIUM")
        return {"lat": lat, "lng": lng,
                "formatted": formatted,
                "confidence": confidence, "source": "google"}
    except Exception as e:
        log.error("Google geocoding error", address=address[:60], error=str(e))
        return None


def _geocode_google_place(address: str) -> dict | None:
    if not _consume_google_token():
        return None
    gmaps = get_gmaps()
    if gmaps is None:
        return None
    try:
        results = gmaps.geocode(
            address,
            bounds={"southwest": (BBOX["south"], BBOX["west"]),
                    "northeast": (BBOX["north"], BBOX["east"])},
            region="us",
        )
        if not results:
            return None
        result        = results[0]
        loc           = result["geometry"]["location"]
        lat, lng      = loc["lat"], loc["lng"]
        if not (BBOX["south"] <= lat <= BBOX["north"] and
                BBOX["west"]  <= lng <= BBOX["east"]):
            return None
        location_type = result["geometry"].get("location_type", "")
        result_types  = result.get("types", [])
        useless_types = {"locality", "administrative_area_level_1",
                         "administrative_area_level_2",
                         "administrative_area_level_3",
                         "country", "postal_code"}
        useful_types  = {"neighborhood", "sublocality", "sublocality_level_1",
                         "park", "establishment", "point_of_interest",
                         "premise", "natural_feature"}
        is_useful     = bool(set(result_types) & useful_types)
        is_city_level = (bool(result_types) and
                         set(result_types) <= useless_types | {"political"} and
                         not is_useful)
        formatted     = result["formatted_address"]
        has_no_street = "," not in formatted.split(",")[0]
        if is_city_level or (location_type == "APPROXIMATE" and
                             has_no_street and not is_useful):
            return None
        confidence = {"ROOFTOP": "HIGH", "RANGE_INTERPOLATED": "HIGH",
                      "GEOMETRIC_CENTER": "MEDIUM",
                      "APPROXIMATE": "MEDIUM" if is_useful else "LOW"
                      }.get(location_type, "MEDIUM")
        return {"lat": lat, "lng": lng, "formatted": formatted,
                "confidence": confidence, "source": "google_place"}
    except Exception as e:
        log.error("Google place geocoding error", address=address[:60], error=str(e))
        return None


PLACE_NAME_SUFFIXES = {
    "apartments", "apartment", "apt", "complex", "bend", "landing",
    "commons", "place", "park", "center", "centre", "plaza", "manor",
    "village", "crossing", "pointe", "ridge", "glen", "woods", "lakes",
    "club", "academy", "school", "hospital", "mall", "station", "tower",
    "towers", "court", "courts", "estates", "grove", "meadows", "heights",
    "terrace", "gardens", "green", "square", "field", "fields", "falls",
    "creek", "lake", "pond", "trail", "sanctuary", "reserve", "recreation",
    "arena", "stadium", "theater", "theatre", "church", "cathedral",
    "mosque", "temple", "synagogue", "university", "college", "institute",
    "library", "museum", "elementary", "middle", "high", "technical",
    "casino", "hotel", "motel", "inn", "suites", "lodge", "restaurant",
    "bar", "grill", "lounge", "warehouse", "factory", "plant", "facility",
    "garage", "lot", "ramp", "terminal", "shelter", "clinic",
    "lofts", "flats", "townhomes", "condos",
}


def _is_named_place(address: str) -> bool:
    if not address or address == "NO_LOCATION":
        return False
    parts = address.strip().split()
    if parts and parts[0].isdigit():
        return False
    lower = address.lower()
    if " and " in lower or " & " in lower:
        return False
    words = {w.strip(".,()").lower() for w in parts}
    return bool(words & PLACE_NAME_SUFFIXES)


def _strip_unit_suffix(address: str) -> str:
    cleaned = re.sub(
        r",?\s*(apartment|apt|unit|suite|ste|floor|fl|room|rm|#)\s*[\w-]+",
        "", address, flags=re.IGNORECASE,
    )
    return cleaned.strip().strip(",").strip()


def _photon_result_is_plausible_for_place(result: dict, address: str) -> bool:
    if result is None:
        return False
    city       = result.get("_city", "").lower()
    city_hints = []
    parts      = address.split(",")
    if len(parts) >= 2:
        hint = parts[-2].strip().lower()
        hint = hint.replace(" mi", "").replace(" oh", "").strip()
        if hint:
            city_hints.append(hint)
    if city_hints and city:
        for hint in city_hints:
            if hint in city or city in hint:
                return True
        log.debug("Photon city mismatch for named place",
                  expected=city_hints, got=city, address=address[:50])
        return False
    return True


DETROIT_LANDMARKS = {
    "mgm":                    "MGM Grand Detroit Casino, Detroit, MI",
    "mgm casino":             "MGM Grand Detroit Casino, Detroit, MI",
    "mgm grand":              "MGM Grand Detroit Casino, Detroit, MI",
    "greektown":              "Greektown Casino, Detroit, MI",
    "greektown casino":       "Greektown Casino, Detroit, MI",
    "motorcity casino":       "MotorCity Casino, Detroit, MI",
    "motor city casino":      "MotorCity Casino, Detroit, MI",
    "dmc":                    "Detroit Medical Center, Detroit, MI",
    "harper":                 "Harper University Hospital, Detroit, MI",
    "sinai":                  "Sinai-Grace Hospital, Detroit, MI",
    "grace":                  "Sinai-Grace Hospital, Detroit, MI",
    "beaumont":               "Beaumont Hospital, Royal Oak, MI",
    "providence":             "Providence Hospital, Southfield, MI",
    "henry ford":             "Henry Ford Hospital, Detroit, MI",
    "receiving":              "Detroit Receiving Hospital, Detroit, MI",
    "childrens":              "Children's Hospital of Michigan, Detroit, MI",
    "ford field":             "Ford Field, Detroit, MI",
    "little caesars arena":   "Little Caesars Arena, Detroit, MI",
    "lca":                    "Little Caesars Arena, Detroit, MI",
    "comerica":               "Comerica Park, Detroit, MI",
    "comerica park":          "Comerica Park, Detroit, MI",
    "metro airport":          "Detroit Metropolitan Wayne County Airport, Romulus, MI",
    "dtwatx":                 "Detroit Metropolitan Wayne County Airport, Romulus, MI",
    "dtw":                    "Detroit Metropolitan Wayne County Airport, Romulus, MI",
    "coleman young":          "Coleman A. Young Municipal Airport, Detroit, MI",
    "belle isle":             "Belle Isle Park, Detroit, MI",
    "chandler park":          "Chandler Park, Detroit, MI",
    "clark park":             "Clark Park, Detroit, MI",
    "rouge park":             "Rouge Park, Detroit, MI",
    "palmer park":            "Palmer Park, Detroit, MI",
    "milliken state park":    "Milliken State Park, Detroit, MI",
    "hart plaza":             "Hart Plaza, Detroit, MI",
    "cobo":                   "Huntington Place, Detroit, MI",
    "renaissance center":     "Renaissance Center, Detroit, MI",
    "ren cen":                "Renaissance Center, Detroit, MI",
    "fairlane":               "Fairlane Town Center, Dearborn, MI",
    "fairlane town center":   "Fairlane Town Center, Dearborn, MI",
    "somerset":               "Somerset Collection, Troy, MI",
    "twelve oaks":            "Twelve Oaks Mall, Novi, MI",
    "lakeside":               "Lakeside Mall, Sterling Heights, MI",
    "partridge creek":        "Partridge Creek Mall, Clinton Township, MI",
    "wayne state":            "Wayne State University, Detroit, MI",
    "u of d":                 "University of Detroit Mercy, Detroit, MI",
    "uofm dearborn":          "University of Michigan-Dearborn, Dearborn, MI",
    "madonna":                "Madonna University, Livonia, MI",
}


def _resolve_landmark(address: str) -> str | None:
    key = address.lower().strip().rstrip(".,")
    if key in DETROIT_LANDMARKS:
        return DETROIT_LANDMARKS[key]
    base = key.split(",")[0].strip()
    if base in DETROIT_LANDMARKS:
        return DETROIT_LANDMARKS[base]
    return None


def geocode(address: str, feed_id: str | None = None) -> dict:
    """
    Main geocoding entry point.
    Fix #5 — feed_id passed through to Photon for per-feed bias.
    Fix #8 — Google calls rate limited.
    """
    if not address or address == "NO_LOCATION":
        return GEOCODE_FAILURE.copy()

    cached = cache_get(address)
    if cached is not None:
        return cached

    stripped = _strip_unit_suffix(address)
    if stripped != address:
        cached = cache_get(stripped)
        if cached is not None:
            cache_set(address, cached)
            return cached

    address = stripped

    resolved = _resolve_landmark(address)
    if resolved and resolved != address:
        log.info("Landmark resolved", shorthand=address[:40], resolved=resolved)
        cached = cache_get(resolved)
        if cached is not None:
            cache_set(address, cached)
            return cached
        address = resolved

    is_place = _is_named_place(address)
    result   = None

    if is_place:
        photon_result = _photon_query(address, feed_id=feed_id)
        if photon_result and _photon_result_is_plausible_for_place(
                photon_result, address):
            result = photon_result
            result["source"] = "photon_place"
        else:
            log.info("Photon failed for named place — trying Google",
                     address=address[:60])
            result = _geocode_google_place(address)
    else:
        result = _photon_query(address, feed_id=feed_id)

        # Idea 1 — if result is outside the feed's expected area, retry
        # with the feed's primary city appended to the query.
        # e.g. "Cameron Drive" on Plymouth feed -> "Cameron Drive, Plymouth, MI"
        if (result is not None and feed_id and
                not _photon_result_in_feed_area(result["lat"], result["lng"], feed_id)):
            primary_city = FEED_PRIMARY_CITY.get(feed_id)
            if primary_city and primary_city.lower() not in address.lower():
                city_qualified = f"{address}, {primary_city}, MI"
                retry = _photon_query(city_qualified, feed_id=feed_id)
                if retry is not None and _photon_result_in_feed_area(
                        retry["lat"], retry["lng"], feed_id):
                    log.info("Photon city-qualified retry succeeded",
                             original=address[:50],
                             qualified=city_qualified[:60],
                             city=primary_city)
                    result = retry
                else:
                    log.debug("Photon city-qualified retry also out of area",
                              address=address[:50], city=primary_city)

        if result is None:
            log.info("Photon failed for street address — trying Google",
                     address=address[:60])
            result = _geocode_google(address)

    if result is None:
        log.warning("All geocoders failed", address=address[:60])
        return GEOCODE_FAILURE.copy()

    result.pop("_city", None)
    cache_set(address, result)

    log.info("Geocoded",
             address    = address[:60],
             source     = result["source"],
             confidence = result["confidence"],
             lat        = result["lat"],
             lng        = result["lng"],
             is_place   = is_place,
             feed_id    = feed_id or "unknown")
    return result