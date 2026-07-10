"""Map read API over the `stores` spine + the crowdsourced survey write path.

Read (see api/CLAUDE.md):
  GET  /stores?bbox=west,south,east,north  -> light pins for the viewport.
  GET  /stores/{license_number}            -> one full record (detail-on-tap).
  GET  /sync/stores[?since=<iso>]          -> offline-first delta feed (hidden rows
    included so the client can delete locally; hours nested; cursor = server_time).

Write (field surveys -> `submissions`):
  POST /submissions  (multipart/form-data)  -> save one survey + its photos.
    mode='report' surveys an existing store by license_number; mode='new' logs a
    bodega not yet in the spine (mints a uuid license_number, geom from client lat/lon);
    mode='delete' flags an existing store as gone (logged, never touches the spine).

Catalog (image -> `products`):
  POST /products/scan  (multipart/form-data)  -> classify one receipt/shelf photo
    into products for a license_number (vision.pipeline). Inserts new products /
    updates known prices; punts ambiguous items to review.

One call: the client sends the answer fields plus photo files as multipart; the
service streams each file to GCS (storage.py) and stores only the object path on
the row. Fine for a handful of photos — total request is bounded by Cloud Run's
32 MB cap (no video here, so that's plenty of headroom).

Deployed as a Cloud Run Service; DB via the shared Connector engine in db.py.
"""
import os
import re
import uuid
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import sqlalchemy
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
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
    "has_cat", "has_atm", "has_wic", "has_plant_based", "takeout", "delivery",
)

# Light pins inside the viewport. `&&` uses the GiST index and is exact for
# POINT geom (no ST_Intersects needed). Flags ride along (denormalized booleans,
# no join) so the map can badge pins. Order by a flag-richness proxy so the
# "fullest" bodegas surface first when a low-zoom box holds everything.
# {filters} is filled with column-name clauses built from a fixed allowlist
# (FLAG_COLUMNS) — values stay bound, so no injection surface.
PINS_TEMPLATE = """
    SELECT license_number, dba, ST_Y(geom) AS lat, ST_X(geom) AS lon,
           has_snap, has_tobacco, has_lottery, has_quick_draw, has_prepared_food, has_wic,
           has_plant_based, (alc_class IS NOT NULL) AS has_alcohol
    FROM public.stores
    WHERE geom && ST_MakeEnvelope(:west, :south, :east, :north, 4326)
      AND NOT hidden
      {filters}
    ORDER BY ( has_prepared_food::int + has_snap::int + has_tobacco::int
             + has_lottery::int + (alc_class IS NOT NULL)::int ) DESC
    LIMIT :lim;
"""

# Nearest stores to a point, closest first. The KNN `<->` operator is index-backed by
# the GiST geom index (planar degrees — fine for ranking neighbors at city scale); the
# returned `distance_m` is the true geodesic distance via ::geography. Same flag filters
# as /stores ride along ({filters} from the FLAG_COLUMNS allowlist, values bound).
NEAREST_TEMPLATE = """
    SELECT license_number, dba, ST_Y(geom) AS lat, ST_X(geom) AS lon,
           has_snap, has_tobacco, has_lottery, has_quick_draw, has_prepared_food, has_wic,
           has_plant_based, (alc_class IS NOT NULL) AS has_alcohol,
           ST_Distance(geom::geography,
                       ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS distance_m
    FROM public.stores
    WHERE geom IS NOT NULL
      AND NOT hidden
      {filters}
    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
    LIMIT :lim;
"""

