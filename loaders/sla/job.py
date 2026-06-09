"""SLA loader entrypoint.

extract(): page the SLA Active Licenses SODA API (9s3h-dpkz), filtered to the
five NYC boroughs (statewide dataset, county is mixed-case).
main(): transform -> stage table -> match onto `stores` and either DELETE the
store (disqualifying license) or tag it with `alc_class` (Cloud SQL + PostGIS).

Matching rules (see CLAUDE.md):
  - look up the license class in `sla_license_codes`.
  - not_bodega = true  -> DELETE the store, but only on a join_key match.
  - not_bodega = false -> set stores.alc_class = class_code on a geocode (~15m)
    OR join_key match.
  - delete wins over tag; classes absent from the lookup are ignored.

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS,
optional SODA_TOKEN.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = "https://data.ny.gov/resource/9s3h-dpkz.json"
BOROS = ['BRONX', 'KINGS', 'NEW YORK', 'QUEENS', 'RICHMOND']
PAGE = 50000
SELECT = ("licensepermitid,premisescounty,class,description,"
          "actualaddressofpremises,zipcode,georeference")


def extract():
    where = "upper(premisescounty) in ({})".format(
        ",".join(f"'{b}'" for b in BOROS))
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


# Clear stale tags so re-runs reflect only currently-matching licenses.
RESET = sqlalchemy.text("UPDATE stores SET alc_class = NULL WHERE alc_class IS NOT NULL;")

# not_bodega = true: drop the store, but only when the address (join_key) matches.
DELETE = sqlalchemy.text("""
    DELETE FROM stores s
    USING sla_stage st, sla_license_codes lc
    WHERE st.class_code = lc.class_code
      AND lc.not_bodega = true
      AND st.join_key <> ''
      AND st.join_key = s.join_key;
""")

# not_bodega = false: tag the surviving store with the license class. Match on
# geocode (~15m) OR join_key; prefer a grocery class, else the lowest class_code.
TAG = sqlalchemy.text("""
    UPDATE stores s
    SET alc_class = sub.class_code
    FROM (
        SELECT DISTINCT ON (s2.license_number)
               s2.license_number, st.class_code
        FROM stores s2
        JOIN sla_stage st ON (
              (st.join_key <> '' AND st.join_key = s2.join_key)
           OR (st.lon IS NOT NULL AND s2.geom IS NOT NULL
               AND ST_DWithin(
                     s2.geom::geography,
                     ST_SetSRID(ST_MakePoint(st.lon::float8, st.lat::float8), 4326)::geography,
                     15))
        )
        JOIN sla_license_codes lc
          ON lc.class_code = st.class_code AND lc.not_bodega = false
        ORDER BY s2.license_number,
                 (st.class_code IN (71, 81)) DESC,
                 st.class_code ASC
    ) sub
    WHERE s.license_number = sub.license_number;
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
            out.to_sql("sla_stage", cx, if_exists="replace", index=False)
            cx.execute(RESET)
            deleted = cx.execute(DELETE).rowcount
            tagged = cx.execute(TAG).rowcount
            cx.execute(sqlalchemy.text("DROP TABLE IF EXISTS sla_stage;"))
    finally:
        connector.close()
    print(f"sla: {len(out)} licenses -> deleted {deleted} stores, tagged {tagged}")


if __name__ == "__main__":
    main()
