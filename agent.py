"""게임 외부행사 수집 에이전트 — 주 1회, 마감검증, Sheets+Calendar 자동 저장."""
import os
import logging
from typing import TypedDict, Annotated
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, START, END

# .env 로드 (langgraph.json env보다 먼저)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")


# ── State ──
class State(TypedDict, total=False):
    messages: list[str]
    raw_search_results: list[dict]
    extracted_events: list[dict]
    valid_events: list[dict]
    expired_events: list[dict]
    sheets_result: dict
    calendar_result: dict
    stats: dict
    errors: list[str]


# ── Nodes ──

def search_node(state: State) -> dict:
    """Tavily 8쿼리 검색 (KO+EN)."""
    from tools.search import search_all
    logger.info("=== search_node start ===")
    try:
        results = search_all()
        msg = f"검색 완료: {len(results)}개 결과"
        logger.info(msg)
        return {"raw_search_results": results, "messages": [msg]}
    except Exception as e:
        logger.error(f"search_node failed: {e}")
        return {"raw_search_results": [], "errors": [f"search: {e}"], "messages": [f"검색 실패: {e}"]}


def extract_node(state: State) -> dict:
    """fetch + Gemini 구조화."""
    from tools.extractor import extract_events
    raw = state.get("raw_search_results", [])
    logger.info(f"=== extract_node start (raw={len(raw)}) ===")
    if not raw:
        return {"extracted_events": [], "messages": ["검색 결과 없음 — 추출 스킵"]}
    try:
        events = extract_events(raw)
        msg = f"추출 완료: {len(events)}개 행사"
        logger.info(msg)
        return {"extracted_events": events, "messages": [msg]}
    except Exception as e:
        logger.error(f"extract_node failed: {e}")
        return {"extracted_events": [], "errors": [f"extract: {e}"]}


def validate_node(state: State) -> dict:
    """마감/종료 필터."""
    from tools.validator import filter_valid
    events = state.get("extracted_events", [])
    logger.info(f"=== validate_node start (extracted={len(events)}) ===")
    if not events:
        return {"valid_events": [], "expired_events": [], "messages": ["추출 결과 없음 — 검증 스킵"]}
    try:
        valid, expired = filter_valid(events)
        msg = f"검증 완료: 유효 {len(valid)}개, 마감/종료 {len(expired)}개 제외"
        logger.info(msg)
        return {"valid_events": valid, "expired_events": expired, "messages": [msg]}
    except Exception as e:
        logger.error(f"validate_node failed: {e}")
        return {"valid_events": events, "expired_events": [], "errors": [f"validate: {e}"]}


def dedup_node(state: State) -> dict:
    """Sheets 기존 id 기준 dedup은 sheets_node에서 처리하므로 여기서는 패스스루 + 통계."""
    valid = state.get("valid_events", [])
    logger.info(f"=== dedup_node (valid={len(valid)}) ===")
    # 실제 dedup은 sheets에서, 여기서는 로깅만
    return {"messages": [f"검증 통과 {len(valid)}개 — Sheets 중복 검사로 전달"]}


def sheets_node(state: State) -> dict:
    """Google Sheets 저장."""
    from tools.sheets import write_events
    events = state.get("valid_events", [])
    logger.info(f"=== sheets_node start (valid={len(events)}) ===")
    if not events:
        return {"sheets_result": {"inserted": 0, "skipped": 0}, "messages": ["저장할 유효 행사 없음"]}
    try:
        result = write_events(events)
        msg = f"Sheets: {result.get('inserted',0)}개 추가, {result.get('skipped',0)}개 중복 스킵"
        if result.get("error"):
            msg += f" (참고: {result['error']})"
        if result.get("sheet_id"):
            msg += f" sheet={result['sheet_id']}"
        logger.info(msg)
        return {"sheets_result": result, "messages": [msg]}
    except Exception as e:
        logger.error(f"sheets_node failed: {e}")
        return {"sheets_result": {"inserted": 0, "error": str(e)}, "errors": [f"sheets: {e}"]}


def calendar_node(state: State) -> dict:
    """Google Calendar 저장 — Sheets에 신규 삽입된 것만? 현재는 valid 전체를 dedup."""
    from tools.calendar import write_events
    events = state.get("valid_events", [])
    sheets_res = state.get("sheets_result", {})
    logger.info(f"=== calendar_node start (valid={len(events)}) ===")
    if not events:
        return {"calendar_result": {"inserted": 0}, "messages": ["Calendar: 저장할 행사 없음"]}
    # Sheets에서 스킵된 것은 Calendar도 스킵될 확률 높지만, Calendar dedup이 별도이므로 전체 전달
    # 단, dry-run 모드에서는 Sheets inserted 0이어도 Calendar 시도
    try:
        result = write_events(events)
        msg = f"Calendar: {result.get('inserted',0)}개 추가, {result.get('skipped',0)}개 중복 스킵"
        if result.get("error"):
            msg += f" (참고: {result['error']})"
        logger.info(msg)
        # 최종 통계
        stats = {
            "found": len(state.get("extracted_events", [])),
            "expired": len(state.get("expired_events", [])),
            "valid": len(events),
            "sheets_inserted": sheets_res.get("inserted", 0),
            "sheets_skipped": sheets_res.get("skipped", 0),
            "calendar_inserted": result.get("inserted", 0),
            "calendar_skipped": result.get("skipped", 0),
            "timestamp": datetime.now(SEOUL).isoformat(),
        }
        return {"calendar_result": result, "stats": stats, "messages": [msg, f"통계: {stats}"]}
    except Exception as e:
        logger.error(f"calendar_node failed: {e}")
        return {"calendar_result": {"inserted": 0, "error": str(e)}, "errors": [f"calendar: {e}"]}


# ── Graph ──
workflow = StateGraph(State)

workflow.add_node("search", search_node)
workflow.add_node("extract", extract_node)
workflow.add_node("validate", validate_node)
workflow.add_node("dedup", dedup_node)
workflow.add_node("sheets", sheets_node)
workflow.add_node("calendar", calendar_node)

workflow.add_edge(START, "search")
workflow.add_edge("search", "extract")
workflow.add_edge("extract", "validate")
workflow.add_edge("validate", "dedup")
workflow.add_edge("dedup", "sheets")
workflow.add_edge("sheets", "calendar")
workflow.add_edge("calendar", END)

graph = workflow.compile()
