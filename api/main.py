"""Map read API over the `stores` spine + the crowdsourced survey write path.

Read (see api/CLAUDE.md):
  GET  /stores?bbox=west,south,east,north  -> light pins for the viewport.
  GET  /stores/{license_number}            -> one full record (detail-on-tap).

Write (field surveys -> `submissions`):
  POST /submissions  (multipart/form-data)  -> save one survey + its photos.
    mode='report' surveys an existing store by license_number; mode='new' logs a
    bodega not yet in the spine (mints a uuid license_number, geom from client lat/lon);
    mode='delete' flags an existing store as gone (logged, never touches the spine).

One call: the client sends the answer fields plus photo files as multipart; the
service streams each file to GCS (storage.py) and stores only the object path on
the row. Fine for a handful of photos — total request is bounded by Cloud Run's
32 MB cap (no video here, so that's plenty of headroom).

Deployed as a Cloud Run Service; DB via the shared Connector engine in db.py.
"""
import os
import re
import uuid
from typing import List, Optional

import sqlalchemy
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from db import engine
from storage import upload_file

app = FastAPI(title="Bodega Map API", description="Read path over the bodega spine.")

# CORS: allow only our own app origin(s) to call the API from a browser. This is
# browser-enforced — it blocks other *websites'* JS from calling us, but does
# nothing to curl/scripts (those are guarded at the gateway layer). Origins come
# from ALLOWED_ORIGINS (comma-separated) so prod vs. local differ by config, not
# code; unset falls back to the Vite dev server for local work.
_origins = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PIN_LIMIT = 2000

# Boolean flag columns the frontend can badge pins with and filter on.
FLAG_COLUMNS = (
    "has_snap", "has_tobacco", "has_lottery", "has_quick_draw", "has_prepared_food",
    "has_cat", "has_atm", "takeout", "delivery",
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
           s.place_id, s.display_name, s.phone, s.rating, s.user_rating_count,
           s.accepts_credit_cards, s.accepts_debit_cards, s.accepts_cash_only,
           s.accepts_nfc, s.takeout, s.delivery, s.hours_summary,
           ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon
    FROM public.stores s
    LEFT JOIN sla_license_codes lc ON lc.class_code = s.alc_class
    WHERE s.license_number = :lid;
""")

# All products for one store, read from the v_products view (which already joins
# subtype + category, so name/label/emoji ride along). Join category once more only
# to pull sort_order for the stable section ordering the client renders by.
PRODUCTS = sqlalchemy.text("""
    SELECT v.product_id, v.name, v.description, v.price_cents, v.price_raw, v.source,
           v.subtype, v.subtype_label,
           v.category AS category_slug, v.category_label, v.category_emoji,
           v.source_category
    FROM public.v_products v
    JOIN public.category c ON c.slug = v.category
    WHERE v.license_number = :lid
    ORDER BY c.sort_order NULLS LAST, v.category_label, v.name;
""")

# Facets: the ENTIRE category list (not just categories this store stocks), each
# with a count of this store's products in it — so the client can render every
# category chip, badging/greying the empty ones. LEFT JOIN keeps zero-count rows.
FACETS = sqlalchemy.text("""
    SELECT c.category_id, c.slug, c.label, c.emoji, c.sort_order,
           count(v.product_id) AS product_count
    FROM public.category c
    LEFT JOIN public.v_products v
           ON v.category = c.slug AND v.license_number = :lid
    GROUP BY c.category_id
    ORDER BY c.sort_order NULLS LAST, c.label;
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
    has_cat: Optional[bool] = Query(None, description="Filter: bodega cat present"),
    has_atm: Optional[bool] = Query(None, description="Filter: ATM on premises"),
    takeout: Optional[bool] = Query(None, description="Filter: offers takeout"),
    delivery: Optional[bool] = Query(None, description="Filter: offers delivery"),
    has_alcohol: Optional[bool] = Query(None, description="Filter on alc_class presence (true = has a license, false = none)"),
    has_products: Optional[bool] = Query(None, description="Filter: true = only stores with catalog items, false = only stores with none"),
):
    west, south, east, north = _parse_bbox(bbox)
    params = {"west": west, "south": south, "east": east, "north": north, "lim": PIN_LIMIT}

    # Tri-state flag filters: None = no filter, True/False = equality on the column.
    # Column names come from the FLAG_COLUMNS allowlist, never user input.
    clauses = []
    flag_args = {
        "has_snap": has_snap, "has_tobacco": has_tobacco, "has_lottery": has_lottery,
        "has_quick_draw": has_quick_draw, "has_prepared_food": has_prepared_food,
        "has_cat": has_cat, "has_atm": has_atm, "takeout": takeout, "delivery": delivery,
    }
    for col in FLAG_COLUMNS:
        val = flag_args[col]
        if val is not None:
            clauses.append(f"AND {col} = :{col}")
            params[col] = val
    if has_alcohol is not None:
        clauses.append("AND alc_class IS " + ("NOT NULL" if has_alcohol else "NULL"))
    # "Has items" isn't a column on stores — it's the presence of products rows.
    # EXISTS short-circuits and rides the products.license_number index; no static
    # values come from the request, so nothing to bind.
    if has_products is not None:
        exists = ("EXISTS" if has_products else "NOT EXISTS")
        clauses.append(
            f"AND {exists} (SELECT 1 FROM public.products p "
            "WHERE p.license_number = stores.license_number)"
        )
    # TODO: "open now" filter — needs structured hours (weekly open/close periods)
    # to compare against current NYC time. hours_summary is free text, not queryable.

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


