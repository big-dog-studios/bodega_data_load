"""Tobacco transform — normalize DCWP tobacco license rows for matching onto stores.

Input: raw SODA rows from the DCWP Tobacco Retail Dealer Licenses dataset
(adw8-wvxb). NYC-only; address is pre-split and geo is separate lat/lon columns.
Output: a slim frame keyed for the join — one row per license:
  license_nbr, join_key, lon, lat.

Matching/flagging against `stores` happens in job.py (SQL); a tobacco license is
purely corroborating (it only sets has_tobacco, never deletes).
"""
import pandas as pd
from normalize import join_key

COLS = ['license_nbr', 'address_building', 'address_street_name',
        'address_zip', 'latitude', 'longitude']


def transform(df):
    if df.empty:
        return pd.DataFrame(columns=['license_nbr', 'join_key', 'lon', 'lat'])

    # SODA omits null fields per row, so a column can be absent entirely.
    for c in COLS:
        if c not in df.columns:
            df[c] = None

    # Address is already split into building/street, so feed join_key() directly.
    return pd.DataFrame({
        'license_nbr': df['license_nbr'],
        'join_key': [join_key(h, s, z) for h, s, z in
                     zip(df['address_building'], df['address_street_name'],
                         df['address_zip'])],
        'lon': pd.to_numeric(df['longitude'], errors='coerce').values,
        'lat': pd.to_numeric(df['latitude'], errors='coerce').values,
    })
