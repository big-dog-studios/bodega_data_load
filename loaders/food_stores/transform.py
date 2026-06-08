"""Food stores transform logic — the layered bodega filter.

Input: a DataFrame of raw SODA rows from the Retail Food Stores dataset
(9a8c-vfzj). SODA field names are lowercased/underscored.
Output: filtered/normalized frame ready to load into `stores`.
"""
import re
import pandas as pd

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

# Name tokens that bump confidence to 'high' (NOT a gate).
POS = re.compile(r'DELI|GROCERY|GROC|BODEGA|MINI ?MART|CONVENIENCE|CANDY|SMOKE|\bMART\b|CORNER')


def _s(v):
    """Coerce to a clean string; missing/NaN -> ''."""
    return v if isinstance(v, str) else ''


def _h(s):
    """norm_house: leading digits only ('1477-1489' -> '1477')."""
    s = _s(s)
    m = re.match(r'\s*(\d+)', s)
    return m.group(1) if m else s


def _z(s):
    """norm_zip: first 5 digits."""
    s = _s(s)
    m = re.match(r'(\d{5})', s)
    return m.group(1) if m else s


def _st(s):
    """norm_street: uppercase, strip punctuation, abbreviate, strip ordinals."""
    s = _s(s).upper()
    s = re.sub(r'[.,#]', ' ', s)
    s = re.sub(r'\bSTREET\b', 'ST', s)
    s = re.sub(r'\bAVENUE\b', 'AVE', s)
    s = re.sub(r'\bEAST\b', 'E', s)
    s = re.sub(r'\bWEST\b', 'W', s)
    s = re.sub(r'(\d+)(ST|ND|RD|TH)\b', r'\1', s)
    return re.sub(r'\s+', ' ', s).strip()


def _lonlat(v):
    """Extract (lon, lat) from a SODA point.

    Socrata returns the point either as a dict
    {"type":"Point","coordinates":[lon,lat]} or as WKT text 'POINT (lon lat)'.
    """
    if isinstance(v, dict):
        c = v.get('coordinates')
        if c and len(c) == 2:
            return c[0], c[1]
        return None, None
    m = re.search(r'POINT \(([-\d.]+) ([-\d.]+)\)', str(v if v is not None else ''))
    return (m.group(1), m.group(2)) if m else (None, None)


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
    df = df[df['establishment_type'].apply(is_retail)]
    # 3. Name exclusions (chains + specialty).
    nm = df['dba_name'].fillna(df['entity_name']).fillna('').str.upper()
    df = df[~nm.str.contains(CHAINS) & ~nm.str.contains(SPECIALTY)]

    # 4. Name tokens -> confidence (NOT a gate; medium survives).
    nm = df['dba_name'].fillna(df['entity_name']).fillna('').str.upper()
    confidence = nm.str.contains(POS).map({True: 'high', False: 'medium'})

    ll = df['georeference'].apply(_lonlat)

    return pd.DataFrame({
        'license_number': df['license_number'],
        'dba': df['dba_name'],
        'entity': df['entity_name'],
        'house': df['street_number'],
        'street': df['street_name'],
        'city': df['city'],
        'county': df['county'],
        'zip': df['zip_code'],
        'estab_type': df['establishment_type'],
        'lon': ll.map(lambda t: t[0]).values,
        'lat': ll.map(lambda t: t[1]).values,
        'bodega_confidence': confidence.values,
        'join_key': (df['street_number'].map(_h) + ' '
                     + df['street_name'].map(_st) + ' '
                     + df['zip_code'].map(_z)).values,
    })
