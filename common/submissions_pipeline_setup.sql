-- ============================================================
--  submissions pipeline — full schema. Idempotent. Run once.
-- ============================================================
BEGIN;

-- new table: hand-off to the vision classifier
CREATE TABLE IF NOT EXISTS public.image_queue (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  submission_id uuid REFERENCES public.submissions(id),
  license_number text,
  url           text NOT NULL,
  kind_hint     text,                              -- 'receipt' | 'shelf'
  status        text NOT NULL DEFAULT 'pending',   -- pending | processed | failed
  enqueued_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  CONSTRAINT image_queue_url_key UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS image_queue_status_ix ON public.image_queue (status);

-- submissions: lifecycle columns the processor writes
ALTER TABLE public.submissions
  ADD COLUMN IF NOT EXISTS status       text NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS resolution   text,
  ADD COLUMN IF NOT EXISTS processed_at timestamptz;

DO $$ BEGIN
  ALTER TABLE public.submissions ADD CONSTRAINT submissions_status_chk
    CHECK (status IN ('pending','accepted','rejected','duplicate','superseded'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS submissions_status_ix ON public.submissions (status);

-- stores: crowdsource support
-- (no "verified" flag -- presence in stores IS verification)
ALTER TABLE public.stores
  ADD COLUMN IF NOT EXISTS hidden bool NOT NULL DEFAULT false;  -- suppress from results (closed or misclassified -- we don't know which)

-- fuzzy store-name dedup (dba OR display_name)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS stores_dba_trgm_ix
  ON public.stores USING gin (lower(dba) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS stores_displayname_trgm_ix
  ON public.stores USING gin (lower(display_name) gin_trgm_ops);

COMMIT;
