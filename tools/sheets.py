"""Google Sheets — Service Account, 자동 생성 + batchUpdate + dedup."""
import os
import json
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

HEADERS = ["id","title","category","start_date","end_date","deadline","location","url","source","status","discovered_at","calendar_event_id","last_updated"]
SHEET_TITLE = os.getenv("SHEET_TITLE", "게임 외부행사 모음 (자동수집)")
SHEET_TAB = os.getenv("SHEET_TAB", "events")

SEOUL = ZoneInfo("Asia/Seoul")


def _hash_id(title: str, start_date: str, url: str) -> str:
    raw = f"{(title or '').strip().lower()}|{(start_date or '').strip()}|{(url or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _get_credentials():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    try:
        from google.oauth2.service_account import Credentials
        if sa_json:
            info = json.loads(sa_json) if sa_json.strip().startswith("{") else json.load(open(sa_json, encoding="utf-8"))
            return Credentials.from_service_account_info(info, scopes=scopes)
        if os.path.exists(sa_file):
            return Credentials.from_service_account_file(sa_file, scopes=scopes)
    except Exception as e:
        logger.warning(f"Service Account load failed: {e}")
    return None


def _get_services():
    creds = _get_credentials()
    if creds is None:
        return None, None
    try:
        from googleapiclient.discovery import build
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        return sheets, drive
    except Exception as e:
        logger.warning(f"Google API build failed: {e}")
        return None, None


def ensure_sheet(sheets_service, drive_service) -> str | None:
    """SHEET_ID가 없으면 생성. 있으면 그대로 반환."""
    sheet_id = os.getenv("SHEET_ID")
    if sheet_id:
        return sheet_id
    if sheets_service is None:
        logger.warning("No sheets service — cannot create sheet")
        return None
    try:
        body = {
            "properties": {"title": SHEET_TITLE},
            "sheets": [{"properties": {"title": SHEET_TAB, "gridProperties": {"frozenRowCount": 1}}}],
        }
        created = sheets_service.spreadsheets().create(body=body).execute()
        new_id = created.get("spreadsheetId")
        logger.info(f"Created new sheet: {new_id} title={SHEET_TITLE}")

        # 헤더
        sheets_service.spreadsheets().values().update(
            spreadsheetId=new_id,
            range=f"{SHEET_TAB}!A1:M1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()

        # 헤더 서식 + 필터 + 고정
        try:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=new_id,
                body={
                    "requests": [
                        {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.8},
                                                                       "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
                                        "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
                        {"setBasicFilter": {"filter": {"range": {"sheetId": 0, "startRowIndex": 0}}}},
                        {"updateSheetProperties": {"properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                        {"autoResizeDimensions": {"dimensions": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 13}}},
                    ]
                },
            ).execute()
        except Exception as e:
            logger.warning(f"Sheet formatting failed (non-fatal): {e}")

        # 소유자는 Service Account — 필요 시 drive 권한 공유는 사용자가 Sheet를 직접 공유
        # drive_service로 권한 추가 시도 (선택)
        share_email = os.getenv("SHARE_EMAIL")
        if share_email and drive_service is not None:
            try:
                drive_service.permissions().create(
                    fileId=new_id, body={"type": "user", "role": "writer", "emailAddress": share_email},
                    sendNotificationEmail=False,
                ).execute()
                logger.info(f"Shared sheet {new_id} with {share_email}")
            except Exception as e:
                logger.warning(f"Share failed: {e}")

        # 사용자에게 ID 알림 (로그)
        logger.info(f"=== NEW SHEET CREATED === ID={new_id} URL=https://docs.google.com/spreadsheets/d/{new_id}")
        print(f"[sheets] NEW SHEET ID={new_id} -> set SHEET_ID in .env")
        return new_id
    except Exception as e:
        logger.error(f"Sheet creation failed: {e}")
        return None


def _ensure_tab(sheets_service, spreadsheet_id: str) -> int:
    """SHEET_TAB이 없으면 생성하고 헤더를 쓴다. sheetId 반환."""
    try:
        meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == SHEET_TAB:
                # 헤더가 비었으면 채움
                try:
                    hdr = sheets_service.spreadsheets().values().get(
                        spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A1:M1"
                    ).execute()
                    if not hdr.get("values"):
                        sheets_service.spreadsheets().values().update(
                            spreadsheetId=spreadsheet_id,
                            range=f"'{SHEET_TAB}'!A1:M1",
                            valueInputOption="RAW",
                            body={"values": [HEADERS]},
                        ).execute()
                except Exception:
                    pass
                return s["properties"]["sheetId"]
        # 없으면 생성
        logger.info(f"Tab '{SHEET_TAB}' not found — creating")
        resp = sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB, "gridProperties": {"frozenRowCount": 1}}}}]},
        ).execute()
        new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        # 헤더
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_TAB}'!A1:M1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        try:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [
                    {"repeatCell": {"range": {"sheetId": new_sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                                    "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.8},
                                                                   "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
                                    "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
                    {"setBasicFilter": {"filter": {"range": {"sheetId": new_sheet_id, "startRowIndex": 0}}}},
                    {"autoResizeDimensions": {"dimensions": {"sheetId": new_sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 13}}},
                ]},
            ).execute()
        except Exception as e:
            logger.warning(f"Tab formatting failed: {e}")
        return new_sheet_id
    except Exception as e:
        logger.warning(f"Ensure tab failed: {e}")
        return 0


