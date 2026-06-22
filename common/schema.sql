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
  has_cat           boolean NOT NULL DEFAULT false,  -- bodega cat present (survey-only)
  cat_name          text,                            -- the cat's name, if any (free text)
  has_atm           boolean NOT NULL DEFAULT false,  -- ATM on premises (survey-only)
  has_wic           boolean DEFAULT false,           -- accepts WIC (survey-only)
  -- Google Places enrichment (set by the places loader; all nullable until enriched).
  place_id             text,                          -- Google Places resource id
  display_name         text,                          -- Places-formatted name
  phone                text,
  rating               numeric(2,1),                  -- 0.0–5.0
  user_rating_count    integer,
  accepts_credit_cards boolean,
  accepts_debit_cards  boolean,
  accepts_cash_only    boolean,
  accepts_nfc          boolean,                        -- contactless/tap payments
  takeout              boolean,
  delivery             boolean,
  hours_summary        text,
  ingested_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stores_geom_gix ON stores USING gist (geom);
CREATE INDEX IF NOT EXISTS stores_join_key_ix ON stores (join_key);

-- Migration for DBs created before has_wic existed (CREATE TABLE IF NOT EXISTS is a
-- no-op on them). Survey-only flag; nullable to match the live schema.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS has_wic boolean DEFAULT false;

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
                                        -- a bodega not yet in the spine. For mode='new'
                                        -- this is a minted uuid (no spine row exists yet).
  mode            text NOT NULL DEFAULT 'report' CHECK (mode IN ('new','report','delete')),
                                        -- 'new' = bodega not in the spine (license_number
                                        -- is a minted uuid); 'report' = survey against an
                                        -- existing spine store by its real license_number.
  name            text,                 -- surveyor-provided store name (esp. mode='new')
  address         text,                 -- DEPRECATED: legacy free-text address; superseded by
                                        -- house/street/city/zip below. Kept for old rows; not written.
  house           text,                 -- surveyor-provided address parts, mirroring the spine
  street          text,
  city            text,
  zip             text,
  geom            geometry(Point, 4326),-- from client lat/lon (NULL if not supplied)
  -- The five survey answers, one typed column each (yes->true, no->false, omitted
  -- ->NULL). Named to mirror the spine's flags so a surveyor's answer diffs
  -- directly against the government signal (e.g. prepared_food vs stores.has_prepared_food).
  prepared_food   boolean,
  lottery         boolean,
  alcohol         boolean,
  tobacco         boolean,
  snap            boolean,              -- accepts SNAP/EBT (vs stores.has_snap)
  atm             boolean,              -- ATM on premises (survey-only; no government feed)
  cat             boolean,              -- bodega cat present (survey-only)
  wic             boolean,              -- accepts WIC (vs stores.has_wic)
  hours           text,
  receipt         text,                 -- GCS object path, or NULL — bytes live in the bucket
  photos          text[] NOT NULL DEFAULT '{}',  -- GCS object paths
  submitted_ip    text,                 -- best-effort client IP (X-Forwarded-For first hop);
                                        -- SPOOFABLE — soft signal for dedup/abuse triage, never auth
  submitted_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submissions_license_ix ON submissions (license_number);

-- Migration for DBs created before mode/name/address/geom existed (CREATE TABLE
-- IF NOT EXISTS above is a no-op on them). All idempotent.
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS mode    text NOT NULL DEFAULT 'report';
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS name    text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS address text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS geom    geometry(Point, 4326);
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS submitted_ip text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS snap         boolean;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS wic          boolean;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS house        text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS street       text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS city         text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS zip          text;
-- Drop-and-recreate so the allowed-values list actually updates on live DBs (a plain
-- ADD CONSTRAINT no-ops when one already exists, leaving the old new/report-only check).
-- Two possible names: submissions_mode_chk (this migration) and submissions_mode_check
-- (the inline CHECK Postgres auto-names on a fresh CREATE TABLE).
ALTER TABLE submissions DROP CONSTRAINT IF EXISTS submissions_mode_chk;
ALTER TABLE submissions DROP CONSTRAINT IF EXISTS submissions_mode_check;
ALTER TABLE submissions ADD CONSTRAINT submissions_mode_chk CHECK (mode IN ('new','report','delete'));
CREATE INDEX IF NOT EXISTS submissions_geom_gix ON submissions USING gist (geom);
