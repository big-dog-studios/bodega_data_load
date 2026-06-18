#!/usr/bin/env bash
# Deploy the bodega-api Cloud Run service (read API + survey + image scan +
# submissions processor trigger POST /submissions/process).
# Run from repo root:  bash deploy.sh
# Backslash line-continuations are safe inside a script file (the paste-into-shell
# breakage only happens when copying a multi-line command into the prompt).
set -euo pipefail

PROJECT=mirror-250005
INSTANCE=mirror-250005:us-east1:bodega
REGION=us-east1

gcloud run deploy bodega-api \
  --source ./api \
  --region="$REGION" \
  --project="$PROJECT" \
  --allow-unauthenticated \
  --add-cloudsql-instances="$INSTANCE" \
  --set-secrets=ANTHROPIC_API_KEY=anthropic-api-key:latest,PROCESS_TOKEN=submissions-process-token:latest \
  --set-env-vars="INSTANCE=${INSTANCE},DB_NAME=bodega,DB_USER=admin,GCS_BUCKET=bodega-submissions,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_REGION=us-central1,DB_PASS=Terrysducks1!"
