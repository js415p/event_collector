"""Google Calendar — Service Account, events.insert/patch + dedup."""
import os
import hashlib
import logging
import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")


def _get_credentials():
    scopes = ["https://www.googleapis.com/auth/calendar"]
    import json
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
        logger.warning(f"Calendar credentials failed: {e}")
    return None


def _get_service():
    creds = _get_credentials()
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"Calendar build failed: {e}")
        return None


def _event_id(title: str, start_date: str, url: str) -> str:
    """Calendar eventId: 소문자, 5~1024자, a-z0-9- 만 허용. 해시 기반."""
    raw = f"{(title or '').strip().lower()}|{(start_date or '').strip()}|{(url or '').strip().lower()}"
    h = hashlib.sha1(raw.encode()).hexdigest()[:20]
    # prefix로 식별
    return f"evt{h}"


def _parse_date(s: str | None):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.lower() in ("null", "none", "-"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        try:
            from dateutil import parser as dparser
            return dparser.parse(s).date()
        except Exception:
            return None


def _build_event(ev: dict) -> dict:
    start = _parse_date(ev.get("start_date")) or _parse_date(ev.get("deadline"))
    end = _parse_date(ev.get("end_date"))
    if start is None:
        # 날짜 없으면 deadline 또는 오늘+7일
        start = datetime.now(SEOUL).date()
    if end is None:
        end = start + timedelta(days=1)
    # Calendar는 end가 exclusive (하루 종일 이벤트: end = 다음날)
    end_exclusive = end + timedelta(days=1)

    title = ev.get("title", "제목 없음")
    location = ev.get("location", "")
    url = ev.get("url", "")
    desc_parts = []
    if ev.get("description"):
        desc_parts.append(ev["description"])
    if url:
        desc_parts.append(f"링크: {url}")
    if ev.get("source"):
        desc_parts.append(f"출처: {ev['source']}")
    if ev.get("category"):
        desc_parts.append(f"카테고리: {ev['category']}")
    # 접수기간 표시 (강화)
    app_start = ev.get("application_start")
    app_end = ev.get("application_end") or ev.get("deadline")
    if app_start and app_end:
        desc_parts.append(f"접수기간: {app_start} ~ {app_end}")
    elif app_end:
        desc_parts.append(f"접수마감: {app_end}")
    elif app_start:
        desc_parts.append(f"접수시작: {app_start}")
    elif ev.get("deadline"):
        desc_parts.append(f"마감: {ev['deadline']}")
    # 행사 기간도 명시
    if ev.get("start_date") or ev.get("end_date"):
        period = f"{ev.get('start_date','')} ~ {ev.get('end_date','')}".strip(" ~")
        if period:
            desc_parts.append(f"행사기간: {period}")
    description = "\n".join(desc_parts)

    return {
        "summary": f"[{ev.get('category','event')}] {title}",
        "location": location,
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": end_exclusive.isoformat()},
        "transparency": "transparent",
        "source": {"url": url, "title": title} if url else None,
    }


def write_events(events: list[dict]) -> dict:
    """Calendar에 이벤트 생성. dedup: eventId 해시로 기존 여부 확인."""
    if not events:
        return {"inserted": 0, "skipped": 0, "error": None}

    svc = _get_service()
    calendar_id = os.getenv("CALENDAR_ID", "primary")

    if svc is None:
        logger.warning("No calendar credentials — dry-run")
        for ev in events[:5]:
            logger.info(f"[dry-run calendar] would create: {ev.get('title')} {ev.get('start_date')}")
        return {"inserted": 0, "skipped": 0, "error": "no_credentials_dry_run", "calendar_id": calendar_id}

    # 캘린더 존재 확인 — 공유된 그룹 캘린더는 calendarList에 없을 수 있어 insert 시도
    try:
        svc.calendarList().get(calendarId=calendar_id).execute()
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            # 공유는 됐지만 목록에 없으면 insert로 추가 (SA가 직접 목록에 등록)
            try:
                svc.calendarList().insert(body={"id": calendar_id}).execute()
                logger.info(f"Calendar {calendar_id} added to service account list via insert")
            except Exception as ie:
                logger.warning(f"Calendar {calendar_id} insert failed, falling back to primary: {ie}")
                calendar_id = "primary"
        else:
            logger.warning(f"Calendar {calendar_id} not found, falling back to primary: {e}")
            calendar_id = "primary"

    inserted = 0
    skipped = 0
    errors = []

    for ev in events:
        eid = _event_id(ev.get("title",""), ev.get("start_date","") or "", ev.get("url",""))
        body = _build_event(ev)
        body["id"] = eid
        # 429 백오프
        for attempt in range(3):
            try:
                # 존재 여부 확인
                try:
                    existing = svc.events().get(calendarId=calendar_id, eventId=eid).execute()
                    # 이미 있으면 스킵 (업데이트 필요 시 patch로 교체 가능)
                    logger.debug(f"Calendar event {eid} already exists, skip")
                    skipped += 1
                    break
                except Exception as ge:
                    if "404" not in str(ge) and "Not Found" not in str(ge):
                        # 404가 아니면 조회 실패로 간주하고 insert 시도
                        pass
                    # 없으면 insert
                    svc.events().insert(calendarId=calendar_id, body=body).execute()
                    inserted += 1
                    break
            except Exception as e:
                msg = str(e)
                is_rate = "429" in msg or "quota" in msg.lower() or "403" in msg
                if is_rate and attempt < 2:
                    time.sleep((2 ** attempt) * 5 + random.uniform(0, 1))
                    continue
                if "alreadyExists" in msg or "409" in msg:
                    skipped += 1
                    break
                logger.warning(f"Calendar insert failed for {ev.get('title')}: {e}")
                errors.append(str(e)[:200])
                break
        # 600/min/user 제한 보호: 이벤트 간 짧은 딜레이
        time.sleep(0.2)

    logger.info(f"Calendar: inserted={inserted} skipped={skipped} calendar={calendar_id}")
    return {"inserted": inserted, "skipped": skipped, "calendar_id": calendar_id, "errors": errors[:5] if errors else None}
