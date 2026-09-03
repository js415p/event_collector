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


KR_KEYWORDS = [
    "한국","대한민국","서울","부산","광주","대전","인천","대구","울산","세종",
    "경기","강원","충북","충남","전북","전남","경북","경남","제주",
    "korea","seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","jeju",
]

def _is_domestic(ev: dict) -> bool:
    loc = (ev.get("location") or "").strip()
    loc_low = loc.lower()
    src = (ev.get("source") or "").lower()
    title = (ev.get("title") or "").lower()
    # 1순위: location에 국내 키워드가 있으면 국내
    for kw in KR_KEYWORDS:
        if kw.lower() in loc_low:
            return True
    # location이 오프라인인데 국내 키워드가 없으면, source/제목으로 재확인
    if loc.startswith("오프라인"):
        # 명백한 해외 지명이 location에 있으면 즉시 해외 (source .kr 여도 해외)
        overseas_hints = ["미국","일본","중국","독일","프랑스","영국","캐나다","호주","스웨덴","터키","이탈리아","스페인","브라질","러시아","태국","베트남","싱가포르","멕시코","인도","호주","멜버른","시애틀","도쿄","베이징","상하이","워싱턴","캘리포니아","보스턴","온타리오","이스탄불","멜버른","오리건","스웨덴","캐나다"]
        for oh in overseas_hints:
            if oh in loc:
                return False
        blob = f"{src} {title}"
        if ".kr" in src:
            return True
        for kw in KR_KEYWORDS:
            if kw.lower() in blob:
                return True
        # 한글 제목은 국내 가능성 높음
        if any('\uac00' <= ch <= '\ud7a3' for ch in ev.get("title","")):
            return True
        return False
    # 온라인이거나 빈 location은 소스/제목으로 보조 판정
    blob = f"{src} {title}"
    if ".kr" in src:
        return True
    for kw in KR_KEYWORDS:
        if kw.lower() in blob:
            return True
    if any('\uac00' <= ch <= '\ud7a3' for ch in ev.get("title","")):
        return True
    return False

def filter_overseas_offline(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """해외 오프라인 제외: 온라인은 유지, 국내 오프라인 유지, 해외 오프라인만 filtered."""
    keep: list[dict] = []
    filtered: list[dict] = []
    for ev in events:
        loc = (ev.get("location") or "").strip()
        is_online = "온라인" in loc
        # 혼합형(온라인 포함)은 유지
        if is_online:
            keep.append(ev)
            continue
        # location이 비어있으면 보수적으로 유지 (소스 기반 판단 불가 시)
        if not loc:
            keep.append(ev)
            continue
        is_domestic = _is_domestic(ev)
        # 오프라인 + 해외 → 제외
        if loc.startswith("오프라인") and not is_domestic:
            filtered.append(ev)
        else:
            keep.append(ev)
    logger.info(f"Overseas filter: {len(events)} -> keep {len(keep)}, filtered(해외 오프라인) {len(filtered)}")
    return keep, filtered


def _category_weight(ev: dict) -> float:
    cat = (ev.get("category") or "").lower()
    weights = {"jam": 1.0, "contest": 1.0, "competition": 0.9, "hackathon": 0.9, "conference": 0.8, "showcase": 0.8, "demo_day": 0.8, "other": 0.7}
    return weights.get(cat, 0.7)

def _boost_score(ev: dict) -> float:
    """충북대/연합동아리 가점 + relevance + category."""
    base = float(ev.get("relevance_score") or 0.5)
    blob = f"{ev.get('title','')} {ev.get('source','')} {ev.get('description','')}".lower()
    # 충북대 한정 가점
    if any(k in blob for k in ["충북대", "충북대학교", "cbnu", "chungbuk"]):
        base += 0.15
    # 연합동아리 전국 가점
    if "연합동아리" in blob or "연합 동아리" in blob:
        base += 0.15
    # 지자체/국가 가점 (약)
    if any(k in blob for k in ["한국콘텐츠진흥원", "콘진원", "문화체육관광부", "지자체"]):
        base += 0.05
    base += _category_weight(ev) * 0.1
    return min(base, 1.5)

def cap_events(events: list[dict], limit: int | None = None) -> tuple[list[dict], list[dict]]:
    """우선순위 정렬 후 limit(신규 추가 20건) 캡. 초과는 capped_skipped."""
    import os
    if limit is None:
        limit = int(os.getenv("WEEKLY_CAP", "20"))
    # 모든 미래 유지 + 정렬: start_date 가까운 순 → boost 높은 순
    def sort_key(ev):
        d = _parse_date(ev.get("start_date"))
        # None은 맨 뒤
        date_key = d or date.max
        return (date_key, -_boost_score(ev))
    ranked = sorted(events, key=sort_key)
    keep, skipped = ranked[:limit], ranked[limit:]
    logger.info(f"Cap: {len(events)} -> keep {len(keep)} (cap {limit}), skipped {len(skipped)}")
    return keep, skipped


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
