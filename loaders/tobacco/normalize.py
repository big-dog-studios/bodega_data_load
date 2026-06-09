"""Shared normalization helpers — the single source of truth for join_key.

Every loader uses these so that the same physical address produces the same
`join_key` across agencies. Loaders build from their own subfolder, so vendor a
copy of this file next to the loader's job.py (keep the copies identical).

join_key convention: "<house> <street> <zip5>"
  - norm_house:   leading digits only ('1477-1489' -> '1477')
  - norm_street:  uppercase, strip punctuation, abbreviate, strip ordinal suffixes
  - norm_zip:     first 5 digits
"""
import re


def _s(v):
    """Coerce to a clean string; missing/NaN -> ''."""
    return v if isinstance(v, str) else ''


def norm_house(s):
    """Leading digits only: '1477-1489' -> '1477'."""
    s = _s(s)
    m = re.match(r'\s*(\d+)', s)
    return m.group(1) if m else s


def norm_zip(s):
    """First 5 digits."""
    s = _s(s)
    m = re.match(r'(\d{5})', s)
    return m.group(1) if m else s


def norm_street(s):
    """Uppercase; strip punctuation; abbreviate; strip ordinal suffixes."""
    s = _s(s).upper()
    s = re.sub(r'[.,#]', ' ', s)
    s = re.sub(r'\bSTREET\b', 'ST', s)
    s = re.sub(r'\bAVENUE\b', 'AVE', s)
    s = re.sub(r'\bEAST\b', 'E', s)
    s = re.sub(r'\bWEST\b', 'W', s)
    s = re.sub(r'(\d+)(ST|ND|RD|TH)\b', r'\1', s)
    return re.sub(r'\s+', ' ', s).strip()


# Queens grid addresses ("97-10 32ND AVE") pair a cross-street number with a
# building number, then an ordinal street. Agencies that drop the hyphen write
# "97 10 32ND AVE", leaking the building number into the street. norm_house keeps
# only the first number, so drop the middle token and keep the ordinal street.
_QUEENS = re.compile(r'(\d+)[-\s]\d+[A-Z]?\s+(\d+(?:ST|ND|RD|TH)\b.*)$', re.I)


def split_address(addr):
    """Split a combined street address into (house, street).

    '115 CONTINUUM DR' -> ('115', 'CONTINUUM DR'),
    '97 10 32ND AVE'   -> ('97', '32ND AVE')  [Queens grid]. Sources like SLA give
    one address field; food_stores gives house/street separately. Both feed the
    same join_key() so they agree. Returns ('', addr) when there's no leading num.
    """
    addr = _s(addr).strip()
    m = _QUEENS.match(addr)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'(\d[\d-]*)\s+(.*)$', addr)
    return (m.group(1), m.group(2)) if m else ('', addr)


def join_key(house, street, zip_):
    """Normalized cross-source key. Empty when house or street is missing, so
    callers can skip address matching for rows that would only match on zip."""
    h, s, z = norm_house(house), norm_street(street), norm_zip(zip_)
    if not (h and s):
        return ''
    return f"{h} {s} {z}".strip()


def lonlat(v):
    """Extract (lon, lat) from a SODA point.

    Socrata returns either a dict {"type":"Point","coordinates":[lon,lat]} or
    WKT text 'POINT (lon lat)'. Returns (None, None) when absent.
    """
    if isinstance(v, dict):
        c = v.get('coordinates')
        if c and len(c) == 2:
            return c[0], c[1]
        return None, None
    m = re.search(r'POINT \(([-\d.]+) ([-\d.]+)\)', str(v if v is not None else ''))
    return (m.group(1), m.group(2)) if m else (None, None)
