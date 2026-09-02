"""추출 레이어 — httpx+BS4 fetch 후 Gemini로 구조화."""
import logging
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

    # 배치로 Gemini 호출 (한 번에 4개씩 묶어 토큰 절약, 무료 RPD 보호)
    BATCH_SIZE = 4
    all_events: list[dict] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i+BATCH_SIZE]
        combined = "\n\n---\n\n".join(batch)
        events = generate_json(combined)
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
