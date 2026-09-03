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


def _event_id(title: str, start_date: str, url: str, suffix: str = "") -> str:
    """Calendar eventId: 소문자, 5~1024자, a-z0-9 만 허용(하이픈 불가 — 그룹 캘린더에서 400). 해시 기반.
    suffix: 'a' = 접수기간, 'e' = 본행사, '' = 레거시 단일 이벤트(마이그레이션용)
    """
    raw = f"{(title or '').strip().lower()}|{(start_date or '').strip()}|{(url or '').strip().lower()}"
    h = hashlib.sha1(raw.encode()).hexdigest()[:20]
    base = f"evt{h}"
    if suffix:
        return f"{base}{suffix}"
    return base


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


def _build_event(ev: dict, kind: str = "event") -> dict:
    """kind: 'application' = 접수기간, 'event' = 본행사"""
    title = ev.get("title", "제목 없음")
    location = ev.get("location", "")
    url = ev.get("url", "")
    category = ev.get("category", "event")

    if kind == "application":
        start = _parse_date(ev.get("application_start")) or _parse_date(ev.get("application_end")) or _parse_date(ev.get("deadline"))
        end = _parse_date(ev.get("application_end")) or _parse_date(ev.get("deadline")) or start
        if start is None:
            return None  # 접수기간 없으면 생성 안 함
        if end is None:
            end = start
        end_exclusive = end + timedelta(days=1)
        summary = f"[접수] {title}"
        # 접수기간 색상: 귤색(6, Tangerine) — 캘린더에서 눈에 띔
        color_id = "6"
        desc_parts = []
        if ev.get("description"):
            desc_parts.append(ev["description"])
        desc_parts.append(f"접수기간: {ev.get('application_start') or ''} ~ {ev.get('application_end') or ev.get('deadline') or ''}".strip(" ~"))
        if ev.get("start_date") or ev.get("end_date"):
            desc_parts.append(f"본행사: {ev.get('start_date','')} ~ {ev.get('end_date','')}".strip(" ~"))
        if url:
            desc_parts.append(f"링크: {url}")
        if ev.get("source"):
            desc_parts.append(f"출처: {ev['source']}")
        desc_parts.append(f"카테고리: {category}")
        description = "\n".join([p for p in desc_parts if p])
    else:
        start = _parse_date(ev.get("start_date"))
        end = _parse_date(ev.get("end_date"))
        if start is None:
            # 본행사 날짜 없으면 생성 안 함 (접수만 있는 경우)
            return None
        if end is None:
            end = start
        end_exclusive = end + timedelta(days=1)
        summary = f"[{category}] {title}"
        # 본행사 색상: 바질(10, Basil) — 녹색 계열, 접수와 대비
        color_id = "10"
        desc_parts = []
        if ev.get("description"):
            desc_parts.append(ev["description"])
        app_start = ev.get("application_start")
        app_end = ev.get("application_end") or ev.get("deadline")
        if app_start and app_end:
            desc_parts.append(f"접수기간: {app_start} ~ {app_end}")
        elif app_end:
            desc_parts.append(f"접수기간: ~ {app_end}")
        if url:
            desc_parts.append(f"링크: {url}")
        if ev.get("source"):
            desc_parts.append(f"출처: {ev['source']}")
        desc_parts.append(f"카테고리: {category}")
        desc_parts.append(f"본행사: {ev.get('start_date','')} ~ {ev.get('end_date','')}".strip(" ~"))
        description = "\n".join([p for p in desc_parts if p])

    return {
        "summary": summary,
        "location": location,
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": end_exclusive.isoformat()},
        "transparency": "transparent",
        "colorId": color_id,
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

    def _upsert_event(eid: str, body: dict):
        nonlocal inserted, skipped
        for attempt in range(3):
            try:
                try:
                    svc.events().get(calendarId=calendar_id, eventId=eid).execute()
                    logger.debug(f"Calendar event {eid} already exists, skip")
                    skipped += 1
                    return
                except Exception as ge:
                    if "404" not in str(ge) and "Not Found" not in str(ge):
                        pass
                    svc.events().insert(calendarId=calendar_id, body=body).execute()
                    inserted += 1
                    return
            except Exception as e:
                msg = str(e)
                is_rate = "429" in msg or "quota" in msg.lower() or "403" in msg
                if is_rate and attempt < 2:
                    time.sleep((2 ** attempt) * 5 + random.uniform(0, 1))
                    continue
                if "alreadyExists" in msg or "409" in msg:
                    skipped += 1
                    return
                logger.warning(f"Calendar insert failed for {body.get('summary')}: {e}")
                errors.append(str(e)[:200])
                return
        time.sleep(0.2)

    for ev in events:
        base_hash = _event_id(ev.get("title",""), ev.get("start_date","") or "", ev.get("url",""))
        # 레거시 단일 이벤트 정리: evt<hash> 가 있으면 삭제 (이제 2개로 분리됨)
        try:
            svc.events().get(calendarId=calendar_id, eventId=base_hash).execute()
            try:
                svc.events().delete(calendarId=calendar_id, eventId=base_hash).execute()
                logger.info(f"Deleted legacy event {base_hash} ({ev.get('title')})")
                time.sleep(0.2)
            except Exception:
                pass
        except Exception:
            pass

        # 1) 접수기간 이벤트 (주황, color 6)
        app_body = _build_event(ev, kind="application")
        if app_body:
            app_id = _event_id(ev.get("title",""), ev.get("start_date","") or "", ev.get("url",""), suffix="a")
            app_body["id"] = app_id
            _upsert_event(app_id, app_body)
            time.sleep(0.2)

        # 2) 본행사 이벤트 (초록, color 10)
        evt_body = _build_event(ev, kind="event")
        if evt_body:
            evt_id = _event_id(ev.get("title",""), ev.get("start_date","") or "", ev.get("url",""), suffix="e")
            evt_body["id"] = evt_id
            _upsert_event(evt_id, evt_body)
            time.sleep(0.2)

        # 둘 다 없으면 (날짜 전부 없음) — 기존 로직대로 최소 1개는 생성하지 않음
        if not app_body and not evt_body:
            logger.warning(f"Calendar skip: no dates for {ev.get('title')}")
            errors.append(f"No dates: {ev.get('title')}")

    logger.info(f"Calendar: inserted={inserted} skipped={skipped} calendar={calendar_id}")
    return {"inserted": inserted, "skipped": skipped, "calendar_id": calendar_id, "errors": errors[:5] if errors else None}
