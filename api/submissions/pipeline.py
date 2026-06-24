"""Submission processor: new / report / delete, corroboration-gated.

Trust signal is DISTINCT authenticated user_ids (no photo/evidence shortcut;
IP is no longer a vote — kept only for abuse triage). Anonymous submissions
(no user_id) never corroborate. Run periodically (Cloud Run Job). Set-based:
groups pending submissions, acts when a group clears its bar, marks the group,
leaves the rest pending.

status:     pending -> accepted | rejected | duplicate | superseded
resolution: why (places_confirmed | user_corroborated | dup_of_existing | ...)
"""
from collections import defaultdict
from . import db, places

# ---- tunable knobs ----
# Corroboration unit is the distinct authenticated user_id (NOT IP). A submission with
# no user_id (anonymous) never counts toward these thresholds.
DUP_RADIUS_M   = 20    # new-store dedup radius (NYC dense; GPS jitter ~ this)
NAME_SIM       = 0.30  # pg_trgm similarity to call two store names "the same"
NEW_MIN_USERS  = 2     # new store not in Places -> distinct users to create provisional
UPD_LOW_USERS  = 2     # report: hours / booleans
UPD_HIGH_USERS = 3     # report: name / geom
DEL_MIN_USERS  = 3     # delete: distinct users


def _norm(v):
    return " ".join(str(v).lower().split()) if v is not None else None


def _accept(conn, subs_list, resolution):
    """Mark accepted + hand any images to the vision queue (not for deletes)."""
    db.mark(conn, [s["id"] for s in subs_list], "accepted", resolution)
    for s in subs_list:
        db.enqueue_images(conn, s["id"], s["license_number"], s.get("photos"), s.get("receipt"))


# ---------- NEW ----------
def process_new(conn):
    subs = db.pending(conn, "new")
    used = set()
    for i, s in enumerate(subs):
        if s["id"] in used or s["lat"] is None:
            continue
        # 0. exact license already in stores -> fast dupe
        if db.license_exists(conn, s["license_number"]):
            db.mark(conn, [s["id"]], "duplicate", "dup_of_existing")
            used.add(s["id"]); continue
        # 1. fuzzy: same spot + similar name already in stores?
        if db.nearby_matching_store(conn, s["lat"], s["lng"], s["name"] or "",
                                    DUP_RADIUS_M, NAME_SIM):
            db.mark(conn, [s["id"]], "duplicate", "dup_of_existing")
            used.add(s["id"]); continue
        # 2. Google Places confirms a real store -> create verified
        if places.find_store(s["name"] or "", s["lat"], s["lng"]):
            db.create_store(conn, s["license_number"], s["name"], s["house"], s["street"],
                            s["city"], s["county"], s["zip"], s["lat"], s["lng"], s.get("hours"), s)
            _accept(conn, [s], "places_confirmed")
            used.add(s["id"]); continue
        # 3. not in Places -> corroboration from distinct users
        group = [s]
        for t in subs[i+1:]:
            if t["id"] in used or t["lat"] is None:
                continue
            if (_norm(t["name"]) == _norm(s["name"])
                    and abs((t["lat"] or 0) - s["lat"]) < 2e-4
                    and abs((t["lng"] or 0) - s["lng"]) < 2e-4):
                group.append(t)
        users = {g["user_id"] for g in group if g["user_id"]}
        if len(users) >= NEW_MIN_USERS:
            db.create_store(conn, s["license_number"], s["name"], s["house"], s["street"],
                            s["city"], s["county"], s["zip"], s["lat"], s["lng"], s.get("hours"), s)
            _accept(conn, group, "user_corroborated")
            used.update(g["id"] for g in group)
        # else: leave pending until more reports arrive


# ---------- REPORT (attribute claims on an existing store) ----------
def _claims(s):
    """Yield (field, value) pairs a report proposes. address excluded (stores uses
    structured house/street/city/zip). alcohol -> alc_class, handled in apply_update."""
    for f in ("hours", "name", "prepared_food", "lottery", "alcohol", "tobacco", "atm", "cat", "snap", "wic", "plant_based"):
        if s.get(f) is not None:
            yield f, s[f]


def process_report(conn):
    subs = db.pending(conn, "report")
    by_id = {s["id"]: s for s in subs}
    groups = defaultdict(lambda: {"users": set(), "ids": [], "value": None})
    for s in subs:
        for field, value in _claims(s):
            g = groups[(s["license_number"], field, _norm(value))]
            g["value"] = value
            if s["user_id"]:
                g["users"].add(s["user_id"])
            g["ids"].append(s["id"])
    applied_ids = set()
    for (license_number, field, _nv), g in groups.items():
        need = UPD_HIGH_USERS if field in db.HIGH_RISK else UPD_LOW_USERS
        if len(g["users"]) >= need:
            db.apply_update(conn, license_number, field, g["value"])
            applied_ids.update(g["ids"])
        # else pending
    if applied_ids:
        _accept(conn, [by_id[i] for i in applied_ids], "user_corroborated")


# ---------- DELETE ----------
def process_delete(conn):
    subs = db.pending(conn, "delete")
    by_store = defaultdict(lambda: {"users": set(), "ids": []})
    for s in subs:
        if s["user_id"]:
            by_store[s["license_number"]]["users"].add(s["user_id"])
        by_store[s["license_number"]]["ids"].append(s["id"])
    for license_number, g in by_store.items():
        if len(g["users"]) >= DEL_MIN_USERS:
            db.hide_store(conn, license_number)
            db.mark(conn, g["ids"], "accepted", "user_corroborated")   # no image enqueue
        # else pending


def run(dsn: str):
    with db.connect(dsn) as conn:
        process_new(conn)
        process_report(conn)
        process_delete(conn)
        conn.commit()
