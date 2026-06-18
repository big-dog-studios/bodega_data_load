"""Google Places verification for submission processing.

Used by the new-store flow only: does a real store exist at this name+location?
(confirm before create). Uses Places API v1 searchText. Swap to your existing
enrichment client if you prefer.

Cost: the field mask drives pricing. We request only `places.id,places.types` —
`types` is needed to confirm the result is a store (not a restaurant/laundromat),
and this mask keeps the call in the Pro tier. `businessStatus` is deliberately NOT
requested: it's Enterprise-tier (would bump the whole call's price) and unused.

Degrades safely on BOTH no-config and call failure: if GOOGLE_MAPS_API_KEY is unset,
or the Places call errors (bad/restricted key -> 403, quota, network), find_store()
returns None — the new-mode pass falls back to IP corroboration instead of the whole
batch 500ing on one external hiccup. A failure is logged (not silent) so a misconfigured
key is still diagnosable in the Cloud Run logs.
"""
import logging, os, requests

_log = logging.getLogger("submissions.places")
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
    try:
        results = _search(name, lat, lng)
    except requests.RequestException as e:
        # One Places failure must not crash the pass — treat as "no match" so the row
        # falls back to IP corroboration. Logged so a forbidden/misconfigured key shows.
        _log.warning("Places lookup failed for %r (%s); skipping Places for this row", name, e)
        return None
    for p in results:
        types = set(p.get("types", []))
        if types & _STORE_TYPES:
            return {"place_id": p.get("id")}
    return None
