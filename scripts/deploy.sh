#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GCP_PROJECT:-discord-verify-bot-jafer}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${CLOUD_RUN_SERVICE:-googlers-verify-bot}"
SA_EMAIL="verify-bot-sa@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/verify-bot/bot:latest"

gcloud builds submit --tag "$IMAGE" --project="$PROJECT_ID" --quiet

gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --service-account="$SA_EMAIL" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --memory=512Mi \
  --cpu=1 \
  --concurrency=20 \
  --timeout=60 \
  --set-secrets="APP_SECRETS_JSON=bot-secrets:latest" \
  --quiet

echo "Deployed $(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)' --quiet)"
