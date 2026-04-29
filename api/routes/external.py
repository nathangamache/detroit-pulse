from fastapi import APIRouter
from api.external_data import load_static, fetch_recent_crimes, fetch_dte_outages

router = APIRouter(prefix="/external", tags=["external"])


@router.get("/fire-stations")
def get_fire_stations():
    return load_static("DFD_Fire_Station_Locations.geojson")


@router.get("/precincts")
def get_precincts():
    return load_static("DPD_Precincts.geojson")


@router.get("/battalions")
def get_battalions():
    return load_static("DFD_Battalions.geojson")


@router.get("/crimes")
async def get_crimes():
    """RMS crime incidents from last 48 hours."""
    return await fetch_recent_crimes()


@router.get("/dte-outages")
async def get_dte_outages():
    """DTE Energy live outage areas (updated every 15min)."""
    return await fetch_dte_outages()


@router.get("/counties")
def get_counties():
    return load_static("County_Boundaries.geojson")


@router.post("/refresh/{dataset}")
def refresh_dataset(dataset: str):
    import redis, os
    r = redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    key_map = {
        "fire_stations": "external:DFD_Fire_Station_Locations.geojson",
        "precincts":     "external:DPD_Precincts.geojson",
        "battalions":    "external:DFD_Battalions.geojson",
        "crimes":        "external:rms_crimes",
        "dte_outages":   "external:dte_outages",
        "counties":      "external:County_Boundaries.geojson",
    }
    if dataset not in key_map:
        return {"error": f"Unknown dataset: {dataset}"}
    r.delete(key_map[dataset])
    return {"refreshed": dataset, "cache_cleared": True}