# Full record off the single row — flags are denormalized booleans, so no join
# for them. LEFT JOIN the seeded SLA lookup only to label alc_class.
DETAIL = sqlalchemy.text("""
    SELECT s.license_number, s.dba, s.entity,
           s.house, s.street, s.city, s.county, s.zip, s.estab_type,
           s.has_snap, s.has_tobacco, s.has_lottery, s.has_quick_draw,
           s.has_prepared_food, s.has_atm, s.has_cat, s.cat_name, s.has_wic, s.has_plant_based,
           s.alc_class, lc.class_description AS alc_description, lc.product AS alc_product,
           s.place_id, s.display_name, s.phone, s.rating, s.user_rating_count,
           s.accepts_credit_cards, s.accepts_debit_cards, s.accepts_cash_only,
           s.accepts_nfc, s.takeout, s.delivery, s.hours_summary,
           s.storefront_photos,
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

# Full-fidelity sync feed for the offline-first client. Returns every field the app
# holds locally + `is_hidden` + `updated_at`, with each store's opening hours nested
# inline (one download, one cursor — store_hours is NOT synced on its own; the client
# just files the nested array into its local store_hours table). {where} is empty for a
# full pull or `WHERE s.updated_at > :since` for a delta. Hidden rows are INCLUDED on
# purpose: a just-hidden store must appear in the delta so the client can delete it
# locally (is_hidden=true == "remove"). Ordered by updated_at so the feed is a clean
# monotonic stream. Mirrors the DETAIL column set so a synced row == a /stores/{id} row.
SYNC_TEMPLATE = """
    SELECT s.license_number, s.dba, s.entity,
           s.house, s.street, s.city, s.county, s.zip, s.estab_type,
           s.has_snap, s.has_tobacco, s.has_lottery, s.has_quick_draw,
           s.has_prepared_food, s.has_atm, s.has_cat, s.cat_name, s.has_wic, s.has_plant_based,
           s.alc_class, lc.class_description AS alc_description, lc.product AS alc_product,
           s.place_id, s.display_name, s.phone, s.rating, s.user_rating_count,
           s.accepts_credit_cards, s.accepts_debit_cards, s.accepts_cash_only,
           s.accepts_nfc, s.takeout, s.delivery, s.hours_summary,
           s.hidden AS is_hidden, s.updated_at,
           s.storefront_photos,
           ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon,
           COALESCE((
             SELECT json_agg(json_build_object(
                      'dow', h.dow, 'open_min', h.open_min, 'close_min', h.close_min)
                    ORDER BY h.dow, h.open_min)
             FROM public.store_hours h
             WHERE h.license_number = s.license_number
           ), '[]'::json) AS hours
    FROM public.stores s
    LEFT JOIN sla_license_codes lc ON lc.class_code = s.alc_class
    {where}
    ORDER BY s.updated_at ASC;
"""


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


def _flag_filters(params, flag_args, has_alcohol, has_products, is_open):
    """Build the tri-state flag WHERE clauses shared by /stores and /stores/nearest.

    Appends `AND ...` fragments (and binds their values into `params`) for each set
    filter, and returns the clause list. Column names come from FLAG_COLUMNS (an
    allowlist), never user input, so there's no injection surface. The `stores.`-
    qualified subqueries below work for both callers (both query `public.stores`
    unaliased)."""
    clauses = []
    for col in FLAG_COLUMNS:
        val = flag_args.get(col)
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
    # "Open now": a store_hours row for the current US/Eastern weekday whose
    # [open_min, close_min) window covers the current minute-of-day. Hours are always
    # Eastern, so we evaluate "now" in America/New_York. store_hours.dow is 0=Monday,
    # which matches Python's weekday(), so no remap. Overnight spans are pre-split at
    # midnight on write (see submissions/db.py), so a plain interval test suffices.
    # Stores with no store_hours rows are treated as "not open" (unknown != open).
    if is_open is not None:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        params["now_dow"] = now_et.weekday()                 # 0=Mon .. 6=Sun
        params["now_min"] = now_et.hour * 60 + now_et.minute
        exists = ("EXISTS" if is_open else "NOT EXISTS")
        clauses.append(
            f"AND {exists} (SELECT 1 FROM public.store_hours h "
            "WHERE h.license_number = stores.license_number "
            "AND h.dow = :now_dow AND h.open_min <= :now_min AND h.close_min > :now_min)"
        )
    return clauses


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
    has_wic: Optional[bool] = Query(None, description="Filter: accepts WIC"),
    has_plant_based: Optional[bool] = Query(None, description="Filter: stocks plant-based/vegan products"),
    takeout: Optional[bool] = Query(None, description="Filter: offers takeout"),
    delivery: Optional[bool] = Query(None, description="Filter: offers delivery"),
    has_alcohol: Optional[bool] = Query(None, description="Filter on alc_class presence (true = has a license, false = none)"),
    has_products: Optional[bool] = Query(None, description="Filter: true = only stores with catalog items, false = only stores with none"),
    is_open: Optional[bool] = Query(None, description="Filter on current open status (store hours are US/Eastern): true = open right now, false = not open right now"),
):
    west, south, east, north = _parse_bbox(bbox)
    params = {"west": west, "south": south, "east": east, "north": north, "lim": PIN_LIMIT}

    flag_args = {
        "has_snap": has_snap, "has_tobacco": has_tobacco, "has_lottery": has_lottery,
        "has_quick_draw": has_quick_draw, "has_prepared_food": has_prepared_food,
        "has_cat": has_cat, "has_atm": has_atm, "has_wic": has_wic,
        "has_plant_based": has_plant_based, "takeout": takeout, "delivery": delivery,
    }
    clauses = _flag_filters(params, flag_args, has_alcohol, has_products, is_open)
    sql = sqlalchemy.text(PINS_TEMPLATE.format(filters="\n      ".join(clauses)))
    with engine.connect() as cx:
        rows = cx.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


@app.get("/stores/nearest")
def nearest_stores(
    lat: float = Query(..., ge=-90, le=90, description="Latitude of the search point"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude of the search point"),
    limit: int = Query(1, ge=1, le=100, description="How many nearest stores to return (closest first)"),
    has_snap: Optional[bool] = Query(None),
    has_tobacco: Optional[bool] = Query(None),
    has_lottery: Optional[bool] = Query(None),
    has_quick_draw: Optional[bool] = Query(None),
    has_prepared_food: Optional[bool] = Query(None),
    has_cat: Optional[bool] = Query(None),
    has_atm: Optional[bool] = Query(None),
    has_wic: Optional[bool] = Query(None),
    has_plant_based: Optional[bool] = Query(None),
    takeout: Optional[bool] = Query(None),
    delivery: Optional[bool] = Query(None),
    has_alcohol: Optional[bool] = Query(None, description="Filter on alc_class presence"),
    has_products: Optional[bool] = Query(None, description="Filter: only stores with/without catalog items"),
    is_open: Optional[bool] = Query(None, description="Filter on current open status (US/Eastern)"),
):
    """Nearest stores to (lat, lon), closest first, with the same flag filters as
    /stores. Each row carries `distance_m` (true geodesic metres). Hidden stores are
    excluded (like /stores). Declared BEFORE /stores/{license_number} so the literal
    path wins the route match."""
    params = {"lat": lat, "lon": lon, "lim": limit}
    flag_args = {
        "has_snap": has_snap, "has_tobacco": has_tobacco, "has_lottery": has_lottery,
        "has_quick_draw": has_quick_draw, "has_prepared_food": has_prepared_food,
        "has_cat": has_cat, "has_atm": has_atm, "has_wic": has_wic,
        "has_plant_based": has_plant_based, "takeout": takeout, "delivery": delivery,
    }
    clauses = _flag_filters(params, flag_args, has_alcohol, has_products, is_open)
    sql = sqlalchemy.text(NEAREST_TEMPLATE.format(filters="\n      ".join(clauses)))
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


@app.get("/sync/stores")
def sync_stores(
    since: Optional[datetime] = Query(
        None,
        description="ISO timestamp cursor. Omit for a full pull; pass the previous "
                    "response's server_time to get only rows changed since then."),
):
    """Offline-first delta feed: every store the client should hold (hidden ones
    INCLUDED, so the client can delete them locally), each with its hours nested.

    The cursor is `server_time` from the prior response — NOT the client clock and NOT
    the max row timestamp — to avoid clock-skew gaps. We read the rows and read now()
    inside ONE transaction, so server_time is the snapshot boundary: anything committed
    after it carries updated_at > server_time and is caught on the next call (no row can
    slip through the gap, none is sent twice)."""
    where = ""
    params = {}
    if since is not None:
        where = "WHERE s.updated_at > :since"
        params["since"] = since
    sql = sqlalchemy.text(SYNC_TEMPLATE.format(where=where))
    # One transaction so now() (the cursor) and the row snapshot are consistent.
    with engine.begin() as cx:
        server_time = cx.execute(sqlalchemy.text("SELECT now()")).scalar()
        rows = cx.execute(sql, params).mappings().all()
    return {
        "stores": [dict(r) for r in rows],
        "server_time": server_time.isoformat(),
    }


# ---------------------------------------------------------------------------
# Write path: crowdsourced field surveys -> `submissions`
# ---------------------------------------------------------------------------

INSERT = sqlalchemy.text("""
    INSERT INTO submissions (license_number, mode, name, house, street, city, county, zip, geom,
                             prepared_food, lottery, alcohol, tobacco, snap,
                             atm, cat, wic, plant_based, hours, receipt, photos, user_id)
    VALUES (:license_number, :mode, :name, :house, :street, :city, :county, :zip,
            CASE WHEN CAST(:lat AS float8) IS NULL OR CAST(:lon AS float8) IS NULL THEN NULL
                 ELSE ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)), 4326) END,
            :prepared_food, :lottery, :alcohol, :tobacco, :snap,
            :atm, :cat, :wic, :plant_based, :hours, :receipt, :photos, :user_id)
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


