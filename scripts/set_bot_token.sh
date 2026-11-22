#!/usr/bin/env bash
# Safely store DISCORD_BOT_TOKEN in GCP Secret Manager (keeping only 1 active version),
# roll Cloud Run so it loads the token, and register the guild slash commands.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT:-discord-verify-bot-jafer}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${CLOUD_RUN_SERVICE:-googlers-verify-bot}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${DISCORD_BOT_TOKEN:-}" ]]; then
  read -r -s -p "Paste Discord Bot Token (input hidden): " DISCORD_BOT_TOKEN
  echo ""
fi
if [[ -z "${DISCORD_BOT_TOKEN}" ]]; then
  echo "ERROR: Empty token." >&2
  exit 1
fi

echo "Reading existing INTERNAL_FORM_SECRET from Secret Manager..."
CURRENT_JSON=$(gcloud secrets versions access latest --secret=bot-secrets --project="$PROJECT_ID" --quiet)
OLD_VERSIONS=$(gcloud secrets versions list bot-secrets --filter="state=ENABLED" --format="value(name.basename())" --project="$PROJECT_ID" --quiet)

NEW_JSON=$(DISCORD_BOT_TOKEN="$DISCORD_BOT_TOKEN" CURRENT_JSON="$CURRENT_JSON" python3 -c '
import json, os
d = json.loads(os.environ["CURRENT_JSON"])
d["DISCORD_BOT_TOKEN"] = os.environ["DISCORD_BOT_TOKEN"].strip()
print(json.dumps(d), end="")
')

echo "Adding new secret version..."
printf "%s" "$NEW_JSON" | gcloud secrets versions add bot-secrets --data-file=- --project="$PROJECT_ID" --quiet

echo "Destroying old secret version(s) to stay within the 6-version Always Free tier..."
for v in $OLD_VERSIONS; do
  gcloud secrets versions destroy "$v" --secret=bot-secrets --project="$PROJECT_ID" --quiet >/dev/null || true
done

echo "Updating Cloud Run revision to load new secret..."
gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --update-env-vars="SECRET_UPDATED_AT=$(date -u +%Y%m%dT%H%M%SZ)" \
  --quiet

echo "Registering guild slash commands..."
DISCORD_BOT_TOKEN="$DISCORD_BOT_TOKEN" "${SCRIPT_DIR}/../.venv/bin/python" "${SCRIPT_DIR}/register_commands.py"

echo "Done! Next: paste the Cloud Run URL + /interactions into Discord Developer Portal -> General Information -> Interactions Endpoint URL, and turn OFF all 3 Privileged Gateway Intents on the Bot tab."
