-- ============================================================
--  products_embedding_setup.sql
--  Adds the dedup embedding column. Run ONCE, before backfill.
--  Dimension 768 = Vertex AI text-embedding-005 (default).
--  If you switch to a local model (e.g. BGE-M3 = 1024), change
--  the dimension here AND EMBED_DIM in vision/embed.py to match.
--
--  NOTE: NO ANN index (HNSW/IVFFlat). Dedup compares only the
--  ~5-30 rows for one store+subtype, so a brute-force scan over
--  that filtered set is instant; an index would be pure overhead.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE public.products
  ADD COLUMN IF NOT EXISTS embedding_dedup vector(768);

-- embedding_dedup is computed from NAME ONLY (both query and stored
-- side) so incoming image items -- which have no description -- compare
-- symmetrically. Search later gets its OWN context-rich column.
