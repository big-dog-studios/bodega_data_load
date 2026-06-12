"""Map read API over the `stores` spine + the crowdsourced survey write path.

Read (see api/CLAUDE.md):
  GET  /stores?bbox=west,south,east,north  -> light pins for the viewport.
  GET  /stores/{license_number}            -> one full record (detail-on-tap).

Write (field surveys -> `submissions`):
  POST /submissions  (multipart/form-data)  -> save one survey + its photos.

One call: the client sends the answer fields plus photo files as multipart; the
service streams each file to GCS (storage.py) and stores only the object path on
the row. Fine for a handful of photos — total request is bounded by Cloud Run's
32 MB cap (no video here, so that's plenty of headroom).

Deployed as a Cloud Run Service; DB via the shared Connector engine in db.py.
"""
from typing import List, Optional

import sqlalchemy
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from db import engine
from storage import upload_file

app = FastAPI(title="Bodega Map API", description="Read path over the bodega spine.")

PIN_LIMIT = 2000

# Boolean flag columns the frontend can badge pins with and filter on.
FLAG_COLUMNS = (
    "has_snap", "has_tobacco", "has_lottery", "has_quick_draw", "has_prepared_food",
)

# Light pins inside the viewport. `&&` uses the GiST index and is exact for
# POINT geom (no ST_Intersects needed). Flags ride along (denormalized booleans,
# no join) so the map can badge pins. Order by a flag-richness proxy so the
# "fullest" bodegas surface first when a low-zoom box holds everything.
# {filters} is filled with column-name clauses built from a fixed allowlist
# (FLAG_COLUMNS) — values stay bound, so no injection surface.
PINS_TEMPLATE = """
    SELECT license_number, dba, ST_Y(geom) AS lat, ST_X(geom) AS lon,
           has_snap, has_tobacco, has_lottery, has_quick_draw, has_prepared_food,
           (alc_class IS NOT NULL) AS has_alcohol
    FROM public.stores
    WHERE geom && ST_MakeEnvelope(:west, :south, :east, :north, 4326)
      {filters}
    ORDER BY ( has_prepared_food::int + has_snap::int + has_tobacco::int
             + has_lottery::int + (alc_class IS NOT NULL)::int ) DESC
    LIMIT :lim;
"""

# Full record off the single row — flags are denormalized booleans, so no join
# for them. LEFT JOIN the seeded SLA lookup only to label alc_class.
DETAIL = sqlalchemy.text("""
    SELECT s.license_number, s.dba, s.entity,
           s.house, s.street, s.city, s.county, s.zip, s.estab_type,
           s.has_snap, s.has_tobacco, s.has_lottery, s.has_quick_draw,
           s.has_prepared_food, s.has_atm, s.has_cat, s.cat_name,
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
def list_stores(
    bbox: str = Query(..., description="Viewport: west,south,east,north (minLon,minLat,maxLon,maxLat)"),
    has_snap: Optional[bool] = Query(None, description="Filter: true = only SNAP, false = only non-SNAP, omit = no filter"),
    has_tobacco: Optional[bool] = Query(None),
    has_lottery: Optional[bool] = Query(None),
    has_quick_draw: Optional[bool] = Query(None),
    has_prepared_food: Optional[bool] = Query(None),
    has_alcohol: Optional[bool] = Query(None, description="Filter on alc_class presence (true = has a license, false = none)"),
):
    west, south, east, north = _parse_bbox(bbox)
    params = {"west": west, "south": south, "east": east, "north": north, "lim": PIN_LIMIT}

    # Tri-state flag filters: None = no filter, True/False = equality on the column.
    # Column names come from the FLAG_COLUMNS allowlist, never user input.
    clauses = []
    flag_args = {
        "has_snap": has_snap, "has_tobacco": has_tobacco, "has_lottery": has_lottery,
        "has_quick_draw": has_quick_draw, "has_prepared_food": has_prepared_food,
    }
    for col in FLAG_COLUMNS:
        val = flag_args[col]
        if val is not None:
            clauses.append(f"AND {col} = :{col}")
            params[col] = val
    if has_alcohol is not None:
        clauses.append("AND alc_class IS " + ("NOT NULL" if has_alcohol else "NULL"))

    sql = sqlalchemy.text(PINS_TEMPLATE.format(filters="\n      ".join(clauses)))
    with engine.connect() as cx:
        rows = cx.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


@app.get("/stores/{license_number}")
def get_store(license_number: str):
    with engine.connect() as cx:
        row = cx.execute(DETAIL, {"lid": license_number}).mappings().first()
    if row is None:
        raise HTTPException(404, "store not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Write path: crowdsourced field surveys -> `submissions`
# ---------------------------------------------------------------------------

INSERT = sqlalchemy.text("""
    INSERT INTO submissions (license_number, prepared_food, lottery, alcohol, tobacco,
                             atm, cat, hours, receipt, photos)
    VALUES (:license_number, :prepared_food, :lottery, :alcohol, :tobacco,
            :atm, :cat, :hours, :receipt, :photos)
    RETURNING id, submitted_at;
""")


def _yn(v: Optional[str]) -> Optional[bool]:
    """yes/no survey answer -> bool; anything else (incl. omitted) -> NULL."""
    if v is None:
        return None
    s = v.strip().lower()
    return True if s in ("yes", "y", "true") else False if s in ("no", "n", "false") else None


@app.post("/submissions", status_code=201)
def create_submission(
    # The survey is sent as multipart/form-data: scalar answers as form fields,
    # photos as file parts in the same request. FastAPI maps each part by name.
    license_number: str = Form(...),
    prepared_food: Optional[str] = Form(None),  # "yes"/"no" — coerced to bool below
    lottery: Optional[str] = Form(None),
    alcohol: Optional[str] = Form(None),
    tobacco: Optional[str] = Form(None),
    atm: Optional[str] = Form(None),  # "yes"/"no" — coerced to bool below
    cat: Optional[str] = Form(None),  # bodega cat present?
    hours: Optional[str] = Form(None),
    receipt: Optional[UploadFile] = File(None),  # one receipt photo, optional
    photos: List[UploadFile] = File(default=[]),  # zero or more store photos
):
    """Persist one survey. Photo files stream to GCS; the row stores their paths."""
    receipt_path = upload_file(license_number, "receipt", receipt) if receipt else None
    photo_paths = [upload_file(license_number, "photo", p) for p in photos]

    params = {
        "license_number": license_number,
        "prepared_food": _yn(prepared_food),
        "lottery": _yn(lottery),
        "alcohol": _yn(alcohol),
        "tobacco": _yn(tobacco),
        "atm": _yn(atm),
        "cat": _yn(cat),
        "hours": hours,
        "receipt": receipt_path,
        "photos": photo_paths,
    }
    with engine.begin() as cx:
        row = cx.execute(INSERT, params).mappings().first()
    return {"id": str(row["id"]), "submitted_at": row["submitted_at"].isoformat()}
