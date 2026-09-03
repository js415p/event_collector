"""추출 레이어 — httpx+BS4 fetch 후 Gemini로 구조화."""
import logging
import re
import httpx
from bs4 import BeautifulSoup

from .gemini import generate_json

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 15
FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GameEventCollector/1.0; +https://example.com/bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko,en-US;q=0.9,en;q=0.8",
}


def _extract_dates_via_regex(text: str) -> list[str]:
    """원문에서 2026 날짜들을 정규식으로 추출 (YYYY-MM-DD로 정규화)."""
    # 패턴: 2026-06-08, 2026/06/08, 2026.06.08, 2026년 6월 8일, 2026년6월8일, 2026 06 08
    pattern = re.compile(r'2026\s*[.\-/년\s]+\s*(\d{1,2})\s*[.\-/월\s]+\s*(\d{1,2})')
    dates = []
    for m in pattern.finditer(text):
        try:
            # 앞의 2026은 고정, 뒤의 월/일은 캡처
            # 전체 매치에서 연도 포함 확인
            context = text[max(0, m.start()-6):m.end()+4]
            if "2026" not in context:
                continue
            month = int(m.group(1))
            day = int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                dates.append(f"2026-{month:02d}-{day:02d}")
        except Exception:
            continue
    # ISO 형식도 직접: 2026-06-08
    iso_pat = re.compile(r'2026-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])')
    for m in iso_pat.finditer(text):
        d = m.group(0)
        if d not in dates:
            dates.append(d)
    # 중복 제거, 순서 유지
    seen=set()
    uniq=[]
    for d in dates:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq

def _enrich_missing_dates(ev: dict, text: str) -> dict:
    """Gemini가 비운 날짜를 원문 정규식으로 보정."""
    # 이미 모두 있으면 스킵
    has_start = bool(ev.get("start_date"))
    has_end = bool(ev.get("end_date"))
    has_app_s = bool(ev.get("application_start"))
    has_app_e = bool(ev.get("application_end") or ev.get("deadline"))
    if has_start and has_end and has_app_e:
        return ev
    dates = _extract_dates_via_regex(text)
    if not dates:
        return ev
    # 접수기간 키워드 근처 날짜 우선
    app_keywords = ["접수", "신청", "모집", "지원"]
    event_keywords = ["행사", "대회", "교육", "진행", "일시", "기간"]
    # 텍스트에서 접수 키워드 근처 날짜 찾기
    app_dates = []
    event_dates = []
    for d in dates:
        # 해당 날짜 주변 30자 내에 키워드가 있는지
        idx = text.find(d.replace("-", ""))
        # ISO와 한글 형식이 달라 찾기 어렵 — 대신 정규식 매치 위치로 찾기
        # 간단히: 접수 키워드가 텍스트에 있으면 첫 2개는 접수, 나머지는 행사로 가정
        pass
    # 간단 휴리스틱: 날짜가 2개 이상이면 앞 2개는 접수, 뒤 2개는 행사
    # 예: 접수기간 2026-06-08 ~ 2026-06-25 → 2개 → application
    #     행사기간 2026-07-21 ~ 2026-08-20 → 2개 → event
    # 텍스트에 "접수"가 있으면 첫 2개를 접수에 할당
    has_app_keyword = any(kw in text for kw in app_keywords)
    if has_app_keyword:
        if not has_app_s and len(dates) >= 1:
            ev["application_start"] = dates[0]
        if not has_app_e and len(dates) >= 2:
            ev["application_end"] = dates[1]
            ev["deadline"] = dates[1]
        elif not has_app_e and len(dates) == 1:
            ev["application_end"] = dates[0]
            ev["deadline"] = dates[0]
        # 남은 날짜가 있으면 행사기간으로
        remaining = dates[2:] if len(dates) > 2 else []
        if remaining:
            if not has_start:
                ev["start_date"] = remaining[0]
            if not has_end and len(remaining) >= 2:
                ev["end_date"] = remaining[1]
            elif not has_end and len(remaining) == 1:
                ev["end_date"] = remaining[0]
        else:
            # 접수 날짜만 있고 행사 날짜 없으면, 접수 2개를 행사로도 사용하지 않음 — 그대로 둠
            # 단, 행사 날짜가 비어있고 접수 날짜가 있으면 행사 시작을 접수 마감 다음날로 추정하지 않음
            pass
    else:
        # 접수 키워드 없으면 날짜를 행사기간으로
        if not has_start and len(dates) >= 1:
            ev["start_date"] = dates[0]
        if not has_end and len(dates) >= 2:
            ev["end_date"] = dates[1]
        # 접수 마감도 채움 (행사 시작 전이 접수 마감이므로)
        if not has_app_e and len(dates) >= 1:
            # 가장 이른 날짜를 접수 마감으로? 보수적으로 첫 날짜를 사용
            ev["application_end"] = dates[0]
            ev["deadline"] = dates[0]
    # 로깅
    if not has_app_e and ev.get("application_end"):
        logger.info(f"Enriched {ev.get('title','')[:30]} with app_end {ev['application_end']} from regex")
    return ev