def _load_existing_ids(sheets_service, spreadsheet_id: str) -> set[str]:
    # 탭 보장 후 로드
    _ensure_tab(sheets_service, spreadsheet_id)
    try:
        resp = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"'{SHEET_TAB}'!A2:A"
        ).execute()
        values = resp.get("values", [])
        return {row[0] for row in values if row and row[0]}
    except Exception as e:
        logger.warning(f"Load existing ids failed: {e}")
        return set()


def write_events(events: list[dict]) -> dict:
    """Dedup 후 Sheets에 append. 반환: {inserted, skipped}."""
    if not events:
        return {"inserted": 0, "skipped": 0, "sheet_id": None, "error": None}

    sheets_svc, drive_svc = _get_services()
    # 자격증명 없으면 dry-run (로그만)
    if sheets_svc is None:
        logger.warning("No credentials — dry-run mode, not writing to Sheets")
        preview = [{"id": _hash_id(e.get("title",""), e.get("start_date","") or "", e.get("url","")), **e} for e in events]
        for p in preview[:5]:
            logger.info(f"[dry-run] would insert: {p['title']} {p.get('start_date')} {p.get('url','')[:60]}")
        return {"inserted": 0, "skipped": 0, "sheet_id": None, "error": "no_credentials_dry_run", "preview": preview[:5]}

    spreadsheet_id = ensure_sheet(sheets_svc, drive_svc)
    if not spreadsheet_id:
        return {"inserted": 0, "skipped": len(events), "sheet_id": None, "error": "no_sheet_id"}

    existing_ids = _load_existing_ids(sheets_svc, spreadsheet_id)
    now_str = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")

    rows_to_append = []
    skipped = 0
    for ev in events:
        hid = _hash_id(ev.get("title",""), ev.get("start_date","") or "", ev.get("url",""))
        if hid in existing_ids:
            skipped += 1
            continue
        ev["id"] = hid
        # discovered_at/last_updated
        ev.setdefault("discovered_at", now_str)
        ev["last_updated"] = now_str
        row = [ev.get(h, "") or "" for h in HEADERS]
        # url 하이퍼링크는 텍스트로 유지 (Sheets에서 자동 링크)
        rows_to_append.append(row)

    if not rows_to_append:
        logger.info(f"Sheets: all {len(events)} events already exist — nothing to append")
        return {"inserted": 0, "skipped": skipped, "sheet_id": spreadsheet_id}

    # batch append (60/min 제한 대응: 한 번에) — 탭 보장 후
    _ensure_tab(sheets_svc, spreadsheet_id)
    try:
        # exponential backoff for 429
        import time, random
        for attempt in range(3):
            try:
                sheets_svc.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{SHEET_TAB}'!A:M",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": rows_to_append},
                ).execute()
                break
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    if attempt == 2:
                        raise
                    time.sleep((2 ** attempt) * 5 + random.uniform(0, 1))
                else:
                    raise
        logger.info(f"Sheets: inserted {len(rows_to_append)} rows into {spreadsheet_id}")
        return {"inserted": len(rows_to_append), "skipped": skipped, "sheet_id": spreadsheet_id}
    except Exception as e:
        logger.error(f"Sheets append failed: {e}")
        return {"inserted": 0, "skipped": skipped, "sheet_id": spreadsheet_id, "error": str(e)}
