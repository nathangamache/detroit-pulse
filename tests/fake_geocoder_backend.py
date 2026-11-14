"""Stand-in for the free-geocoder backend (`module:Class` contract used by LocalIndexProvider)."""


class FakeIndex:
    calls: list = []

    def __init__(self, index_dir: str):
        self.index_dir = index_dir

    def geocode(self, struct: dict, bias=(42.33, -83.05)):
        FakeIndex.calls.append(struct)
        if struct["kind"] == "intersection" and struct["street1"] == "Packard Street":
            return {"lat": 42.2650, "lng": -83.7310, "formatted": "Packard St & Stadium Blvd",
                    "confidence": "HIGH", "source": "osm_intersection"}
        return None
