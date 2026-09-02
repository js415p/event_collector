"""마감/종료 검증 — Python 날짜 + Gemini status 이중 필터."""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")


def _parse_date(s: str | None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.lower() in ("null", "none", "-", ""):
        return None
    # YYYY-MM-DD 우선
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            pass
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    # dateutil fallback
    try:
        from dateutil import parser as dparser
        return dparser.parse(s).date()
    except Exception:
        return None


def is_expired(event: dict, today: date | None = None) -> bool:
    """True면 마감/종료로 제외해야 함."""
    if today is None:
        today = datetime.now(SEOUL).date()

    # 1) Gemini status가 closed/cancelled면 즉시 제외
    status = (event.get("status") or "").strip().lower()
    if status in ("closed", "cancelled", "expired"):
        return True

    # 2) deadline 우선, 없으면 end_date로 판단
    deadline = _parse_date(event.get("deadline"))
    end_date = _parse_date(event.get("end_date"))
    start_date = _parse_date(event.get("start_date"))

    # deadline이 과거면 마감
    if deadline and deadline < today:
        return True
    # end_date가 과거면 종료
    if end_date and end_date < today:
        return True
    # 둘 다 없고 start_date만 있고 과거 30일 이상이면 종료로 간주 (오래된 행사)
    if not deadline and not end_date and start_date and (today - start_date).days > 30:
        return True

    return False


def filter_valid(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(valid, expired) 분리."""
    today = datetime.now(SEOUL).date()
    valid = []
    expired = []
    for ev in events:
        if is_expired(ev, today=today):
            expired.append(ev)
        else:
            # status가 비어있으면 upcoming으로 보정
            if not ev.get("status"):
                ev["status"] = "upcoming"
            valid.append(ev)
    logger.info(f"Validator: {len(events)} total -> {len(valid)} valid, {len(expired)} expired (today={today})")
    return valid, expired
