# Event Collector — 게임 외부행사 자동 수집

매주 월요일 09:00 KST에 한국+글로벌 게임잼/컨퍼런스/공모전/대회를 수집해 Google Sheets + Calendar에 저장.

## 구조
```
agent.py              # 6노드 LangGraph (search → extract → validate → dedup → sheets → calendar)
tools/
  gemini.py           # Gemini 무료 티어 rate limiter (4초 간격, 429 backoff)
  search.py           # Tavily 8쿼리 (KO+EN)
  extractor.py        # httpx+BS4 fetch → Gemini JSON 추출
  validator.py        # 마감/종료 필터 (Python 날짜 + Gemini status)
  sheets.py           # Service Account, Sheet 자동 생성 + dedup + batchUpdate
  calendar.py         # Service Account, Calendar dedup + insert
prompts/extract_prompt.txt
setup_cron.py         # Cron 등록 (CRON_SCHEDULE env로 변경)
```

## 빠른 시작
1. `cp .env.example .env` 후 `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `TAVILY_API_KEY`(선택) 채우기
2. Service Account 이메일(`...@...iam.gserviceaccount.com`)을 Google Cloud에서 생성, Sheets/Calendar/Drive API 활성화
3. `pip install -r requirements.txt`
4. 로컬 테스트: `python -c "from agent import graph; print(graph.invoke({'messages':['test']}))"`  (키 없으면 dry-run)
5. LangGraph Studio: `langgraph dev`
6. Cron 등록: `python setup_cron.py`  (또는 `.env` CRON_SCHEDULE 수정)

## 주기 변경
`.env` 한 줄만 수정:
```
CRON_SCHEDULE=0 0 * * *   # 매일
CRON_SCHEDULE=0 0 * * 1,4 # 월/목
```
후 `python setup_cron.py` 재실행 또는 Platform API로 update.

## 무료 티어
- Gemini 2.5-flash-lite: 15 RPM / 1,000 RPD — 주 1회 × ~25 req = 2.5% 소모
- Sheets: 300/min/project, 60/min/user — batchUpdate 1회로 충분
- Calendar: 10,000/min/project, 600/min/user — 이벤트 간 0.2초 딜레이
