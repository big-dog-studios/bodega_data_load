"""Google Places verification for submission processing.

Used by the new-store flow only: does a real store exist at this name+location?
(confirm before create). Uses Places API v1 searchText. Swap to your existing
enrichment client if you prefer.

Cost: the field mask drives pricing. We request only `places.id,places.types` —
`types` is needed to confirm the result is a store (not a restaurant/laundromat),
and this mask keeps the call in the Pro tier. `businessStatus` is deliberately NOT
requested: it's Enterprise-tier (would bump the whole call's price) and unused.

Degrades safely: if GOOGLE_MAPS_API_KEY is unset, find_store() returns None (the
new-mode pass falls back to IP corroboration) instead of raising — so the processor
still runs without Places configured.
"""
import os, requests

_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELDS = "places.id,places.types"
_STORE_TYPES = {"convenience_store", "grocery_store", "supermarket", "store", "food"}


def _search(name: str, lat: float, lng: float, radius_m: int = 60) -> list[dict]:
    body = {"textQuery": name,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                        "radius": float(radius_m)}}}
    r = requests.post(_URL, json=body, timeout=10, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _KEY,
        "X-Goog-FieldMask": _FIELDS})
    r.raise_for_status()
    return r.json().get("places", [])


def find_store(name: str, lat: float, lng: float) -> dict | None:
    """Return {"place_id": <id>} for the best store-type match near the point, or None.

    The caller (pipeline.process_new) only checks truthiness — create_store uses the
    submission's own name/lat/lng, not the Places values — so we return just the id.
    No key configured -> None (skip the Places step; the caller falls back to IP
    corroboration). A real "no match" also returns None, so the two collapse cleanly."""
    if not _KEY:
        return None
    for p in _search(name, lat, lng):
        types = set(p.get("types", []))
        if types & _STORE_TYPES:
            return {"place_id": p.get("id")}
    return None
