"""Map read API over the `stores` spine (FastAPI, read-only).

Two endpoints (see api/CLAUDE.md):
  GET /stores?bbox=west,south,east,north  -> light pins for the viewport.
  GET /stores/{license_number}            -> one full record (detail-on-tap).

Deployed as a Cloud Run Service; DB via the shared Connector engine in db.py.
"""
import sqlalchemy
from fastapi import FastAPI, HTTPException, Query

from db import engine

app = FastAPI(title="Bodega Map API", description="Read path over the bodega spine.")

PIN_LIMIT = 2000

# Light pins inside the viewport. `&&` uses the GiST index and is exact for
# POINT geom (no ST_Intersects needed). Order by a flag-richness proxy so the
# "fullest" bodegas surface first when a low-zoom box holds everything.
PINS = sqlalchemy.text("""
    SELECT license_number, dba, ST_Y(geom) AS lat, ST_X(geom) AS lon
    FROM public.stores
    WHERE geom && ST_MakeEnvelope(:west, :south, :east, :north, 4326)
    ORDER BY ( has_prepared_food::int + has_snap::int + has_tobacco::int
             + has_lottery::int + (alc_class IS NOT NULL)::int ) DESC
    LIMIT :lim;
""")

# Full record off the single row — flags are denormalized booleans, so no join
# for them. LEFT JOIN the seeded SLA lookup only to label alc_class.
DETAIL = sqlalchemy.text("""
    SELECT s.license_number, s.dba, s.entity,
           s.house, s.street, s.city, s.county, s.zip, s.estab_type,
           s.has_snap, s.has_tobacco, s.has_lottery, s.has_quick_draw,
           s.has_prepared_food,
           s.alc_class, lc.class_description AS alc_description, lc.product AS alc_product,
           ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon
    FROM public.stores s
    LEFT JOIN sla_license_codes lc ON lc.class_code = s.alc_class
    WHERE s.license_number = :lid;
""")


def _parse_bbox(bbox: str):
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(400, "bbox must be 'west,south,east,north' (4 values)")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(400, "bbox values must be numeric")
    # Axis order is the documented footgun: lon then lat, SW then NE.
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise HTTPException(400, "longitude (west/east) out of range [-180, 180]")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(400, "latitude (south/north) out of range [-90, 90]")
    if west >= east:
        raise HTTPException(400, "west must be < east (axis order: minLon,minLat,maxLon,maxLat)")
    if south >= north:
        raise HTTPException(400, "south must be < north (axis order: minLon,minLat,maxLon,maxLat)")
    return west, south, east, north


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stores")
def list_stores(bbox: str = Query(..., description="Viewport: west,south,east,north (minLon,minLat,maxLon,maxLat)")):
    west, south, east, north = _parse_bbox(bbox)
    with engine.connect() as cx:
        rows = cx.execute(
            PINS, {"west": west, "south": south, "east": east, "north": north, "lim": PIN_LIMIT}
        ).mappings().all()
    return [dict(r) for r in rows]


@app.get("/stores/{license_number}")
def get_store(license_number: str):
    with engine.connect() as cx:
        row = cx.execute(DETAIL, {"lid": license_number}).mappings().first()
    if row is None:
        raise HTTPException(404, "store not found")
    return dict(row)
