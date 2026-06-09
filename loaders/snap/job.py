"""SNAP loader entrypoint.

extract(): page the USDA SNAP retailer ArcGIS FeatureServer, prefiltered to the 5
NYC counties via the `where` clause (server-side, so only ~8.6k NYC rows come back,
not the ~250k national set). ArcGIS caps pages at 1000, so page on `resultOffset`.
main(): transform -> stage table (indexed) -> flag matching stores with has_snap.
Corroborating only: geocode (~15m) OR join_key match, never deletes.

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = ("https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/"
       "snap_retailer_location_data/FeatureServer/0/query")
WHERE = ("State='NY' AND County IN "
         "('BRONX','KINGS','NEW YORK','QUEENS','RICHMOND')")
FIELDS = "Record_ID,Store_Street_Address,Zip_Code,Latitude,Longitude"
PAGE = 1000  # ArcGIS maxRecordCount for this layer


def extract():
    rows, off = [], 0
    while True:
        resp = requests.get(
            API,
            params={"where": WHERE, "outFields": FIELDS,
                    "returnGeometry": "false", "f": "json",
                    "resultOffset": off, "resultRecordCount": PAGE},
            timeout=120,
        ).json()
        feats = resp.get("features", [])
        rows += [f["attributes"] for f in feats]
        off += PAGE
        if len(feats) < PAGE:
            break
    return pd.DataFrame(rows)


# Build a geom column + indexes on the staged rows so matching uses the index.
PREP = [
    "ALTER TABLE snap_stage ADD COLUMN geom geometry(Point, 4326);",
    "UPDATE snap_stage SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326) "
    "WHERE lon IS NOT NULL;",
    "CREATE INDEX ON snap_stage USING gist (geom);",
    "CREATE INDEX ON snap_stage (join_key);",
    "ANALYZE snap_stage;",
]

# Clear stale flags so re-runs reflect the current retailer set.
RESET = sqlalchemy.text("UPDATE stores SET has_snap = false WHERE has_snap;")

# Two index-backed passes: join_key equality, then GiST bbox + ST_DWithin (~15m).
PASSES = [
    "UPDATE stores s SET has_snap = true FROM snap_stage st "
    "WHERE st.join_key <> '' AND st.join_key = s.join_key;",
    "UPDATE stores s SET has_snap = true FROM snap_stage st "
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
            out.to_sql("snap_stage", cx, if_exists="replace", index=False)
            for stmt in PREP:
                cx.execute(sqlalchemy.text(stmt))
            cx.execute(RESET)
            for stmt in PASSES:
                cx.execute(sqlalchemy.text(stmt))
            tagged = cx.execute(sqlalchemy.text(
                "SELECT count(*) FROM stores WHERE has_snap")).scalar()
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS snap_stage;"))
    finally:
        connector.close()
    print(f"snap: {len(out)} NYC retailers -> flagged {tagged} stores")


if __name__ == "__main__":
    main()
