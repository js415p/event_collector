"""검색 레이어 — Tavily 우선, 없으면 Gemini grounding fallback."""
import os
import time
import random
import logging

logger = logging.getLogger(__name__)

# 16개 쿼리: KO 10 + EN 6 (충북대 한정 + 연합동아리 전국 + 국가/지자체 + 글로벌)
DEFAULT_QUERIES = [
    # 기존 4
    "한국 게임잼 2026 참가 모집",
    "게임 공모전 2026 대학생",
    "한국 게임 컨퍼런스 2026 일정",
    "게임 개발 대회 2026",
    # 신규: 충북대 한정 (2)
    "충북대학교 게임잼 공모전 2026",
    "충북대 CBNU 게임 개발 대회 2026",
    # 신규: 연합동아리 전국 (1)
    "전국 연합동아리 게임잼 해커톤 2026",
    # 신규: 국가/지자체 (3)
    "문화체육관광부 한국콘텐츠진흥원 게임 공모전 2026",
    "지자체 게임 공모전 2026 서울 부산 경기",
    "대학생 게임 개발 대회 2026 위비티 콘테스트코리아",
    # EN 6
    "game jam 2026 submissions open",
    "indie game competition 2026 university",
    "game conference 2026 schedule",
    "hackathon game dev 2026",
    "university game jam contest 2026 Korea",
    "government indie game contest 2026",
]

# Tavily 무료: 1,000회/월
TAVILY_MAX_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS", "5"))
TAVILY_SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "basic")  # basic / advanced


def _tavily_search(query: str) -> list[dict]:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        resp = client.search(
            query=query,
            max_results=TAVILY_MAX_RESULTS,
            search_depth=TAVILY_SEARCH_DEPTH,
            include_answer=False,
        )
        results = []
        for r in resp.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0),
                "query": query,
            })
        return results
    except Exception as e:
        logger.warning(f"Tavily search failed for '{query}': {e}")
        return []


def _gemini_grounding_search(query: str) -> list[dict]:
    """Gemini googleSearch grounding fallback (선택). 현재는 placeholder."""
    # gemini grounding은 별도 API 필요하므로 여기서는 빈 결과
    # 필요 시 tools/gemini.py에 grounding tool 추가
    return []


def search_all(queries: list[str] | None = None) -> list[dict]:
    """모든 쿼리로 검색, 결과 합침. 4초 간격으로 RPM 보호."""
    queries = queries or DEFAULT_QUERIES
    # 환경변수로 쿼리 커스텀 가능
    env_q = os.getenv("SEARCH_QUERIES")
    if env_q:
        # 세미콜론 구분
        queries = [q.strip() for q in env_q.split(";") if q.strip()]

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for q in queries:
        results = _tavily_search(q)
        if not results:
            results = _gemini_grounding_search(q)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)
            elif not url:
                all_results.append(r)
        # 무료 티어 보호: 쿼리 간 딜레이
        time.sleep(random.uniform(1.0, 2.0))

    logger.info(f"Search done: {len(queries)} queries -> {len(all_results)} unique results")
    return all_results
