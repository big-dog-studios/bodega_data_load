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
  alc_class         integer,                        -- SLA license class_code (set by sla loader)
  ingested_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stores_geom_gix ON stores USING gist (geom);
CREATE INDEX IF NOT EXISTS stores_join_key_ix ON stores (join_key);

-- SLA license type lookup (3NF). Seeded from the LEAP decoder
-- (leap-license-type-and-class-definitions.xlsx); see seed_sla_license_codes.sql.
-- Natural key is (type_code, class_code) — Type is 1 across the file, Class is the
-- real discriminator. `not_bodega` is functionally dependent on the license type,
-- so it lives here once instead of being copied onto every license row.
CREATE TABLE IF NOT EXISTS sla_license_codes (
  type_code         integer NOT NULL,        -- LEAP "Type"
  class_code        integer NOT NULL,        -- LEAP "Class" — the specific license type
  class_description text NOT NULL,            -- e.g. 'Grocery Store', 'Liquor Store'
  product           text,                     -- 'Beer' | 'Wine' | 'Liquor' | 'Cider' | 'Mead' | NULL
  not_bodega        boolean NOT NULL DEFAULT false,  -- true = dedicated packaged-alcohol retailer (Wine/Liquor Store), exclude
  PRIMARY KEY (type_code, class_code)
);
