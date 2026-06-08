"""Food stores loader entrypoint.

extract(): page the Retail Food Stores SODA API (9a8c-vfzj), filtered to the
five NYC boroughs.
main(): transform -> stage table -> upsert into `stores` (Cloud SQL + PostGIS).

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS,
optional SODA_TOKEN.
"""
import os
import requests
import pandas as pd
import sqlalchemy
from google.cloud.sql.connector import Connector
from transform import transform

API = "https://data.ny.gov/resource/9a8c-vfzj.json"
BOROS = ['BRONX', 'KINGS', 'NEW YORK', 'QUEENS', 'RICHMOND']
PAGE = 50000


def extract():
    where = "county in({})".format(",".join(f"'{b}'" for b in BOROS))
    rows, off = [], 0
    while True:
        batch = requests.get(
            API,
            params={"$where": where, "$limit": PAGE, "$offset": off},
            headers={"X-App-Token": os.environ.get("SODA_TOKEN", "")},
            timeout=120,
        ).json()
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return pd.DataFrame(rows)


UPSERT = sqlalchemy.text("""
    INSERT INTO stores (license_number, dba, entity, house, street, city, county,
                        zip, estab_type, bodega_confidence, join_key, geom)
    SELECT license_number, dba, entity, house, street, city, county, zip,
           estab_type, bodega_confidence, join_key,
           ST_SetSRID(ST_MakePoint(lon::float, lat::float), 4326)
    FROM stage WHERE lon IS NOT NULL
    ON CONFLICT (license_number) DO UPDATE SET
        dba = EXCLUDED.dba,
        entity = EXCLUDED.entity,
        house = EXCLUDED.house,
        street = EXCLUDED.street,
        city = EXCLUDED.city,
        county = EXCLUDED.county,
        zip = EXCLUDED.zip,
        estab_type = EXCLUDED.estab_type,
        bodega_confidence = EXCLUDED.bodega_confidence,
        join_key = EXCLUDED.join_key,
        geom = EXCLUDED.geom,
        ingested_at = now();
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
            out.to_sql("stage", cx, if_exists="replace", index=False)
            cx.execute(UPSERT)
    finally:
        connector.close()
    print(f"loaded {len(out)} candidate bodegas")


if __name__ == "__main__":
    main()
