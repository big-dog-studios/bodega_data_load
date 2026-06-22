"""DB helpers for submission processing. psycopg (v3). Parameterized only.
Mapped to the real stores schema (has_* amenities, dba/display_name, alc_class)."""
import json

import psycopg

# Day index 0 = Monday (the client's hours-picker convention), abbrevs in week order.
_DAY_ABBR = ["M", "Tu", "W", "Th", "F", "Sa", "Su"]


def _fmt_time(m) -> str:
    """Minutes-from-midnight -> 12h clock. 420 -> '7AM', 510 -> '8:30AM', 0 -> '12AM'."""
    m = int(m) % (24 * 60)
    h, mn = divmod(m, 60)
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}{period}" if mn == 0 else f"{h12}:{mn:02d}{period}"


def _fmt_days(days) -> str:
    """Compress 0-6 day indices into runs: all 7 -> 'Daily', [0..5] -> 'M-Sa',
    [0,2,4] -> 'M-W-F' style joined by ',', single -> 'M'."""
    ds = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    if not ds:
        return ""
    if len(ds) == 7:
        return "Daily"
    runs, start, prev = [], ds[0], ds[0]
    for d in ds[1:]:
        if d == prev + 1:
            prev = d
        else:
            runs.append((start, prev)); start = prev = d
    runs.append((start, prev))
    return ",".join(_DAY_ABBR[a] if a == b else f"{_DAY_ABBR[a]}-{_DAY_ABBR[b]}"
                    for a, b in runs)