@app.post("/submissions", status_code=201)
def create_submission(
    # The survey is sent as multipart/form-data: scalar answers as form fields,
    # photos as file parts in the same request. FastAPI maps each part by name.
    mode: str = Form(..., description='"new" (bodega not in the spine), "report" (existing license_number), or "delete" (flag existing store as gone)'),
    license_number: Optional[str] = Form(None, description="required when mode='report' or 'delete'; ignored & minted (uuid) when mode='new'"),
    user_id: Optional[str] = Form(None, description="authenticated submitter id; the corroboration unit (distinct user_ids = independent reports). Omit for anonymous — anonymous submissions never corroborate"),
    name: Optional[str] = Form(None),     # surveyor-provided store name
    house: Optional[str] = Form(None),    # surveyor-provided address parts (mirror the spine)
    street: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    county: Optional[str] = Form(None),  # borough/county (BRONX, KINGS, NEW YORK, QUEENS, RICHMOND)
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
    wic: Optional[str] = Form(None),  # accepts WIC — "yes"/"no" — coerced to bool below
    plant_based: Optional[str] = Form(None),  # stocks plant-based/vegan — "yes"/"no" — coerced to bool below
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
        "county": county,
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
        "wic": _yn(wic),
        "plant_based": _yn(plant_based),
        "hours": hours,
        "receipt": receipt_path,
        "photos": photo_paths,
        "user_id": user_id,
    }
    with engine.begin() as cx:
        row = cx.execute(INSERT, params).mappings().first()
    return {
        "id": str(row["id"]),
        "license_number": row["license_number"],  # echo the minted uuid for mode='new'
        "mode": mode,
        "submitted_at": row["submitted_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Catalog write path: an image (receipt / shelf photo) -> `products`
# ---------------------------------------------------------------------------
# vision.pipeline does the real work (gate the image, extract items with Claude
# vision, dedup against the store's existing products, insert new / update price).
# It manages its OWN psycopg3 + pgvector connections — pgvector needs psycopg3,
# which the Cloud SQL Connector can't drive — so it gets a libpq DSN against the
# Cloud SQL unix socket rather than the pg8000 `engine` the read path uses.
# Deploy the service with `--add-cloudsql-instances=$INSTANCE` so /cloudsql/<inst>
# is mounted; set DB_DSN to override for local dev (e.g. a TCP/localhost proxy).


_STORE_EXISTS = sqlalchemy.text("SELECT 1 FROM public.stores WHERE license_number = :lid")


def _socket_dsn() -> str:
    """libpq DSN against the Cloud SQL unix socket, shared by the psycopg3 paths
    (vision catalog scan + the submissions processor). DB_DSN overrides for local dev."""
    dsn = os.environ.get("DB_DSN")
    if dsn:
        return dsn
    return (f"host=/cloudsql/{os.environ['INSTANCE']} dbname={os.environ['DB_NAME']} "
            f"user={os.environ['DB_USER']} password={os.environ['DB_PASS']}")


@app.post("/products/scan", status_code=201)
def scan_image(
    license_number: str = Form(..., description="store to attach detected products to"),
    gcs_path: Optional[str] = Form(None, description="GCS object path or gs:// URI of an image already in a bucket (automated path)"),
    image: Optional[UploadFile] = File(None, description="alternative: upload a receipt/shelf photo directly"),
    kind: Optional[str] = Form(None, description="uploader's classification: 'receipt' or 'general' (shelf or storefront). Verified (not trusted); omit to let the model classify"),
):
    """Classify one receipt/shelf image into products for `license_number`.

    Two ways to supply the image — pass exactly one:
      - `gcs_path` (automated): object path in GCS_BUCKET, or a full gs:// URI. The
        service fetches the bytes server-side (storage.download_image).
      - `image`: a direct multipart file upload.

    Thin wrapper over vision.pipeline.process — the heavy lifting (image gate,
    item extraction, semantic dedup vs the store's existing catalog, insert /
    price-update) lives there. Returns what was applied and what was routed to
    manual review. The pipeline is imported lazily so the read path doesn't carry
    the vision deps / ANTHROPIC_API_KEY / Vertex creds unless this route is hit.

    Unlike /submissions, `products.license_number` is a HARD FK to stores
    (ON DELETE CASCADE), so the store must already exist in the spine — we check
    here and 404 cleanly rather than letting the pipeline's INSERT raise a FK
    violation as a 500.
    """
    if not _LICENSE_RE.fullmatch(license_number):
        raise HTTPException(400, "license_number is malformed")
    if (gcs_path is None) == (image is None):
        raise HTTPException(400, "provide exactly one of `gcs_path` or `image`")
    # The uploader may only claim 'receipt' or 'general' (a store photo — shelf or
    # storefront, the gate decides). Unknown values -> 400.
    if kind is not None:
        kind = {"receipt": "receipt", "general": "general"}.get(kind.strip().lower())
        if kind is None:
            raise HTTPException(400, "kind must be 'receipt' or 'general'")
    with engine.connect() as cx:
        if cx.execute(_STORE_EXISTS, {"lid": license_number}).first() is None:
            raise HTTPException(404, "unknown license_number (not in stores spine)")

    from vision import pipeline  # lazy: pulls in anthropic + vertex at first use

    if gcs_path:
        from storage import download_image
        try:
            image_bytes, media_type = download_image(gcs_path)
        except Exception:
            raise HTTPException(404, f"image not found in GCS: {gcs_path}")
    else:
        image_bytes = image.file.read()
        media_type = image.content_type or "image/jpeg"
    if not image_bytes:
        raise HTTPException(400, "empty image")

    # photo_ref is the durable path we can record for a storefront image — only the
    # GCS-backed path has one (a raw upload's bytes aren't persisted here).
    res = pipeline.process(image_bytes, license_number, _socket_dsn(), media_type,
                           kind=kind, photo_ref=gcs_path)
    return {
        "license_number": license_number,
        "kind": res.kind,                    # receipt | shelf | storefront | other
        "rejected_reason": res.rejected_reason,  # set iff the image was gated out
        "applied": res.applied,              # inserts / price updates / storefront attach
        "review": res.review,                # items punted to manual review
    }


# ---------------------------------------------------------------------------
# Submissions processor trigger: one corroboration-gated pass over `submissions`
# ---------------------------------------------------------------------------
# The `submissions/` package (vendored here like `vision/`) does the real work:
# new -> dedup / Places-confirm / IP-corroborate -> create a store; report ->
# IP-corroborate an attribute claim -> apply to the store; delete -> IP-corroborate
# -> stores.hidden = true. Accepted rows enqueue their photos into image_queue for
# the vision classifier. It runs on psycopg3 (own connections from _socket_dsn()),
# the same reason /products/scan does — so it's imported lazily here too.
#
# This MUTATES the spine (creates/edits/hides stores), so it is two-factor guarded.
# It IS declared in openapi-gateway.yaml (callable only through the gateway, not the
# raw run.app URL in practice), so it carries the global x-api-key like every route —
# but that key is shared with the public apps, so it is NOT sufficient on its own.
# The backend ALSO requires a secret: it compares the X-Process-Token header (which
# the gateway forwards untouched) against the PROCESS_TOKEN env secret. Cloud Scheduler
# hits the gateway URL on a cadence sending BOTH headers (x-api-key + X-Process-Token).
# Fail-closed: if PROCESS_TOKEN is unset the route 503s rather than run unguarded.


@app.post("/submissions/process")
def process_submissions(x_process_token: Optional[str] = Header(None)):
    """Run one pass over pending submissions (admin/cron trigger, not public).

    Guarded by the PROCESS_TOKEN env secret, compared against the X-Process-Token
    header. Returns a per-mode count of submissions still pending after the pass so a
    scheduler/operator can see it's draining. The pass itself commits inside run()."""
    expected = os.environ.get("PROCESS_TOKEN")
    if not expected:
        raise HTTPException(503, "submissions processor is not enabled (PROCESS_TOKEN unset)")
    if x_process_token != expected:
        raise HTTPException(403, "invalid or missing X-Process-Token")

    from submissions import pipeline as sub_pipeline  # lazy: psycopg3 + requests

    dsn = _socket_dsn()
    sub_pipeline.run(dsn)
    # Report what's left pending per mode so the caller can watch the queue drain.
    with engine.connect() as cx:
        rows = cx.execute(sqlalchemy.text(
            "SELECT mode, count(*) AS pending FROM submissions "
            "WHERE status = 'pending' GROUP BY mode"
        )).mappings().all()
    return {"status": "ok", "still_pending": {r["mode"]: r["pending"] for r in rows}}
