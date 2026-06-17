# api/ — read API + survey write path + image catalog scan

FastAPI Cloud Run **Service** (warm, not a Job). DB via the shared Connector
engine in `db.py`. Build context is `./api`, so `db.py`/`storage.py` are vendored
here (no cross-folder import).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/stores?bbox=W,S,E,N` | light pins in the viewport (+ flag filters) |
| GET  | `/stores/{license_number}` | one full record |
| GET  | `/stores/{license_number}/products` | catalog + category facets for one store |
| POST | `/submissions` | save one field survey + its photos (multipart) |
| POST | `/products/scan` | classify one receipt/shelf photo into `products` (multipart) |

## The image catalog scan path (`POST /products/scan`)

A `multipart/form-data` request with `license_number` (text) + `image` (file) →
the **`vision/` package** (`pipeline.process`) gates the image, extracts items
with Claude vision, dedups against the store's existing products, then inserts new
products / updates known prices. Response echoes `kind` (receipt|shelf|other),
`rejected_reason` (set iff gated out), and the `applied` / `review` item lists.

Unlike `/submissions`, `products.license_number` is a **hard FK** to `stores`
(`ON DELETE CASCADE`), so the store must already exist in the spine. The handler
checks via the read `engine` and returns **404** for an unknown license rather than
letting the pipeline's INSERT raise a FK violation as a 500. (Caveat: that CASCADE
means a food-stores/sla refresh that DELETEs & re-adds a store row also drops its
products — re-scan or re-derive after a spine refresh, same durability gotcha as the
SLA tags.)

The vision pipeline runs on **psycopg3 + pgvector** (pgvector needs psycopg3, which
the Cloud SQL Connector can't drive), so it opens its own connections from a libpq
**DSN** rather than the pg8000 `engine` the read path uses. The DSN points at the
Cloud SQL **unix socket** (`/cloudsql/$INSTANCE`); set `DB_DSN` to override locally.
`pipeline` is imported lazily inside the handler so the read path doesn't carry the
vision deps / `ANTHROPIC_API_KEY` / Vertex creds unless this route is actually hit.

**DB prerequisite:** the catalog tables (`products`, `subtype`, `category`, the
`v_products` view) plus `common/products_embedding_setup.sql` (adds the
`embedding_dedup vector(768)` column + `CREATE EXTENSION vector`) must be applied
before first scan; run `python -m vision.backfill_embeddings "$DSN"` once to embed
any pre-existing product names.

## The survey write path (single multipart POST)

The client sends **one** `multipart/form-data` request: the answer fields as form
parts, plus the photo files as file parts. The service streams each file to GCS
and stores only the object path on the row. No video, so the whole submission sits
well under Cloud Run's 32 MB request cap.

Form fields (all optional except `mode`):

| field | type | notes |
|---|---|---|
| `mode` | text | **required** — `"new"`, `"report"`, or `"delete"` |
| `license_number` | text | required when `mode="report"` or `"delete"`; ignored & minted (uuid) when `mode="new"` |
| `name` | text | surveyor-provided store name (esp. for `mode="new"`) |
| `house` / `street` / `city` / `zip` | text | address parts (mirror the spine; replaced the old free-text `address`) |
| `lat` / `lon` | float | client-supplied; `geom` POINT built only when **both** are present |
| `prepared_food` / `lottery` / `alcohol` / `tobacco` / `snap` | text | `"yes"`/`"no"` → bool |
| `atm` / `cat` | text | `"yes"`/`"no"` → bool |
| `hours` | text | free text |
| `receipt` | file | one receipt photo |
| `photos` | file (repeatable) | zero or more store photos |

`mode` distinguishes a survey of an existing spine store (`report`, keyed by its real
`license_number`) from a brand-new bodega not yet in the spine (`new`, where the server
mints a uuid `license_number`) from a closure report (`delete`, keyed by the existing
`license_number`, flagging the store as gone). All three land **only** in `submissions` —
we never write the `stores` spine, so `delete` is an advisory flag for downstream review,
not an actual spine deletion. `lat`/`lon` become `geom` via
`ST_SetSRID(ST_MakePoint(lon,lat),4326)` (the codebase's geom pattern); the response
echoes `license_number` so the client can capture the minted uuid.

The answer fields are coerced to typed boolean columns on insert (yes→true,
no→false, omitted→NULL) — named to mirror the spine's `has_*` flags so surveyor
answers diff directly against the government signal. Photo files become GCS object
paths in `receipt` (text) and `photos` (text[]); the bytes live in the bucket.

`submissions.license_number` is a **soft** reference — no FK, because the
food-stores/sla loaders DELETE & re-add `stores` rows on refresh and would cascade
survey data away. Join in SQL when reading.

## One-time GCP setup

```bash
# 1. Bucket for the photos (uniform access, your region)
gcloud storage buckets create gs://bodega-submissions --location=us-east1 \
  --uniform-bucket-level-access

# 2. Let the Cloud Run runtime SA write objects. That's it — no signing/token-creator
#    grant, because bytes flow THROUGH the service (multipart) rather than direct-to-GCS.
SA=PROJECT_NUMBER-compute@developer.gserviceaccount.com   # or your dedicated SA
gcloud storage buckets add-iam-policy-binding gs://bodega-submissions \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
```

(No bucket CORS needed — the browser/app never talks to GCS directly.)

## Deploy

```bash
gcloud run deploy bodega-api --source ./api --region=us-east1 \
  --add-cloudsql-instances=PROJECT:us-east1:INSTANCE \
  --set-env-vars=INSTANCE=PROJECT:us-east1:INSTANCE,DB_NAME=bodega,DB_USER=postgres,DB_PASS=YOURPASS,GCS_BUCKET=bodega-submissions \
  --set-secrets=ANTHROPIC_API_KEY=anthropic-api-key:latest
```

New env var vs the read-only deploy: **`GCS_BUCKET`**. New deps: `google-cloud-storage`,
`python-multipart`. Run the `submissions` table DDL (+ `pgcrypto`) from
`common/schema.sql` once before first write.

For the scan path (`/products/scan`) additionally:
- **`--add-cloudsql-instances`** mounts the `/cloudsql/$INSTANCE` socket the psycopg3
  pipeline connects through (the read engine's Connector doesn't need it; the pipeline
  does). Locally, set `DB_DSN` instead.
- **`ANTHROPIC_API_KEY`** (Secret Manager) for Claude vision; **Vertex AI** embeddings
  use the runtime service account's ADC (grant `roles/aiplatform.user`), so no key.
- New deps: `anthropic`, `psycopg[binary]`, `pgvector`, `numpy`, `google-cloud-aiplatform`.
- Apply the catalog DDL + `common/products_embedding_setup.sql` once (see scan section).
