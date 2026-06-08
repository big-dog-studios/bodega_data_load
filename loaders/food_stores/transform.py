"""Food stores transform logic — the layered bodega filter.

Input: a DataFrame of raw SODA rows from the Retail Food Stores dataset
(9a8c-vfzj). SODA field names are lowercased/underscored.
Output: filtered/normalized frame ready to load into `stores`.
"""
import re
import pandas as pd
from normalize import norm_house, norm_street, norm_zip, join_key, lonlat

BOROS = {'BRONX', 'KINGS', 'NEW YORK', 'QUEENS', 'RICHMOND'}

# Establishment-type letters that disqualify (warehouse/wholesale/processing).
# Keep a row only if its code contains 'A' (Store) and NONE of these.
DISQ = set('DEFGHILMNOPQRSTUVWZ')

# Chains / big-box / non-bodega food retailers — drop outright (high precision).
CHAINS = re.compile('|'.join(map(re.escape, [
    'TRADER JOE', 'WHOLE FOODS', 'KEY FOOD', 'C-TOWN', 'CTOWN', 'FOODTOWN',
    'GRISTEDE', 'DUANE READE', 'CVS', 'WALGREEN', 'RITE AID', '7-ELEVEN',
    '7 ELEVEN', 'DOLLAR TREE', 'DOLLAR GENERAL', 'TARGET', 'COSTCO', 'ALDI',
    'LIDL', 'STOP & SHOP', 'FINE FARE', 'FOOD BAZAAR', 'TRADE FAIR'])))
SPECIALTY = re.compile('|'.join(map(re.escape, [
    'PHARMACY', 'DRUG', ' RX', 'NUTRITION', 'BAKERY', 'CAFE', 'RESTAURANT',
    'LIQUOR', 'SEAFOOD', 'BUTCHER'])))


def is_retail(c):
    """Structural retail gate: contains 'A' (Store) and none of the DISQ letters."""
    c = (c or '').upper()
    return 'A' in c and not set(c) & DISQ


def transform(df):
    if df.empty:
        return df

    # 1. Borough gate.
    df = df[df['county'].str.upper().isin(BOROS)]
    # 2. Structural retail gate.
    df = df[df['estab_type'].apply(is_retail)]
    # 3. Name exclusions (chains + specialty).
    nm = df['dba_name'].fillna(df['entity_name']).fillna('').str.upper()
    df = df[~nm.str.contains(CHAINS) & ~nm.str.contains(SPECIALTY)]

    ll = df['georeference'].apply(lonlat)

    return pd.DataFrame({
        'license_number': df['license_number'],
        'dba': df['dba_name'],
        'entity': df['entity_name'],
        'house': df['street_number'],
        'street': df['street_name'],
        'city': df['city'],
        'county': df['county'],
        'zip': df['zip_code'],
        'estab_type': df['estab_type'],
        'lon': ll.map(lambda t: t[0]).values,
        'lat': ll.map(lambda t: t[1]).values,
        'join_key': [join_key(h, s, z) for h, s, z in
                     zip(df['street_number'], df['street_name'], df['zip_code'])],
    })
