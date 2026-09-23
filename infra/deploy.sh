#!/usr/bin/env bash
# Create the resource group if needed and deploy main.bicep into it.
# Secrets come from the environment and are never printed.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-agent-runs}"
LOCATION="${LOCATION:-australiaeast}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-agent-runs}"

: "${IMAGE:?set IMAGE to the container image, including its tag}"
: "${DATABASE_URL:?set DATABASE_URL to the Supabase session pooler string}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --template-file "$here/main.bicep" \
  --parameters \
      image="$IMAGE" \
      databaseUrl="$DATABASE_URL" \
      model="${MODEL:-stub}" \
      modelBaseUrl="${MODEL_BASE_URL:-}" \
      modelApiKey="${MODEL_API_KEY:-}" \
      voyageApiKey="${VOYAGE_API_KEY:-}" \
      mercuryBearerToken="${MERCURY_BEARER_TOKEN:-}" \
      mercuryConfigB64="${MERCURY_CONFIG_B64:-}" \
  --output none

az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DEPLOYMENT_NAME" \
  --query properties.outputs.apiUrl.value \
  --output tsv
