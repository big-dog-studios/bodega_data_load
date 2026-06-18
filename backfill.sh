#!/usr/bin/env bash
# One-time: embed existing product names into products.embedding_dedup.
# Runs vision/backfill_embeddings.py as a Cloud Run Job from the api/ image, so it
# reuses Vertex creds + the Cloud SQL socket (no local deps). Idempotent: only
# touches rows where embedding_dedup IS NULL, so safe to re-run.
# Run from repo root:  bash backfill.sh
set -euo pipefail

PROJECT=mirror-250005
INSTANCE=mirror-250005:us-east1:bodega
REGION=us-east1
DSN="host=/cloudsql/${INSTANCE} dbname=bodega user=admin password=Terrysducks1!"

gcloud run jobs deploy bodega-backfill \
  --source ./api \
  --region="$REGION" \
  --project="$PROJECT" \
  --add-cloudsql-instances="$INSTANCE" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_REGION=us-central1" \
  --command python \
  --args="-m,vision.backfill_embeddings,${DSN}"

gcloud run jobs execute bodega-backfill --region="$REGION" --project="$PROJECT" --wait
