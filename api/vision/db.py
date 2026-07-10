"""DB helpers for the vision classifier. psycopg (v3) + pgvector. Parameterized only."""
import psycopg
from functools import lru_cache
from pgvector.psycopg import register_vector


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn)
    register_vector(conn)        # so vector cols come back as numpy arrays and lists adapt in
    return conn


@lru_cache(maxsize=1)
def _allowed(dsn: str) -> tuple[str, ...]:
    with connect(dsn) as c:
        rows = c.execute("SELECT name FROM subtype ORDER BY name").fetchall()
    return tuple(r[0] for r in rows)


def allowed_codes(dsn: str) -> list[str]:
    return list(_allowed(dsn))


def subtype_id_map(dsn: str) -> dict[str, int]:
    with connect(dsn) as c:
        rows = c.execute("SELECT name, subtype_id FROM subtype").fetchall()
    return {name: sid for name, sid in rows}


def candidates_at_store(conn, license_number: str, subtype_id: int) -> list[dict]:
    """Existing products at this store in the same subtype, with their stored
    name-embeddings. This is the entire dedup comparison set (~5-30 rows)."""
    rows = conn.execute(
        "SELECT product_id, name, price_cents, embedding_dedup "
        "FROM products WHERE license_number = %s AND subtype_id = %s",
        (license_number, subtype_id),
    ).fetchall()
    return [{"product_id": r[0], "name": r[1], "price_cents": r[2], "embedding": r[3]}
            for r in rows]


def add_storefront_photo(conn, license_number, url) -> None:
    """Append a storefront/exterior photo path to stores.storefront_photos, deduped.
    Mirrors submissions.photos (a text[] of GCS paths). The NOT ... = ANY guard makes
    re-scanning the same image a no-op, and the stores UPDATE bumps updated_at so the
    new photo rides the next /sync/stores delta out to the client."""
    conn.execute(
        "UPDATE stores SET storefront_photos = array_append(storefront_photos, %s) "
        "WHERE license_number = %s AND NOT (%s = ANY(storefront_photos))",
        (url, license_number, url),
    )


def update_price(conn, product_id, price_cents, price_raw, source) -> None:
    conn.execute(
        "UPDATE products SET price_cents = %s, price_raw = %s, source = %s, ingested_at = now() "
        "WHERE product_id = %s",
        (price_cents, price_raw, source, product_id),
    )


def insert_product(conn, license_number, subtype_id, name, price_cents, price_raw,
                   source, source_category, embedding) -> int:
    """Insert a genuinely-new product (stage 3 ruled out a semantic match).
    ON CONFLICT DO NOTHING is ONLY an exact-race backstop -- it does NOT dedup,
    because names are similar-but-not-equal; that's stage 3's job."""
    row = conn.execute(
        "INSERT INTO products "
        "(license_number, subtype_id, name, price_cents, price_raw, source, "
        " source_category, embedding_dedup) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (license_number, source_category, name) DO NOTHING "
        "RETURNING product_id",
        (license_number, subtype_id, name, price_cents, price_raw, source,
         source_category, embedding),
    ).fetchone()
    if row:
        return row[0]
    existing = conn.execute(
        "SELECT product_id FROM products "
        "WHERE license_number = %s AND source_category = %s AND name = %s",
        (license_number, source_category, name),
    ).fetchone()
    return existing[0] if existing else None
