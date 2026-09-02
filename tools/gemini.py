"""Gemini 클라이언트 — 무료 티어 대응 rate limiter + 지수 백오프."""
import os
import time
import json
import random
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 모델 설정: 2.0은 2026-06-01 deprecated이므로 기본 2.5-flash-lite
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
MIN_INTERVAL_SEC = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "4.0"))
MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "5"))

_client = None
_last_call_ts = 0.0


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        _client = genai
        return _client
    except Exception as e:
        logger.warning(f"Gemini client init failed: {e}")
        return None


def _throttle():
    """RPM 준수: 최소 간격 보장 (기본 4초 → 15 RPM 안전)."""
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_INTERVAL_SEC:
        sleep_s = MIN_INTERVAL_SEC - elapsed + random.uniform(0, 0.5)
        time.sleep(sleep_s)
    _last_call_ts = time.time()


def _load_prompt(input_text: str) -> str:
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    year = time.strftime("%Y")
    prompt_path = Path(__file__).parent.parent / "prompts" / "extract_prompt.txt"
    if prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
        return template.format(today=today, now=now, year=year, input_text=input_text[:12000])
    # fallback
    return f"Extract game events as JSON array. Today is {today}. Input:\n{input_text[:12000]}"


def generate_json(input_text: str, model: Optional[str] = None) -> list[dict]:
    """Gemini로 JSON 배열 추출. 실패 시 빈 배열."""
    client = _get_client()
    if client is None:
        logger.warning("GEMINI_API_KEY not set — returning empty")
        return []

    prompt = _load_prompt(input_text)
    model_name = model or DEFAULT_MODEL

    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            m = client.GenerativeModel(model_name)
            resp = m.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": 4096,
                    "response_mime_type": "application/json",
                },
            )
            text = (resp.text or "").strip()
            # JSON 배열 파싱 (코드펜스 제거)
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            # gemini가 객체로 감싸는 경우 대응
            data = json.loads(text)
            if isinstance(data, dict) and "events" in data:
                data = data["events"]
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                logger.warning(f"Gemini returned non-list: {type(data)}")
                return []
            return data
        except Exception as e:
            msg = str(e)
            is_rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
            is_retryable = is_rate or "503" in msg or "500" in msg
            logger.warning(f"Gemini call failed (attempt {attempt+1}/{MAX_RETRIES}, model={model_name}): {e}")
            if not is_retryable or attempt == MAX_RETRIES - 1:
                # fallback 모델 시도
                if model_name == DEFAULT_MODEL and FALLBACK_MODEL != DEFAULT_MODEL and attempt == MAX_RETRIES - 1:
                    logger.info(f"Trying fallback model {FALLBACK_MODEL}")
                    try:
                        return generate_json(input_text, model=FALLBACK_MODEL)
                    except Exception:
                        pass
                return []
            # 지수 백오프 + jitter
            backoff = (2 ** attempt) * 2 + random.uniform(0, 1)
            if is_rate:
                backoff = max(backoff, 10)
            time.sleep(backoff)
    return []


def generate_text(prompt: str, model: Optional[str] = None) -> str:
    """단순 텍스트 생성 (검색 쿼리 확장 등에 사용 가능)."""
    client = _get_client()
    if client is None:
        return ""
    model_name = model or DEFAULT_MODEL
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            m = client.GenerativeModel(model_name)
            resp = m.generate_content(prompt, generation_config={"temperature": 0.3})
            return (resp.text or "").strip()
        except Exception as e:
            msg = str(e)
            is_rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg
            if attempt == MAX_RETRIES - 1:
                return ""
            backoff = (2 ** attempt) * 2 + random.uniform(0, 1)
            if is_rate:
                backoff = max(backoff, 10)
            time.sleep(backoff)
    return ""
