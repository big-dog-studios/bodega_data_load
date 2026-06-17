#!/usr/bin/env bash
# Call the /products/scan endpoint through the gateway.
# Usage:  bash scan.sh <license_number> <gcs_path>
# Example: bash scan.sh 715943 "unnamed (18).webp"
#   gcs_path is the object path inside the bodega-submissions bucket (or a full
#   gs://bucket/object URI). Use the DECODED name (real space, not %20).
set -euo pipefail

LICENSE="${1:?usage: bash scan.sh <license_number> <gcs_path>}"
GCS_PATH="${2:?usage: bash scan.sh <license_number> <gcs_path>}"

curl --location --request POST 'https://bodega-gateway-2q60deae.ue.gateway.dev/products/scan' \
  --header 'x-api-key: AIzaSyA31lUH4vxxewg7Jgoj8kZS9wJ0p9O0NKs' \
  --form "license_number=${LICENSE}" \
  --form "gcs_path=${GCS_PATH}"
echo
