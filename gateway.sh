#!/usr/bin/env bash
# Republish the API Gateway config from openapi-gateway.yaml.
# Run from repo root:  bash gateway.sh
# Configs are immutable, so we mint a fresh timestamped name each run, then point
# the gateway at it.
set -euo pipefail

PROJECT=mirror-250005
CFG="bodega-cfg-$(date +%Y%m%d-%H%M%S)"

gcloud api-gateway api-configs create "$CFG" \
  --api=bodega-api-gw \
  --openapi-spec=openapi-gateway.yaml \
  --backend-auth-service-account=bodega-gw-invoker@mirror-250005.iam.gserviceaccount.com \
  --project="$PROJECT"

gcloud api-gateway gateways update bodega-gateway \
  --api=bodega-api-gw \
  --api-config="$CFG" \
  --location=us-east1 \
  --project="$PROJECT"
