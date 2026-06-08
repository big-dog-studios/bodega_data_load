-- Cloud SQL (PostgreSQL + PostGIS) schema for the bodega spine.
-- Run once before the first load (Cloud SQL Studio or `gcloud sql connect`).

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS stores (
  license_number    text PRIMARY KEY,            -- natural key, idempotent refresh
  source            text DEFAULT 'ny_rfs:9a8c-vfzj',
  dba               text,
  entity            text,
  house             text,
  street            text,
  city              text,
  county            text,
  zip               text,
  geom              geometry(Point, 4326),
  estab_type        text,
  join_key          text,                          -- normalized, cross-source join
  ingested_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stores_geom_gix ON stores USING gist (geom);
CREATE INDEX IF NOT EXISTS stores_join_key_ix ON stores (join_key);
