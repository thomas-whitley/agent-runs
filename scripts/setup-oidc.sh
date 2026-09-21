#!/usr/bin/env bash
# Run this once, signed in with az. It creates an app registration that GitHub
# Actions signs in as, using a federated credential rather than a stored secret.
#
# It prints three values to put in the repo's Actions secrets:
#   AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
set -euo pipefail

APP_NAME="${APP_NAME:-agent-runs-deploy}"
REPO="${REPO:-thomas-whitley/agent-runs}"
RESOURCE_GROUP="${RESOURCE_GROUP:-agent-runs}"
LOCATION="${LOCATION:-australiaeast}"

subscription_id="$(az account show --query id --output tsv)"
tenant_id="$(az account show --query tenantId --output tsv)"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

client_id="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' --output tsv)"
if [ -z "$client_id" ]; then
  client_id="$(az ad app create --display-name "$APP_NAME" --query appId --output tsv)"
fi

if ! az ad sp show --id "$client_id" >/dev/null 2>&1; then
  az ad sp create --id "$client_id" --output none
fi

# GitHub may present either the plain subject or the immutable one, which
# embeds the numeric owner and repo ids. Which you get depends on the account,
# so register both rather than guessing. A mismatch here fails at sign in with
# AADSTS700213 and nothing else explains why.
owner="${REPO%%/*}"
repo_name="${REPO##*/}"
owner_id="$(gh api "/repos/${REPO}" --jq .owner.id)"
repo_id="$(gh api "/repos/${REPO}" --jq .id)"
immutable="repo:${owner}@${owner_id}/${repo_name}@${repo_id}:ref:refs/heads/main"

for subject in \
    "repo:${REPO}:ref:refs/heads/main" \
    "repo:${REPO}:environment:production" \
    "${immutable}"; do
  name="$(printf '%s' "$subject" | tr ':/@' '---' | cut -c1-120)"
  if ! az ad app federated-credential list --id "$client_id" \
        --query "[?subject=='${subject}']" --output tsv | grep -q .; then
    az ad app federated-credential create --id "$client_id" --parameters "{
      \"name\": \"${name}\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"${subject}\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }" --output none
  fi
done

# Assign by the service principal's object id, not the app id. Assigning by
# app id needs a directory lookup that fails while the principal is still
# propagating, and swallowing that leaves a deploy that cannot touch anything.
sp_object_id="$(az ad sp show --id "$client_id" --query id --output tsv)"
scope="/subscriptions/${subscription_id}/resourceGroups/${RESOURCE_GROUP}"

if ! az role assignment list --assignee-object-id "$sp_object_id" --scope "$scope" \
      --query "[?roleDefinitionName=='Contributor']" --output tsv | grep -q .; then
  az role assignment create \
    --assignee-object-id "$sp_object_id" \
    --assignee-principal-type ServicePrincipal \
    --role Contributor \
    --scope "$scope" \
    --output none
fi

echo "Put these in the repo's Actions secrets:"
echo "  AZURE_CLIENT_ID       ${client_id}"
echo "  AZURE_TENANT_ID       ${tenant_id}"
echo "  AZURE_SUBSCRIPTION_ID ${subscription_id}"
echo
echo "Then add DATABASE_URL, and MODEL_API_KEY if the loop should call a real model."
