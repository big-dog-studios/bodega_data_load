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
  has_tobacco       boolean NOT NULL DEFAULT false,  -- DCWP tobacco license (set by tobacco loader)
  has_lottery       boolean NOT NULL DEFAULT false,  -- NYS lottery retailer (set by lottery loader)
  has_quick_draw    boolean NOT NULL DEFAULT false,  -- lottery + offers Quick Draw (subset of has_lottery)
  has_prepared_food boolean NOT NULL DEFAULT false,  -- DOHMH-inspected food prep on premises (set by dohmh loader)
  has_snap          boolean NOT NULL DEFAULT false,  -- USDA SNAP-authorized retailer (set by snap loader)
  has_atm           boolean,                         -- ATM on premises (no government feed — survey-only)
  has_cat           boolean,                         -- bodega cat present (survey-only)
  cat_name          text,                            -- the cat's name, if any (free text)
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

-- Crowdsourced field surveys (the API write path). A surveyor stands in a store,
-- confirms the flags, records hours, and attaches a receipt / photos / a video.
-- Distinct from the spine: this is human ground-truth, NOT a government feed.
-- gen_random_uuid() needs pgcrypto on PG<13; harmless to enable everywhere.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS submissions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  license_number  text NOT NULL,        -- SOFT ref to stores; no FK on purpose —
                                        -- food-stores/sla loaders DELETE & re-add
                                        -- store rows on refresh, which would cascade
                                        -- away survey data. Also lets a surveyor log
                                        -- a bodega not yet in the spine.
  -- The four survey answers, one typed column each (yes->true, no->false, omitted
  -- ->NULL). Named to mirror the spine's flags so a surveyor's answer diffs
  -- directly against the government signal (e.g. prepared_food vs stores.has_prepared_food).
  prepared_food   boolean,
  lottery         boolean,
  alcohol         boolean,
  tobacco         boolean,
  hours           text,
  receipt         text,                 -- GCS object path, or NULL — bytes live in the bucket
  photos          text[] NOT NULL DEFAULT '{}',  -- GCS object paths
  submitted_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submissions_license_ix ON submissions (license_number);