def fetch_url(url: str) -> str:
    """URL fetch → 텍스트 추출 (BS4). 실패 시 빈 문자열."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, headers=FETCH_HEADERS, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                return resp.text[:12000]
            soup = BeautifulSoup(resp.text, "lxml")
            # 스크립트/스타일 제거
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # 제목 + 메타 추가
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            meta_desc = ""
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                meta_desc = md["content"]
            combined = f"Title: {title}\nURL: {url}\nDescription: {meta_desc}\n\n{text[:10000]}"
            return combined
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return ""


def extract_events(search_results: list[dict], max_fetch: int = 12) -> list[dict]:
    """검색 결과 → fetch → Gemini 추출 → 병합/정규화."""
    if not search_results:
        return []

    # fetch 대상 선정: Tavily content가 짧으면 fetch, 충분하면 바로 Gemini에 전달
    texts: list[str] = []
    fetch_count = 0
    for r in search_results:
        content = r.get("content", "")
        url = r.get("url", "")
        title = r.get("title", "")
        # content가 500자 이상이면 fetch 생략 가능 (토큰 절약)
        if len(content) >= 500 or not url or fetch_count >= max_fetch:
            texts.append(f"Title: {title}\nURL: {url}\nContent: {content}")
        else:
            fetched = fetch_url(url)
            if fetched:
                texts.append(fetched)
                fetch_count += 1
            else:
                texts.append(f"Title: {title}\nURL: {url}\nContent: {content}")

    # Gemini 호출 — 정확도를 위해 1개씩 처리 (배치 시 날짜 누락 발생)
    # 무료 RPD 1,000 내, 주 71건 *1회 = 71회로 충분
    all_events: list[dict] = []
    for idx, text in enumerate(texts):
        events = generate_json(text)
        # Gemini가 2025를 2026으로 둔갑시키거나 날짜를 비우는 경우, 원문에서 정규식으로 보정
        for ev in events:
            # 기본 정규화
            ev.setdefault("url", "")
            ev.setdefault("source", "")
            if not ev.get("source") and ev.get("url"):
                try:
                    from urllib.parse import urlparse
                    ev["source"] = urlparse(ev["url"]).netloc
                except Exception:
                    pass
            # 누락된 날짜를 원문 텍스트에서 정규식으로 보정
            ev = _enrich_missing_dates(ev, text)
            # relevance 필터: other 카테고리는 0.7 이상만
            if ev.get("category") == "other" and float(ev.get("relevance_score", 0)) < 0.7:
                continue
            all_events.append(ev)

    # url 기반 1차 dedup (메모리)
    seen = set()
    deduped = []
    for ev in all_events:
        key = (ev.get("title", "").strip().lower(), ev.get("url", "").strip())
        if key not in seen:
            seen.add(key)
            deduped.append(ev)

    logger.info(f"Extracted {len(all_events)} raw -> {len(deduped)} after in-memory dedup (fetched {fetch_count})")
    return deduped
