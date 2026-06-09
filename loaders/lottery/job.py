"""Lottery loader entrypoint.

extract(): pull the NYS Lottery Retailers SODA API (2vvn-pdyi). Statewide (~13k
rows, one page) with no borough field, so we don't filter — the join to the
NYC-only `stores` spine drops everything outside the city.
main(): transform -> stage table -> flag matching stores with has_lottery (and
has_quick_draw where the retailer offers Quick Draw). Corroborating only: it sets
flags on a geocode (~15m) OR join_key match, and never deletes.

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


def extract():
    rows, off = [], 0
    while True:
        batch = requests.get(
            API,
            params={"$select": SELECT, "$limit": PAGE, "$offset": off},
            headers={"X-App-Token": os.environ.get("SODA_TOKEN", "")},
            timeout=120,
        ).json()
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return pd.DataFrame(rows)


# Clear stale flags so re-runs reflect only current lottery retailers.
RESET = sqlalchemy.text(
    "UPDATE stores SET has_lottery = false, has_quick_draw = false "
    "WHERE has_lottery OR has_quick_draw;")

# A store matches a lottery retailer by geocode (~15m) OR join_key.
_MATCH = """
        SELECT 1 FROM lottery_stage st
        WHERE ({extra}
              (st.join_key <> '' AND st.join_key = s.join_key)
           OR (st.lon IS NOT NULL AND s.geom IS NOT NULL
               AND ST_DWithin(
                     s.geom::geography,
                     ST_SetSRID(ST_MakePoint(st.lon::float8, st.lat::float8), 4326)::geography,
                     15)))
"""
LOTTERY = sqlalchemy.text(
    f"UPDATE stores s SET has_lottery = true WHERE EXISTS ({_MATCH.format(extra='')});")
# Quick Draw is a subset: same match, restricted to retailers that offer it.
QUICK_DRAW = sqlalchemy.text(
    f"UPDATE stores s SET has_quick_draw = true "
    f"WHERE EXISTS ({_MATCH.format(extra='st.quick_draw AND')});")


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
            cx.execute(RESET)
            lottery = cx.execute(LOTTERY).rowcount
            quick = cx.execute(QUICK_DRAW).rowcount
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS lottery_stage;"))
    finally:
        connector.close()
    print(f"lottery: {len(out)} retailers -> flagged {lottery} stores "
          f"({quick} with quick draw)")


if __name__ == "__main__":
    main()
