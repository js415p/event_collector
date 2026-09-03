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


def _has_2025(event: dict) -> bool:
    """제목/날짜에 2025가 포함되면 2025 행사로 간주 (강화 검증)."""
    # 날짜 필드 year 체크
    for key in ("start_date", "end_date", "deadline", "application_start", "application_end"):
        d = _parse_date(event.get(key))
        if d and d.year == 2025:
            return True
        val = event.get(key)
        if isinstance(val, str) and "2025" in val:
            return True
    title = event.get("title") or ""
    desc = event.get("description") or ""
    # 제목/설명에 2025가 있고 2026이 없으면 2025 행사로 간주
    if "2025" in title and "2026" not in title:
        return True
    if "2025" in desc and "2026" not in desc and "2025" in title:
        return True
    # 설명/URL에 2025 흔적이 있으면 의심 — 외부 검증은 is_expired에서 별도 수행
    return False

def _has_2025_external(event: dict) -> bool:
    """날짜가 없는 경우 Tavily로 외부 검증: 2025 언급이 2026보다 많으면 2025로 간주."""
    # 날짜가 하나라도 있으면 외부 검증 스킵 (이미 _has_2025로 잡힘)
    for key in ("start_date", "end_date", "deadline", "application_start", "application_end"):
        if event.get(key):
            return False
    title = (event.get("title") or "").strip()
    if not title or "2026" in title:
        return False  # 2026이 제목에 있으면 2026으로 간주
    # 캐시
    cache = getattr(_has_2025_external, "_cache", {})
    if title in cache:
        return cache[title]
    try:
        import os
        from tavily import TavilyClient
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return False
        client = TavilyClient(api_key=api_key)
        res = client.search(title, max_results=3, include_answer=False)
        text = ""
        for r in res.get("results", [])[:2]:
            text += " " + (r.get("content") or "") + " " + (r.get("title") or "")
        cnt2025 = text.count("2025")
        cnt2026 = text.count("2026")
        is2025 = cnt2025 > 0 and cnt2025 > cnt2026
        cache[title] = is2025
        _has_2025_external._cache = cache
        if is2025:
            logger.info(f"External 2025 detected for '{title}': 2025={cnt2025}, 2026={cnt2026}")
        return is2025
    except Exception as e:
        logger.debug(f"External 2025 check failed for '{title}': {e}")
        return False

def is_expired(event: dict, today: date | None = None) -> bool:
    """True면 마감/종료로 제외해야 함."""
    if today is None:
        today = datetime.now(SEOUL).date()

    # 0) 2025년 행사는 무조건 제외 (강화)
    if _has_2025(event):
        return True
    # 0-1) 날짜 없는 경우 외부 검증 (Tavily)
    if _has_2025_external(event):
        return True

    # 1) Gemini status가 closed/cancelled면 즉시 제외
    status = (event.get("status") or "").strip().lower()
    if status in ("closed", "cancelled", "expired"):
        return True

    # 2) 접수기간/마감일/행사 종료일 중 하나라도 과거면 제외 (접수기간 강화)
    deadline = _parse_date(event.get("deadline")) or _parse_date(event.get("application_end"))
    app_start = _parse_date(event.get("application_start"))
    app_end = _parse_date(event.get("application_end"))
    end_date = _parse_date(event.get("end_date"))
    start_date = _parse_date(event.get("start_date"))

    # 접수 마감이 과거면 마감
    if deadline and deadline < today:
        return True
    if app_end and app_end < today:
        return True
    # 접수 시작이 없고 마감이 과거면 이미 종료 — 이미 위에서 처리
    # end_date가 과거면 종료
    if end_date and end_date < today:
        return True
    # start_date가 과거 90일 이상이면 오래된 행사로 제외 (기존 30일 → 90일로 완화하되 2025는 이미 위에서 걸러짐)
    # 대신, start_date가 오늘보다 30일 이상 과거이고 end_date/deadline이 없으면 제외
    if not deadline and not app_end and not end_date and start_date and (today - start_date).days > 30:
        return True
    # 접수기간이 모두 과거인데 행사일이 미래로 둔갑한 경우 방지: application_end가 과거면 위에서 이미 True
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
