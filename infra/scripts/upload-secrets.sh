#!/usr/bin/env bash
# Upload secret values from local .env into GCP Secret Manager.
# Reads infra/config.yaml for the env-var → secret-ID mapping, then for each
# entry: takes the value from .env and pushes it to the named GCP secret.
#
# Run this once after `terraform apply` (which creates the secret resources).
# Re-run any time you rotate a secret value.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/infra/config.yaml"
ENV_FILE="$REPO_ROOT/.env"

[ -f "$ENV_FILE" ] || { echo "error: $ENV_FILE not found" >&2; exit 1; }
[ -f "$CONFIG_FILE" ] || { echo "error: $CONFIG_FILE not found" >&2; exit 1; }
command -v yq >/dev/null || { echo "error: yq is required (brew install yq)" >&2; exit 1; }
command -v gcloud >/dev/null || { echo "error: gcloud is required" >&2; exit 1; }

PROJECT_ID="$(yq -r '.gcp_project_id' "$CONFIG_FILE")"
gcloud config set project "$PROJECT_ID" >/dev/null

# Source .env so its keys become shell variables.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# For each "env_var: secret_id" pair in config.yaml, push $env_var's value to
# the named secret.
while IFS=$'\t' read -r env_var secret_id; do
  value="${!env_var-}"
  if [ -z "$value" ]; then
    echo "skip: \$$env_var is empty in .env (would create empty secret $secret_id)" >&2
    continue
  fi
  printf '%s' "$value" | gcloud secrets versions add "$secret_id" --data-file=- >/dev/null
  echo "uploaded: $env_var → $secret_id"
done < <(yq -r '.secrets | to_entries | .[] | .key + "\t" + .value' "$CONFIG_FILE")

echo
echo "Done. To verify: gcloud secrets list --project=$PROJECT_ID"
