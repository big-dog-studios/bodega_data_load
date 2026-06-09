"""SNAP transform — normalize USDA SNAP retailer rows for matching onto stores.

Input: ArcGIS feature attributes from the USDA SNAP retailer FeatureServer,
already filtered to the 5 NYC counties server-side in job.extract(). Address is one
combined field; geo is Latitude/Longitude (WGS84) — ignore the X/Y Web Mercator cols.
Output: a slim frame keyed for the join — one row per retailer:
  record_id, join_key, lon, lat.

Flagging against `stores` happens in job.py (sets has_snap, never deletes).
"""
import pandas as pd
from normalize import split_address, join_key

COLS = ['Record_ID', 'Store_Street_Address', 'Zip_Code', 'Latitude', 'Longitude']


def transform(df):
    if df.empty:
        return pd.DataFrame(columns=['record_id', 'join_key', 'lon', 'lat'])

    for c in COLS:
        if c not in df.columns:
            df[c] = None

    # Combined street address -> house/street (incl. the Queens grid hyphen case).
    hs = df['Store_Street_Address'].apply(split_address)
    house = hs.map(lambda t: t[0])
    street = hs.map(lambda t: t[1])

    return pd.DataFrame({
        'record_id': df['Record_ID'],
        'join_key': [join_key(h, s, z) for h, s, z in
                     zip(house, street, df['Zip_Code'])],
        'lon': pd.to_numeric(df['Longitude'], errors='coerce').values,
        'lat': pd.to_numeric(df['Latitude'], errors='coerce').values,
    })
