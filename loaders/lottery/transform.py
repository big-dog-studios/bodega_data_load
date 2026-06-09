"""Lottery transform — normalize NYS Lottery Retailer rows for matching onto stores.

Input: raw SODA rows from the NYS Lottery Retailers dataset (2vvn-pdyi). Statewide
with no borough/county field, so we don't filter here — non-NYC retailers simply
won't match the NYC-only spine. `street` is combined; geo is lat/lon columns.
Output: a slim frame keyed for the join — one row per retailer:
  retailer, join_key, lon, lat, quick_draw.

Flagging against `stores` happens in job.py (SQL); lottery is corroborating only
(it sets has_lottery / has_quick_draw, never deletes).
"""
import pandas as pd
from normalize import split_address, join_key

COLS = ['retailer', 'street', 'zip', 'latitude', 'longitude', 'quick_draw']


def transform(df):
    if df.empty:
        return pd.DataFrame(
            columns=['retailer', 'join_key', 'lon', 'lat', 'quick_draw'])

    # SODA omits null fields per row (quick_draw is absent when blank), so a
    # column can be missing entirely.
    for c in COLS:
        if c not in df.columns:
            df[c] = None

    # Combined street address -> house/street (incl. the Queens grid hyphen case).
    hs = df['street'].apply(split_address)
    house = hs.map(lambda t: t[0])
    street = hs.map(lambda t: t[1])

    return pd.DataFrame({
        'retailer': df['retailer'],
        'join_key': [join_key(h, s, z) for h, s, z in zip(house, street, df['zip'])],
        'lon': pd.to_numeric(df['longitude'], errors='coerce').values,
        'lat': pd.to_numeric(df['latitude'], errors='coerce').values,
        'quick_draw': (df['quick_draw'] == 'Y').values,
    })
