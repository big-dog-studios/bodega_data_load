"""Tobacco loader entrypoint.

extract(): page the DCWP Tobacco Retail Dealer Licenses SODA API (adw8-wvxb),
filtered to Active licenses (the feed also carries expired/surrendered rows).
NYC-only dataset, so no borough filter is needed.
main(): transform -> stage table -> flag matching stores with has_tobacco
(Cloud SQL + PostGIS). A tobacco license is corroborating only: it sets the flag
on a geocode (~15m) OR join_key match, and never deletes.

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS,
optional SODA_TOKEN.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = "https://data.cityofnewyork.us/resource/adw8-wvxb.json"
PAGE = 50000
SELECT = ("license_nbr,address_building,address_street_name,address_zip,"
          "latitude,longitude")


def extract():
    where = "license_status='Active'"
    rows, off = [], 0
    while True:
        batch = requests.get(
            API,
            params={"$select": SELECT, "$where": where,
                    "$limit": PAGE, "$offset": off},
            headers={"X-App-Token": os.environ.get("SODA_TOKEN", "")},
            timeout=120,
        ).json()
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return pd.DataFrame(rows)


# Clear stale flags so re-runs reflect only currently-active licenses.
RESET = sqlalchemy.text("UPDATE stores SET has_tobacco = false WHERE has_tobacco;")

# Flag stores that match an active tobacco license by geocode (~15m) OR join_key.
TAG = sqlalchemy.text("""
    UPDATE stores s
    SET has_tobacco = true
    WHERE EXISTS (
        SELECT 1 FROM tobacco_stage st
        WHERE (st.join_key <> '' AND st.join_key = s.join_key)
           OR (st.lon IS NOT NULL AND s.geom IS NOT NULL
               AND ST_DWithin(
                     s.geom::geography,
                     ST_SetSRID(ST_MakePoint(st.lon::float8, st.lat::float8), 4326)::geography,
                     15))
    );
""")


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
            out.to_sql("tobacco_stage", cx, if_exists="replace", index=False)
            cx.execute(RESET)
            tagged = cx.execute(TAG).rowcount
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS tobacco_stage;"))
    finally:
        connector.close()
    print(f"tobacco: {len(out)} active licenses -> flagged {tagged} stores")


if __name__ == "__main__":
    main()
