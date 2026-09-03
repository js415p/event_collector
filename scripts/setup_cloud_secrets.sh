#!/usr/bin/env bash
# .env → gcloud secrets로 업로드 (push_secrets.py 대안, gcloud CLI 필요)
# 사용: bash scripts/setup_cloud_secrets.sh [PROJECT_ID] [.env]
set -euo pipefail
PROJECT="${1:-game-event-agent}"
ENV_FILE="${2:-.env}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud not found. Install: https://cloud.google.com/sdk/docs/install"
  exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Env file not found: $ENV_FILE"
  exit 1
fi

# GOOGLE_SERVICE_ACCOUNT_FILE → JSON 문자열 처리
SA_FILE=$(grep -E "^GOOGLE_SERVICE_ACCOUNT_FILE=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' | head -1)
if [ -n "$SA_FILE" ] && [ -f "$SA_FILE" ]; then
  echo "Injecting GOOGLE_SERVICE_ACCOUNT_JSON from $SA_FILE"
  # 임시로 env에 추가 (gcloud는 파일 기반 secrets를 직접 지원하지 않으므로 문자열로)
  JSON_CONTENT=$(cat "$SA_FILE")
  # gcloud에 파일로 전달하기 위해 임시 파일
  TMP_JSON=$(mktemp)
  echo -n "$JSON_CONTENT" > "$TMP_JSON"
  # 아래 루프에서 GOOGLE_SERVICE_ACCOUNT_JSON을 TMP_JSON으로 처리
  export TMP_JSON
fi

KEYS=(
  GEMINI_API_KEY GEMINI_MODEL GEMINI_FALLBACK_MODEL
  TAVILY_API_KEY TAVILY_MAX_RESULTS
  SHEET_ID CALENDAR_ID SHEET_TITLE SHEET_TAB
  CRON_SCHEDULE TIMEZONE
  LANGSMITH_API_KEY LANGSMITH_PROJECT LANGSMITH_ENDPOINT LANGCHAIN_TRACING_V2
)

for KEY in "${KEYS[@]}"; do
  VAL=$(grep -E "^${KEY}=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' | head -1)
  # GOOGLE_SERVICE_ACCOUNT_JSON은 파일에서
  if [ "$KEY" = "GOOGLE_SERVICE_ACCOUNT_JSON" ] && [ -n "${TMP_JSON:-}" ]; then
    VAL=$(cat "$TMP_JSON")
  fi
  if [ -z "$VAL" ]; then
    echo "Skip $KEY (empty)"
    continue
  fi
  # LangSmith 기본값
  if [ "$KEY" = "LANGSMITH_PROJECT" ] && [ -z "$VAL" ]; then
    VAL="game event collector"
  fi

  if gcloud secrets describe "$KEY" --project="$PROJECT" >/dev/null 2>&1; then
    echo "Secret exists: $KEY"
  else
    echo "Creating secret: $KEY"
    gcloud secrets create "$KEY" --replication-policy=automatic --project="$PROJECT"
  fi
  echo -n "$VAL" | gcloud secrets versions add "$KEY" --data-file=- --project="$PROJECT"
  echo "Added version for $KEY"
done

if [ -n "${TMP_JSON:-}" ]; then rm -f "$TMP_JSON"; fi

echo "Done. Example grant:"
echo "  gcloud secrets add-iam-policy-binding GEMINI_API_KEY --member=serviceAccount:gdev-968@game-event-agent.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=$PROJECT"
