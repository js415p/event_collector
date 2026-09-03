#!/usr/bin/env python3
"""
.env → Google Secret Manager로 Secrets 업로드 (LangGraph Cloud 배포용)

사용법:
  pip install google-cloud-secret-manager python-dotenv
  gcloud auth application-default login  # 또는 GOOGLE_APPLICATION_CREDENTIALS=...json
  python scripts/push_secrets.py --env .env --project game-event-agent

선택:
  --dry-run : 실제 생성 없이 목록만 출력
  --target cloud : Cloud 배포용 Env 평문 출력 (langgraph deploy --env-file 대안)

대상 Secrets (없으면 스킵):
  GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODEL,
  GEMINI_MIN_INTERVAL_SEC, GEMINI_MAX_RETRIES,
  TAVILY_API_KEY, TAVILY_MAX_RESULTS, TAVILY_SEARCH_DEPTH,
  GOOGLE_SERVICE_ACCOUNT_JSON (파일→문자열 자동 변환), GOOGLE_SERVICE_ACCOUNT_FILE,
  SHEET_ID, CALENDAR_ID, SHEET_TITLE, SHEET_TAB,
  CRON_SCHEDULE, TIMEZONE,
  LANGSMITH_API_KEY, LANGSMITH_PROJECT, LANGSMITH_ENDPOINT, LANGCHAIN_TRACING_V2
"""
import argparse
import os
import pathlib
from dotenv import dotenv_values

SECRET_KEYS = [
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "GEMINI_MIN_INTERVAL_SEC",
    "GEMINI_MAX_RETRIES",
    "TAVILY_API_KEY",
    "TAVILY_MAX_RESULTS",
    "TAVILY_SEARCH_DEPTH",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "SHEET_ID",
    "CALENDAR_ID",
    "SHEET_TITLE",
    "SHEET_TAB",
    "CRON_SCHEDULE",
    "TIMEZONE",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_TRACING_V2",
]

def load_env(env_path: str) -> dict:
    vals = dotenv_values(env_path)
    # GOOGLE_SERVICE_ACCOUNT_FILE → JSON 문자열 자동 주입
    sa_file = vals.get("GOOGLE_SERVICE_ACCOUNT_FILE") or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    sa_json = vals.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if (not sa_json or len(sa_json.strip()) < 10) and sa_file:
        p = pathlib.Path(sa_file)
        if not p.is_absolute():
            # env 파일 기준 상대경로
            p = pathlib.Path(env_path).parent / p
        if p.exists():
            vals["GOOGLE_SERVICE_ACCOUNT_JSON"] = p.read_text(encoding="utf-8").strip()
            print(f"Loaded GOOGLE_SERVICE_ACCOUNT_JSON from file: {p} ({len(vals['GOOGLE_SERVICE_ACCOUNT_JSON'])} chars)")
        else:
            print(f"Warning: GOOGLE_SERVICE_ACCOUNT_FILE not found: {p}")
    # 기본값 보정
    if not vals.get("LANGSMITH_PROJECT"):
        vals["LANGSMITH_PROJECT"] = "game event collector"
    if vals.get("LANGSMITH_API_KEY") and not vals.get("LANGCHAIN_TRACING_V2"):
        vals["LANGCHAIN_TRACING_V2"] = "true"
    return {k: v for k, v in vals.items() if k in SECRET_KEYS and v}

def push_to_secret_manager(project: str, secrets: dict, dry_run: bool = False):
    try:
        from google.cloud import secretmanager
    except ImportError:
        raise SystemExit("Missing google-cloud-secret-manager. Run: pip install google-cloud-secret-manager")

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"

    for key, value in secrets.items():
        # 빈 값 스킵 (예: SHARE_EMAIL 등)
        if not value or not str(value).strip():
            print(f"Skip empty {key}")
            continue
        secret_id = key  # Secret 이름 = Env 키와 동일
        name = f"{parent}/secrets/{secret_id}"
        # 1. Secret 존재 확인/생성
        try:
            client.get_secret(request={"name": name})
            print(f"Secret exists: {secret_id}")
        except Exception:
            if dry_run:
                print(f"[dry-run] would create secret: {secret_id}")
                continue
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            print(f"Created secret: {secret_id}")
        # 2. 새 버전 추가
        if dry_run:
            print(f"[dry-run] would add version for {secret_id} ({len(value)} chars)")
            continue
        payload = str(value).encode("utf-8")
        version = client.add_secret_version(
            request={"parent": name, "payload": {"data": payload}}
        )
        print(f"Added version {version.name} for {secret_id} ({len(value)} chars)")

    print(f"\nDone. Grant SA access if needed:")
    print(f"  gcloud secrets add-iam-policy-binding <SECRET> --member=serviceAccount:gdev-968@game-event-agent.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project={project}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env", help="Env file path")
    ap.add_argument("--project", default=os.getenv("GCP_PROJECT") or "game-event-agent", help="GCP project id")
    ap.add_argument("--dry-run", action="store_true", help="List only, do not create")
    ap.add_argument("--target", choices=["secretmanager", "cloud"], default="secretmanager", help="secretmanager or cloud (print env)")
    args = ap.parse_args()

    secrets = load_env(args.env)
    print(f"Loaded {len(secrets)} secrets from {args.env} for project {args.project}")
    for k in sorted(secrets):
        v = secrets[k]
        preview = v[:8] + "..." if k.endswith("_KEY") or k.endswith("_JSON") else v
        print(f"  {k}={preview} ({len(str(v))} chars)")

    if args.target == "cloud":
        print("\n# For LangGraph Cloud: set these as Environment Variables in deployment")
        for k, v in secrets.items():
            print(f"{k}={v}")
        return

    push_to_secret_manager(args.project, secrets, dry_run=args.dry_run)

if __name__ == "__main__":
    main()
