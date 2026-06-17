"""One-time backfill: embed existing product names into embedding_dedup.
Run AFTER products_embedding_setup.sql. Idempotent -- only touches NULL rows,
so safe to re-run if interrupted. NAME-ONLY, matching the query side.

    python -m vision.backfill_embeddings "$DSN"
"""
import sys
from . import db, embed

PAGE = 500


def run(dsn: str):
    total = 0
    with db.connect(dsn) as conn:
        while True:
            rows = conn.execute(
                "SELECT product_id, name FROM products "
                "WHERE embedding_dedup IS NULL ORDER BY product_id LIMIT %s",
                (PAGE,),
            ).fetchall()
            if not rows:
                break
            ids = [r[0] for r in rows]
            vecs = embed.embed_texts([r[1] or "" for r in rows])
            for pid, v in zip(ids, vecs):
                conn.execute("UPDATE products SET embedding_dedup = %s WHERE product_id = %s",
                             (v, pid))
            conn.commit()
            total += len(rows)
            print(f"  embedded {total}")
    print(f"done: {total} products embedded")


if __name__ == "__main__":
    run(sys.argv[1])
