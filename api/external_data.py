import json
import os
import time
from pathlib import Path

import httpx
import structlog
import redis as _redis
from dotenv import load_dotenv

load_dotenv()
log      = structlog.get_logger()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

DATA_DIR  = Path(__file__).parent / "data"
TTL_CRIME = 900    # 15 min
TTL_STATIC = 86400  # 24h

BBOX = {"north": 43.1, "south": 42.0, "east": -82.5, "west": -84.1}


def get_redis():
    return _redis.from_url(REDIS_URL, decode_responses=True)


# ── Bundled static GeoJSON files ───────────────────────────────────────────

def load_static(filename: str) -> dict:
    """Load a bundled GeoJSON file, with Redis caching."""
    cache_key = f"external:{filename}"
    r = get_redis()
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)

    path = DATA_DIR / filename
    if not path.exists():
        log.error("Static data file not found", path=str(path))
        return {"type": "FeatureCollection", "features": []}

    with open(path) as f:
        data = json.load(f)

    # Normalize fire station data — fix property names for frontend
    if filename == "DFD_Fire_Station_Locations.geojson":
        for feat in data.get("features", []):
            p = feat["properties"]
            # Rename fields to consistent lowercase
            p["name"]       = p.pop("Firehouse", "")
            p["address"]    = p.pop("Address", "")
            p["battalion"]  = p.pop("Battalion", "")
            p["short"]      = p.pop("FH_Short", "")
            # Lat/Long in properties are backwards (lng/lat) — already
            # correct in geometry.coordinates so just clean up
            p.pop("Lat", None)
            p.pop("Long", None)
            p.pop("FID", None)

    if filename == "DPD_Precincts.geojson":
        for feat in data.get("features", []):
            p = feat["properties"]
            p["precinct"] = p.get("name", "")
            p["label"]    = f"Precinct {p.get('name', '')}"

    if filename == "DFD_Battalions.geojson":
        for feat in data.get("features", []):
            p = feat["properties"]
            p["label"] = p.get("Label_Name", p.get("Battalion", ""))
    
    if filename == "County_Boundaries.geojson":
        for feat in data.get("features", []):
            p = feat["properties"]
            p["label"] = p.get("name", "")

    r.setex(cache_key, TTL_STATIC, json.dumps(data))
    log.info("Static data loaded", file=filename,
             features=len(data.get("features", [])))
    return data


# ── RMS Crime Incidents (live — last 48h) ─────────────────────────────────
# Detroit Open Data Portal — Socrata SODA API
# Dataset: RMS Crime Incidents 2025
# Filtered to last 48 hours, geocoded records only

# ArcGIS Online hosted service — confirmed working URL
# Org ID: qvkbeam7Wirps6zC (Detroit)
RMS_CRIME_URL = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services"
    "/RMS_Crime_Incidents_2025/FeatureServer/0/query"
    "?where=incident_timestamp+>+TIMESTAMP+'{cutoff}'"
    "+AND+latitude+IS+NOT+NULL"
    "+AND+longitude+IS+NOT+NULL"
    "&outFields=incident_id,offense_description,neighborhood,"
    "address,latitude,longitude,incident_timestamp,state_offense_code"
    "&resultRecordCount=500"
    "&orderByFields=incident_timestamp+DESC"
    "&f=geojson"
)

CRIME_CATEGORY_COLORS = {
    "HOMICIDE":        "#ff0000",
    "SEXUAL ASSAULT":  "#ff4499",
    "ROBBERY":         "#ff6600",
    "ASSAULT":         "#ff8800",
    "BURGLARY":        "#ffaa00",
    "VEHICLE THEFT":   "#ffcc00",
    "LARCENY":         "#ffee00",
    "ARSON":           "#ff4400",
    "OTHER":           "#888888",
}


def _crime_color(offense: str) -> str:
    offense_upper = (offense or "").upper()
    for key, color in CRIME_CATEGORY_COLORS.items():
        if key in offense_upper:
            return color
    return CRIME_CATEGORY_COLORS["OTHER"]


async def fetch_recent_crimes() -> dict:
    cache_key = "external:rms_crimes"
    r = get_redis()
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)

    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    url = RMS_CRIME_URL.replace("{cutoff}", cutoff)

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            records = resp.json()

        features = []
        for rec in records:
            try:
                lat = float(rec.get("latitude") or 0)
                lng = float(rec.get("longitude") or 0)
                if not (BBOX["south"] <= lat <= BBOX["north"] and
                        BBOX["west"]  <= lng <= BBOX["east"]):
                    continue

                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type":        "Point",
                        "coordinates": [lng, lat],
                    },
                    "properties": {
                        "id":          rec.get("incident_id", ""),
                        "offense":     rec.get("offense_description", ""),
                        "category":    rec.get("state_offense_code", ""),
                        "address":     rec.get("address", ""),
                        "neighborhood":rec.get("neighborhood", ""),
                        "timestamp":   rec.get("incident_timestamp", ""),
                        "color":       _crime_color(
                            rec.get("offense_description", "")
                        ),
                    },
                })
            except (ValueError, TypeError):
                continue

        geojson = {
            "type":        "FeatureCollection",
            "features":    features,
            "_count":      len(features),
            "_fetched_at": int(time.time()),
            "_cutoff":     cutoff,
        }

        r.setex(cache_key, TTL_CRIME, json.dumps(geojson))
        log.info("RMS crimes fetched", count=len(features), cutoff=cutoff)
        return geojson

    except Exception as e:
        log.error("RMS crime fetch failed", error=str(e))
        return {
            "type": "FeatureCollection", "features": [],
            "_error": str(e), "_count": 0,
        }


# ── DTE Energy Outages ─────────────────────────────────────────────────────
# Public ArcGIS endpoint on GISRest subdomain — updated every 15 minutes.
# Layer 2 = OutageAreas (polygons), Layer 0 = ServiceArea, Layer 1 = ZipCodes
# Note: the /arcgis/ path returns 403 but /GISRest/ path works.

DTE_URL = (
    "https://outagemap.serv.dteenergy.com/GISRest/services/OMP"
    "/OutageLocations/MapServer/2/query"
    "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=500"
    "&geometry=-84.1%2C42.0%2C-82.5%2C43.1"
    "&geometryType=esriGeometryEnvelope&inSR=4326"
    "&spatialRel=esriSpatialRelIntersects"
)


async def fetch_dte_outages() -> dict:
    cache_key = "external:dte_outages"
    r = get_redis()
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(DTE_URL, headers={
                "Referer":    "https://outagemap.serv.dteenergy.com/",
                "User-Agent": "Mozilla/5.0 (compatible; DetroitPulse/1.0)",
            })
            resp.raise_for_status()
            data = resp.json()
            data["_count"]      = len(data.get("features", []))
            data["_fetched_at"] = int(time.time())
            r.setex(cache_key, 900, json.dumps(data))
            log.info("DTE outages fetched", count=data["_count"])
            return data
    except Exception as e:
        log.error("DTE outage fetch failed", error=str(e))
        return {"type": "FeatureCollection", "features": [],
                "_error": str(e), "_count": 0}
