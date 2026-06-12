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

# --- Name filter: four stages, in priority order ---------------------------
# The gov flags (tobacco/lottery/snap/alc_class) do NOT separate bodegas from
# supermarkets or restaurants — a full grocery store trips MORE flags, not fewer.
# So the discriminator is the DBA name. Matching is on a punctuation-normalized
# name with word boundaries, so "C TOWN" and "C-TOWN" both hit (the old list
# missed the spaced spelling and let all 53 C-Town rows through).

# 1. Chain blocklist — supermarket / gas / specialty brands. DROP unconditionally;
#    outranks keep-words so "CHERRY VALLEY MARKET" still drops. Cherry Valley uses
#    several suffixes (FARM/MARKET/MARKETPLACE/GOURMET DELI) — the prefix catches all.
_CHAINS = [
    # supermarket chains
    'CHERRY VALLEY', 'C TOWN', 'CTOWN', 'IDEAL FOOD BASKET', 'FOOD UNIVERSE',
    'BRAVO SUPERMARKET', 'SHOP FAIR', 'ASSOCIATED', 'FOOD EMPORIUM',
    'STOP SHOP',  # "STOP & SHOP" — & normalizes to a space, so no ampersand here
    'MORTON WILLIAMS', 'PIONEER', 'MET FOOD', 'MET FRESH', 'CITY FRESH MARKET',
    'LINCOLN MARKET', 'FOOD TOWN', 'FOODTOWN', 'UNION MARKET', 'H MART',
    'WESTSIDE MARKET', 'BROOKLYN FARE', 'FOOD DYNASTY', 'WESTERN BEEF',
    'BROOKLYN HARVEST', 'SUPER FRESH', 'SUPERFRESH', 'MARKET FRESH SUPERMARKET',
    'PREMIUM SUPERMARKET', 'FAIRWAY', 'D AGOSTINO', 'COMPARE FOODS',
    'GOURMET GARAGE', 'FOOD BAZAAR', 'KEY FOOD', 'GRISTEDE', 'GRISTEDES',
    'TRADE FAIR', 'FINE FARE', 'WHOLE FOODS', 'TRADER JOE', 'ALDI', 'LIDL',
    'TARGET', 'COSTCO', 'DOLLAR TREE', 'DOLLAR GENERAL', 'FAMILY DOLLAR', '7 ELEVEN',
    'DUANE READ', 'CVS', 'WALGREEN', 'RITE AID',
    # gas-station c-stores (NOT bare "BP" — collides with bodega initials)
    'MOBIL', 'SUNOCO', 'SHELL', 'EXXON', 'GULF', 'CITGO', 'SPEEDWAY', 'WAWA',
    'QUICK CHEK',
    # specialty / dessert / non-grocery chains
    'EDIBLE ARRANGEMENTS', 'NUTS FACTORY', 'BAKED BY MELISSA', 'NEUHAUS',
    'PASTOSA', 'TESOLIFE',
]
# 2. Hard non-bodega words — a dedicated pharmacy/liquor/butcher is never a bodega.
#    DROP even if a store-type word is present.
_HARD_KILL = ['PHARMACY', 'DRUG', 'DRUGS', 'RX', 'NUTRITION', 'LIQUOR',
              'SEAFOOD', 'BUTCHER']
# 3. Store-type keep-words — bodega vocabulary. KEEP regardless of any soft-kill
#    word (e.g. PARROT COFFEE GROCERY, VITALITY BAGELS & MARKET).
_KEEP = ['MARKET', 'GROCERY', 'GROCERIES', 'DELI', 'BODEGA', 'MINI MART',
         'MINIMART', 'CONVENIENCE', 'SMOKE']
# 4. Soft restaurant/cafe kill-words — DROP only when no keep-word present.
#    Deliberately excludes GRILL / BAGEL / DELI / KITCHEN: bodega-deli vocabulary.
_SOFT_KILL = ['SUSHI', 'COFFEE', 'CAFE', 'CAFETERIA', 'RESTAURANT', 'BAKERY',
              'RAMEN', 'THAI', 'POKE', 'BISTRO', 'CHOCOLATE', 'CHOCOLATIER',
              'DESSERT', 'DESSERTS', 'JUICE PRESS', 'JUICE BAR', 'NOODLE',
              'NOODLES', 'BURGER', 'BURGERS', 'TACO', 'TACOS', 'TAQUERIA',
              'PIZZA', 'PIZZERIA', 'CREAMERY', 'GELATO', 'SMOOTHIE',
              'SMOOTHIES', 'EATERY', 'DINER', 'BBQ', 'TRATTORIA']


def _rx(words, suffix=False):
    # suffix=True lets a stem also catch plural/suffixed forms by allowing trailing
    # word chars on the last token ("WALGREEN"->"WALGREENS", "KEY FOOD"->"KEY FOODS").
    # Only safe on the CHAINS drop-list; on KEEP it would match DELI->DELICIOUS and
    # wrongly keep cafes, so the other lists stay strict whole-word.
    tail = r'\w*\b' if suffix else r'\b'
    return re.compile(r'\b(?:' + '|'.join(map(re.escape, words)) + r')' + tail)


CHAINS = _rx(_CHAINS, suffix=True)
HARD_KILL, KEEP, SOFT_KILL = _rx(_HARD_KILL), _rx(_KEEP), _rx(_SOFT_KILL)


def keep_name(name):
    """Name filter: chain → hard-kill → keep-word → soft-kill → default keep."""
    n = re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 ]', ' ', (name or '').upper())).strip()
    if CHAINS.search(n) or HARD_KILL.search(n):
        return False
    if KEEP.search(n):
        return True
    return not SOFT_KILL.search(n)


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
    # 3. Name filter (chain → hard-kill → keep-word → soft-kill).
    nm = df['dba_name'].fillna(df['entity_name']).fillna('')
    df = df[nm.apply(keep_name)]

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
