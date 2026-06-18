"""DB helpers for submission processing. psycopg (v3). Parameterized only.
Mapped to the real stores schema (has_* amenities, dba/display_name, alc_class)."""
import psycopg

# submission report field -> stores column. address excluded: stores uses
# structured house/street/city/zip, not a freeform field. alcohol is special-
# cased in apply_update (sets alc_class = 71 when true).
FIELD_MAP = {
    "hours":         "hours_summary",
    "prepared_food": "has_prepared_food",
    "lottery":       "has_lottery",
    "tobacco":       "has_tobacco",
    "atm":           "has_atm",
    "cat":           "has_cat",
    "snap":          "has_snap",
    "name":          "dba",       # stored UPPER()
}
LOW_RISK  = {"hours", "prepared_food", "lottery", "tobacco", "atm", "cat", "snap", "alcohol"}
HIGH_RISK = {"name", "geom"}
UPDATABLE = LOW_RISK | HIGH_RISK


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn)


# ---------- reads ----------
def nearby_matching_store(conn, lat, lng, name, radius_m, sim_threshold):
    """Existing store within radius AND fuzzy-name match -> its license, else None.
    Matches against dba OR display_name (dba is sparse until backfilled)."""
    row = conn.execute(
        "SELECT license_number, GREATEST("
        "   similarity(lower(coalesce(dba,'')),         lower(%s)),"
        "   similarity(lower(coalesce(display_name,'')),lower(%s))) AS sim "
        "FROM stores "
        "WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s) "
        "  AND GREATEST(similarity(lower(coalesce(dba,'')),         lower(%s)),"
        "               similarity(lower(coalesce(display_name,'')),lower(%s))) >= %s "
        "ORDER BY sim DESC LIMIT 1",
        (name, name, lng, lat, radius_m, name, name, sim_threshold),
    ).fetchone()
    return row[0] if row else None


def license_exists(conn, license_number) -> bool:
    if not license_number:
        return False
    return conn.execute("SELECT 1 FROM stores WHERE license_number = %s",
                        (license_number,)).fetchone() is not None


def pending(conn, mode: str) -> list[dict]:
    """Unprocessed submissions of a mode. submitted_ip aliased to 'ip';
    geom split to lat/lng; structured address + photos/receipt carried."""
    rows = conn.execute(
        "SELECT id, license_number, submitted_ip AS ip, name, "
        "       house, street, city, zip, hours, "
        "       prepared_food, lottery, alcohol, tobacco, atm, cat, snap, "
        "       photos, receipt, ST_Y(geom) AS lat, ST_X(geom) AS lng "
        "FROM submissions WHERE mode = %s AND status = 'pending' "
        "ORDER BY submitted_at",
        (mode,),
    ).fetchall()
    cols = ["id","license_number","ip","name","house","street","city","zip","hours",
            "prepared_food","lottery","alcohol","tobacco","atm","cat","snap",
            "photos","receipt","lat","lng"]
    return [dict(zip(cols, r)) for r in rows]


# ---------- writes (submissions) ----------
def mark(conn, ids: list, status: str, resolution: str | None = None):
    conn.execute("UPDATE submissions SET status = %s, resolution = %s, processed_at = now() "
                 "WHERE id = ANY(%s)", (status, resolution, ids))


def enqueue_images(conn, submission_id, license_number, photos, receipt):
    """On acceptance, push image refs to the vision queue. Originals stay on the
    submission (audit). receipt -> 'receipt', photos -> 'shelf'. UNIQUE(url) dedupes."""
    items = []
    if receipt:
        items.append((receipt, "receipt"))
    for url in (photos or []):
        items.append((url, "shelf"))
    for url, hint in items:
        conn.execute(
            "INSERT INTO image_queue (submission_id, license_number, url, kind_hint) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (url) DO NOTHING",
            (submission_id, license_number, url, hint))


# ---------- writes (stores) ----------
def create_store(conn, license_number, name, house, street, city, zip_, lat, lng):
    """Create a store. license_number is the GUID assigned in the submission (new
    stores have no real license). dba = UPPER(name); join_key = UPPER('house street zip').
    Being in stores IS the verification -- no separate verified flag."""
    dba = (name or "").upper() or None
    join_key = " ".join(p for p in (house, street, zip_) if p).upper() or None
    conn.execute(
        "INSERT INTO stores (license_number, source, dba, display_name, "
        "  house, street, city, zip, geom, join_key) "
        "VALUES (%s, 'submission', %s, %s, %s, %s, %s, %s, "
        "        ST_SetSRID(ST_MakePoint(%s,%s),4326), %s) "
        "ON CONFLICT (license_number) DO NOTHING",
        (license_number, dba, name, house, street, city, zip_, lng, lat, join_key))
    return license_number


def apply_update(conn, license_number, field, value):
    if field not in UPDATABLE:                      # column-name injection guard
        raise ValueError(f"field not updatable: {field}")
    if field == "geom":
        conn.execute("UPDATE stores SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326) "
                     "WHERE license_number = %s", (value[1], value[0], license_number))
        return
    if field == "alcohol":                          # alc_class is a license code; 71 = sells alcohol
        conn.execute("UPDATE stores SET alc_class = %s WHERE license_number = %s",
                     (71 if value else None, license_number))
        return
    col = FIELD_MAP[field]
    v = value.upper() if field == "name" else value
    conn.execute(f'UPDATE stores SET "{col}" = %s WHERE license_number = %s',
                 (v, license_number))


def hide_store(conn, license_number):
    """Suppress from results. We don't know if it's closed or misclassified -- just
    that it should stop showing. Reversible: set hidden = false.
    NEVER hard-delete a store from a submission."""
    conn.execute("UPDATE stores SET hidden = true WHERE license_number = %s",
                 (license_number,))
