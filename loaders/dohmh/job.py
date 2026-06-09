"""DOHMH loader entrypoint.

extract(): page the DOHMH Restaurant Inspections SODA API (43nn-pn8j), deduped to
one row per establishment server-side ($group=camis) — the raw feed is one row per
violation per inspection, so a single store appears in many rows.
main(): transform -> stage table (indexed) -> flag matching stores with
has_prepared_food. NYC-only dataset; corroborating only (geocode ~15m OR join_key,
never deletes). Presence means the store prepares/serves food (deli/grill bodega).

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS,
optional SODA_TOKEN.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = "https://data.cityofnewyork.us/resource/43nn-pn8j.json"
PAGE = 50000
# One row per establishment: group by camis, take a representative address/geo.
SELECT = ("camis,max(building) as building,max(street) as street,"
          "max(zipcode) as zipcode,max(latitude) as latitude,"
          "max(longitude) as longitude")
GROUP = "camis"


def extract():
    rows, off = [], 0
    while True:
        batch = requests.get(
            API,
            params={"$select": SELECT, "$group": GROUP,
                    "$limit": PAGE, "$offset": off},
            headers={"X-App-Token": os.environ.get("SODA_TOKEN", "")},
            timeout=180,
        ).json()
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return pd.DataFrame(rows)


# Build a geom column + indexes on the staged rows so matching uses the index.
PREP = [
    "ALTER TABLE dohmh_stage ADD COLUMN geom geometry(Point, 4326);",
    "UPDATE dohmh_stage SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326) "
    "WHERE lon IS NOT NULL;",
    "CREATE INDEX ON dohmh_stage USING gist (geom);",
    "CREATE INDEX ON dohmh_stage (join_key);",
    "ANALYZE dohmh_stage;",
]

# Clear stale flags so re-runs reflect the current establishment set.
RESET = sqlalchemy.text(
    "UPDATE stores SET has_prepared_food = false WHERE has_prepared_food;")

# Two index-backed passes: join_key equality, then GiST bbox + ST_DWithin (~15m).
PASSES = [
    "UPDATE stores s SET has_prepared_food = true FROM dohmh_stage st "
    "WHERE st.join_key <> '' AND st.join_key = s.join_key;",
    "UPDATE stores s SET has_prepared_food = true FROM dohmh_stage st "
    "WHERE s.geom && ST_Expand(st.geom, 0.0003) "
    "AND ST_DWithin(s.geom::geography, st.geom::geography, 15);",
]


def main():
    out = transform(extract())
    connector = Connector()

    def getconn():
        return connector.connect(
            os.environ["INSTANCE"], "pg8000",
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASS"],
            db=os.environ["DB_NAME"],
        )

    eng = sqlalchemy.create_engine("postgresql+pg8000://", creator=getconn)
    try:
        with eng.begin() as cx:
            out.to_sql("dohmh_stage", cx, if_exists="replace", index=False)
            for stmt in PREP:
                cx.execute(sqlalchemy.text(stmt))
            cx.execute(RESET)
            for stmt in PASSES:
                cx.execute(sqlalchemy.text(stmt))
            tagged = cx.execute(sqlalchemy.text(
                "SELECT count(*) FROM stores WHERE has_prepared_food")).scalar()
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS dohmh_stage;"))
    finally:
        connector.close()
    print(f"dohmh: {len(out)} establishments -> flagged {tagged} stores")


if __name__ == "__main__":
    main()