def format_hours(raw) -> str | None:
    """Structured hours JSON -> readable hours_summary, e.g.
    'M-Sa: 7AM - 11PM, Su: 8:30AM - 8:30PM'. Accepts the JSON string (or a dict).
    Returns None on empty/garbage so we never store a raw blob or crash the pass.

    Shape: {"v":1,"groups":[{"days":[0..6],"mode":"24"|"hours"|"closed",
            "open":<min>,"close":<min>}]}. open/close are minutes from midnight."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        groups = data.get("groups", [])
    except (ValueError, AttributeError, TypeError):
        return None
    segs = []
    for g in groups:
        try:
            label = _fmt_days(g.get("days", []))
            if not label:
                continue
            mode = g.get("mode")
            if mode == "24":
                when = "24 hours"
            elif mode == "closed":
                when = "Closed"
            elif mode == "hours" and g.get("open") is not None and g.get("close") is not None:
                when = f"{_fmt_time(g['open'])} - {_fmt_time(g['close'])}"
            else:
                continue
            segs.append((min(int(d) for d in g["days"]), f"{label}: {when}"))
        except (ValueError, TypeError, KeyError):
            continue
    if not segs:
        return None
    segs.sort(key=lambda x: x[0])           # week order, earliest day first
    return ", ".join(s for _, s in segs)


def _expand_hours(data) -> list[tuple]:
    """Structured hours dict -> [(dow, open_min, close_min), ...] rows for store_hours.
    dow is 0=Monday (the picker convention, == Python's weekday()), open/close are
    minutes from midnight. mode '24' -> a full (0,1440) day; 'closed'/anything else ->
    no rows. An overnight span (close <= open, e.g. 6AM->2AM) is SPLIT at midnight into
    (open,1440) on the day and (0,close) on the next day so every row satisfies the
    table's close_min > open_min / close_min <= 1440 checks and the is_open interval
    test needs no wraparound logic. Garbage groups are skipped, never raised."""
    rows = []
    for g in (data.get("groups", []) if isinstance(data, dict) else []):
        try:
            mode = g.get("mode")
            days = [int(d) for d in g.get("days", []) if 0 <= int(d) <= 6]
            if not days:
                continue
            if mode == "24":
                for d in days:
                    rows.append((d, 0, 1440))
            elif mode == "hours":
                o, c = g.get("open"), g.get("close")
                if o is None or c is None:
                    continue
                o, c = int(o), int(c)
                if not (0 <= o < 1440) or not (0 < c <= 1440):
                    continue
                for d in days:
                    if c > o:                       # same-day window
                        rows.append((d, o, c))
                    elif c < o:                     # overnight: split at midnight
                        rows.append((d, o, 1440))
                        rows.append(((d + 1) % 7, 0, c))
                    # c == o: degenerate zero-length window -> skip
            # mode == "closed" (or unknown): contributes no open rows
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(set(rows))                          # de-dup overlapping groups


def write_store_hours(conn, license_number, raw):
    """Replace a store's store_hours rows from the submission's structured hours JSON.
    Idempotent delete-then-insert of the expanded weekly rows (store_hours powers the
    is_open filter on GET /stores). No-op when raw is empty or not a valid hours dict —
    we leave existing rows untouched rather than wipe on garbage. A structurally valid
    payload that encodes all-closed clears the rows (store reads as never open)."""
    if not raw:
        return
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict) or "groups" not in data:
        return
    rows = _expand_hours(data)
    conn.execute("DELETE FROM store_hours WHERE license_number = %s", (license_number,))
    if rows:
        # executemany lives on the cursor in psycopg3 (conn.execute is a shortcut, but
        # there is no conn.executemany), so go through an explicit cursor here.
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO store_hours (license_number, dow, open_min, close_min) "
                "VALUES (%s, %s, %s, %s)",
                [(license_number, d, o, c) for (d, o, c) in rows])

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
    "wic":           "has_wic",
    "name":          "dba",       # stored UPPER()
}
LOW_RISK  = {"hours", "prepared_food", "lottery", "tobacco", "atm", "cat", "snap", "wic", "alcohol"}
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
    """Unprocessed submissions of a mode. user_id is the corroboration unit;
    submitted_ip aliased to 'ip' (abuse triage only). geom split to lat/lng;
    structured address + photos/receipt carried."""
    rows = conn.execute(
        "SELECT id, license_number, user_id, submitted_ip AS ip, name, "
        "       house, street, city, zip, hours, "
        "       prepared_food, lottery, alcohol, tobacco, atm, cat, snap, wic, "
        "       photos, receipt, ST_Y(geom) AS lat, ST_X(geom) AS lng "
        "FROM submissions WHERE mode = %s AND status = 'pending' "
        "ORDER BY submitted_at",
        (mode,),
    ).fetchall()
    cols = ["id","license_number","user_id","ip","name","house","street","city","zip","hours",
            "prepared_food","lottery","alcohol","tobacco","atm","cat","snap","wic",
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
def create_store(conn, license_number, name, house, street, city, zip_, lat, lng,
                 hours=None, flags=None):
    """Create a store. license_number is the GUID assigned in the submission (new
    stores have no real license). dba = UPPER(name); join_key = UPPER('house street zip').
    hours is the submission's structured hours JSON -> formatted into hours_summary.

    flags is the submission row (or any dict) carrying the survey booleans
    prepared_food/lottery/alcohol/tobacco/snap/atm/cat/wic -> the store's has_*/alc_class
    columns. A survey-created store has no government feed yet, so the surveyor's
    answers are the only signal; the additive loaders (snap/tobacco/lottery/...)
    corroborate later. NULL/False -> false; alcohol true -> alc_class 71 (sells beer).
    Being in stores IS the verification -- no separate verified flag."""
    flags = flags or {}
    yn = lambda k: bool(flags.get(k))      # unanswered (None) or False -> False
    dba = (name or "").upper() or None
    join_key = " ".join(p for p in (house, street, zip_) if p).upper() or None
    conn.execute(
        "INSERT INTO stores (license_number, source, dba, display_name, "
        "  house, street, city, zip, geom, join_key, hours_summary, "
        "  has_prepared_food, has_lottery, has_tobacco, has_snap, has_atm, has_cat, has_wic, alc_class) "
        "VALUES (%s, 'submission', %s, %s, %s, %s, %s, %s, "
        "        ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s, "
        "        %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (license_number) DO NOTHING",
        (license_number, dba, name, house, street, city, zip_, lng, lat, join_key,
         format_hours(hours),
         yn("prepared_food"), yn("lottery"), yn("tobacco"), yn("snap"),
         yn("atm"), yn("cat"), yn("wic"), 71 if flags.get("alcohol") else None))
    write_store_hours(conn, license_number, hours)   # structured rows for the is_open filter
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
    if field == "name":
        v = value.upper()
    elif field == "hours":          # structured JSON -> readable hours_summary, not raw blob
        v = format_hours(value)
        write_store_hours(conn, license_number, value)   # + structured rows for is_open
    else:
        v = value
    conn.execute(f'UPDATE stores SET "{col}" = %s WHERE license_number = %s',
                 (v, license_number))


def hide_store(conn, license_number):
    """Suppress from results. We don't know if it's closed or misclassified -- just
    that it should stop showing. Reversible: set hidden = false.
    NEVER hard-delete a store from a submission."""
    conn.execute("UPDATE stores SET hidden = true WHERE license_number = %s",
                 (license_number,))
