"""SLA transform — normalize raw Liquor Authority rows for matching onto stores.

Input: raw SODA rows from the SLA Active Licenses dataset (9s3h-dpkz). SODA
field names are lowercased/concatenated (e.g. 'actualaddressofpremises').
Output: a slim frame keyed for the join — one row per license:
  licensepermitid, class_code (int), join_key, lon, lat.

Matching/flagging against `stores` happens in job.py (SQL); this file only
parses + normalizes so the keys agree with every other loader.
"""
import pandas as pd
from normalize import split_address, join_key, lonlat


COLS = ['licensepermitid', 'class', 'actualaddressofpremises', 'zipcode',
        'georeference']


def transform(df):
    if df.empty:
        return pd.DataFrame(
            columns=['licensepermitid', 'class_code', 'join_key', 'lon', 'lat'])

    # SODA omits null fields per row, so a column can be absent entirely.
    for c in COLS:
        if c not in df.columns:
            df[c] = None

    # SLA gives one combined address field; split into house + street so the
    # shared join_key() matches food_stores' separate house/street columns.
    hs = df['actualaddressofpremises'].apply(split_address)
    house = hs.map(lambda t: t[0])
    street = hs.map(lambda t: t[1])

    ll = df['georeference'].apply(lonlat)

    # Class is zero-padded text ('0071'); cast to int to match
    # sla_license_codes.class_code. Non-numeric classes -> NA (won't join).
    class_code = pd.to_numeric(df['class'], errors='coerce').astype('Int64')

    return pd.DataFrame({
        'licensepermitid': df['licensepermitid'],
        'class_code': class_code,
        'join_key': [join_key(h, s, z) for h, s, z in
                     zip(house, street, df['zipcode'])],
        'lon': pd.to_numeric(ll.map(lambda t: t[0]), errors='coerce').values,
        'lat': pd.to_numeric(ll.map(lambda t: t[1]), errors='coerce').values,
    })
