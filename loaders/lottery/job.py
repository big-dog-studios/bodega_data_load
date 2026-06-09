"""Lottery loader entrypoint.

extract(): pull the NYS Lottery Retailers SODA API (2vvn-pdyi), prefiltered to a
NYC bounding box server-side (the feed is statewide with no borough field).
main(): transform -> stage table (indexed) -> flag matching stores with
has_lottery (and has_quick_draw where the retailer offers Quick Draw).
Corroborating only: sets flags on a geocode (~15m) OR join_key match, never deletes.

Matching is split into two index-backed passes (join_key equality, then a GiST
bbox-prefiltered ST_DWithin) instead of one OR, so the spatial index is used.

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS,
optional SODA_TOKEN.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = "https://data.ny.gov/resource/2vvn-pdyi.json"
PAGE = 50000
SELECT = "retailer,street,zip,latitude,longitude,quick_draw"
# NYC envelope: within_box(point, NW_lat, NW_lon, SE_lat, SE_lon).
NYC_BOX = "within_box(georeference, 40.92, -74.27, 40.49, -73.68)"


def extract():
    rows, off = [], 0
    while True:
        batch = requests.get(
            API,
            params={"$select": SELECT, "$where": NYC_BOX,
                    "$limit": PAGE, "$offset": off},
            headers={"X-App-Token": os.environ.get("SODA_TOKEN", "")},
            timeout=120,
        ).json()
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return pd.DataFrame(rows)


# Build a geom column + indexes on the staged rows so matching uses the index.
PREP = [
    "ALTER TABLE lottery_stage ADD COLUMN geom geometry(Point, 4326);",
    "UPDATE lottery_stage SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326) "
    "WHERE lon IS NOT NULL;",
    "CREATE INDEX ON lottery_stage USING gist (geom);",
    "CREATE INDEX ON lottery_stage (join_key);",
    "ANALYZE lottery_stage;",
]

RESET = sqlalchemy.text(
    "UPDATE stores SET has_lottery = false, has_quick_draw = false "
    "WHERE has_lottery OR has_quick_draw;")

# Two index-backed passes per flag: join_key equality, then GiST bbox + ST_DWithin.
# ST_Expand(0.0003 deg ~= 33m) is a safe prefilter box; ST_DWithin refines to 15m.
PASSES = [
    "UPDATE stores s SET has_lottery = true FROM lottery_stage st "
    "WHERE st.join_key <> '' AND st.join_key = s.join_key;",
    "UPDATE stores s SET has_lottery = true FROM lottery_stage st "
    "WHERE s.geom && ST_Expand(st.geom, 0.0003) "
    "AND ST_DWithin(s.geom::geography, st.geom::geography, 15);",
    "UPDATE stores s SET has_quick_draw = true FROM lottery_stage st "
    "WHERE st.quick_draw AND st.join_key <> '' AND st.join_key = s.join_key;",
    "UPDATE stores s SET has_quick_draw = true FROM lottery_stage st "
    "WHERE st.quick_draw AND s.geom && ST_Expand(st.geom, 0.0003) "
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
            out.to_sql("lottery_stage", cx, if_exists="replace", index=False)
            for stmt in PREP:
                cx.execute(sqlalchemy.text(stmt))
            cx.execute(RESET)
            for stmt in PASSES:
                cx.execute(sqlalchemy.text(stmt))
            lottery = cx.execute(sqlalchemy.text(
                "SELECT count(*) FROM stores WHERE has_lottery")).scalar()
            quick = cx.execute(sqlalchemy.text(
                "SELECT count(*) FROM stores WHERE has_quick_draw")).scalar()
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS lottery_stage;"))
    finally:
        connector.close()
    print(f"lottery: {len(out)} NYC retailers -> flagged {lottery} stores "
          f"({quick} with quick draw)")


if __name__ == "__main__":
    main()
