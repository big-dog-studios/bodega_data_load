"""DOHMH transform — normalize restaurant-inspection rows for matching onto stores.

Input: rows from the DOHMH Restaurant Inspections dataset (43nn-pn8j), already
deduped to one row per establishment (camis) server-side in job.extract(). Address
is pre-split (building/street/zipcode); geo is lat/lon columns.
Output: a slim frame keyed for the join — one row per establishment:
  camis, join_key, lon, lat.

Presence in DOHMH means the establishment prepares/serves food on premises;
flagging against `stores` happens in job.py (sets has_prepared_food, never deletes).
"""
import pandas as pd
from normalize import join_key

COLS = ['camis', 'building', 'street', 'zipcode', 'latitude', 'longitude']


def transform(df):
    if df.empty:
        return pd.DataFrame(columns=['camis', 'join_key', 'lon', 'lat'])

    # SODA omits null fields per row, so a column can be absent entirely.
    for c in COLS:
        if c not in df.columns:
            df[c] = None

    # Address is already split into building/street, so feed join_key() directly.
    return pd.DataFrame({
        'camis': df['camis'],
        'join_key': [join_key(h, s, z) for h, s, z in
                     zip(df['building'], df['street'], df['zipcode'])],
        'lon': pd.to_numeric(df['longitude'], errors='coerce').values,
        'lat': pd.to_numeric(df['latitude'], errors='coerce').values,
    })
