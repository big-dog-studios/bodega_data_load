#!/usr/bin/env bash
# Show recent bodega-api logs.  Usage: bash logs.sh
set -euo pipefail
gcloud run services logs read bodega-api --region=us-east1 --project=mirror-250005 --limit=60