@app.get("/stores/{license_number}/products")
def get_products(license_number: str):
    """All products for a store (with category + emoji) plus the full category
    list as facets (with per-store counts)."""
    with engine.connect() as cx:
        products = cx.execute(PRODUCTS, {"lid": license_number}).mappings().all()
        facets = cx.execute(FACETS, {"lid": license_number}).mappings().all()
    return {
        "license_number": license_number,
        "products": [dict(r) for r in products],
        "facets": [dict(r) for r in facets],
    }


# ---------------------------------------------------------------------------
# Write path: crowdsourced field surveys -> `submissions`
# ---------------------------------------------------------------------------

INSERT = sqlalchemy.text("""
    INSERT INTO submissions (license_number, mode, name, house, street, city, zip, geom,
                             prepared_food, lottery, alcohol, tobacco, snap,
                             atm, cat, hours, receipt, photos, submitted_ip)
    VALUES (:license_number, :mode, :name, :house, :street, :city, :zip,
            CASE WHEN CAST(:lat AS float8) IS NULL OR CAST(:lon AS float8) IS NULL THEN NULL
                 ELSE ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)), 4326) END,
            :prepared_food, :lottery, :alcohol, :tobacco, :snap,
            :atm, :cat, :hours, :receipt, :photos, :submitted_ip)
    RETURNING id, license_number, submitted_at;
""")


# A `report` license_number is free client input that becomes a GCS object prefix
# (storage.py) and a DB key — validate its shape to keep both clean. Spine keys are
# short alphanumeric; minted `new` keys are uuid hex. Reject anything with path
# separators / out-of-charset chars (e.g. "../.." bucket-prefix steering).
_LICENSE_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def _yn(v: Optional[str]) -> Optional[bool]:
    """yes/no survey answer -> bool; anything else (incl. omitted) -> NULL."""
    if v is None:
        return None
    s = v.strip().lower()
    return True if s in ("yes", "y", "true") else False if s in ("no", "n", "false") else None


def _client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP. Behind Cloud Run the real caller is the first hop of
    X-Forwarded-For (request.client.host is Google's front-end proxy). SPOOFABLE —
    the caller can prepend a fake XFF — so use it for dedup/abuse triage only, never
    for auth or access decisions."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.client.host if request.client else None


@app.post("/submissions", status_code=201)
def create_submission(
    # `request` (no default) must precede the Form(...) params. Used only to read the
    # client IP from headers — it carries no body part of its own.
    request: Request,
    # The survey is sent as multipart/form-data: scalar answers as form fields,
    # photos as file parts in the same request. FastAPI maps each part by name.
    mode: str = Form(..., description='"new" (bodega not in the spine), "report" (existing license_number), or "delete" (flag existing store as gone)'),
    license_number: Optional[str] = Form(None, description="required when mode='report' or 'delete'; ignored & minted (uuid) when mode='new'"),
    name: Optional[str] = Form(None),     # surveyor-provided store name
    house: Optional[str] = Form(None),    # surveyor-provided address parts (mirror the spine)
    street: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    zip: Optional[str] = Form(None),
    lat: Optional[float] = Form(None),    # client-supplied; geom built only if lat AND lon present
    lon: Optional[float] = Form(None),
    prepared_food: Optional[str] = Form(None),  # "yes"/"no" — coerced to bool below
    lottery: Optional[str] = Form(None),
    alcohol: Optional[str] = Form(None),
    tobacco: Optional[str] = Form(None),
    snap: Optional[str] = Form(None),  # accepts SNAP/EBT — "yes"/"no" — coerced to bool below
    atm: Optional[str] = Form(None),  # "yes"/"no" — coerced to bool below
    cat: Optional[str] = Form(None),  # bodega cat present?
    hours: Optional[str] = Form(None),
    receipt: Optional[UploadFile] = File(None),  # one receipt photo, optional
    photos: List[UploadFile] = File(default=[]),  # zero or more store photos
):
    """Persist one survey. Photo files stream to GCS; the row stores their paths.

    mode='new' mints a uuid license_number for a bodega not yet in the spine (so its
    photos/answers get a stable key); mode='report' surveys an existing store by its
    real license_number; mode='delete' flags an existing store as gone (logged like any
    other survey — we don't touch `stores`). Surveys live in `submissions` only.
    """
    if mode not in ("new", "report", "delete"):
        raise HTTPException(400, "mode must be 'new', 'report', or 'delete'")
    if mode == "new":
        license_number = uuid.uuid4().hex  # minted key; no spine row exists yet
    elif not license_number:
        raise HTTPException(400, f"license_number is required when mode='{mode}'")
    elif not _LICENSE_RE.fullmatch(license_number):
        # Bound out of SQL anyway, but it also forms a GCS object prefix — reject
        # path separators / odd chars before it touches storage.
        raise HTTPException(400, "license_number is malformed")

    receipt_path = upload_file(license_number, "receipt", receipt) if receipt else None
    photo_paths = [upload_file(license_number, "photo", p) for p in photos]

    params = {
        "license_number": license_number,
        "mode": mode,
        "name": name,
        "house": house,
        "street": street,
        "city": city,
        "zip": zip,
        "lat": lat,
        "lon": lon,
        "prepared_food": _yn(prepared_food),
        "lottery": _yn(lottery),
        "alcohol": _yn(alcohol),
        "tobacco": _yn(tobacco),
        "snap": _yn(snap),
        "atm": _yn(atm),
        "cat": _yn(cat),
        "hours": hours,
        "receipt": receipt_path,
        "photos": photo_paths,
        "submitted_ip": _client_ip(request),
    }
    with engine.begin() as cx:
        row = cx.execute(INSERT, params).mappings().first()
    return {
        "id": str(row["id"]),
        "license_number": row["license_number"],  # echo the minted uuid for mode='new'
        "mode": mode,
        "submitted_at": row["submitted_at"].isoformat(),
    }
