# Event Collector — 게임 외부행사 자동 수집

매주 월요일 09:00 KST에 한국+글로벌 게임잼/컨퍼런스/공모전/대회를 수집해 Google Sheets + Calendar에 저장.
해외 행사는 온라인만, 국내 행사는 온/오프라인 모두 저장하며 마감/종료된 행사는 자동 제외.

## 구조
```
agent.py              # 7노드 LangGraph (search → extract → validate → overseas_filter → dedup → sheets → calendar)
tools/
  gemini.py           # Gemini 3.5-flash-lite rate limiter (4초 간격, 429 지수백오프, fallback flash-lite-latest)
  search.py           # Tavily 8쿼리 (KO 4 + EN 4)
  extractor.py        # httpx+BS4 fetch → Gemini JSON 추출 (배치 4개씩)
  validator.py        # 마감/종료 필터 + 해외 오프라인 필터 (국내 키워드/.kr/온라인 포함 시 유지)
  sheets.py           # Service Account, events 탭 자동 생성 + hash dedup + batchUpdate
  calendar.py         # Service Account, 그룹 캘린더 자동 등록 + eventId dedup + insert
prompts/extract_prompt.txt  # JSON 스키마 + location/해외 표기 규칙
setup_cron.py         # Cron 등록 (CRON_SCHEDULE env로 변경)
```

## 빠른 시작
1. `cp .env.example .env` 후 채우기:
   - `GEMINI_API_KEY` (모델: `gemini-3.5-flash-lite`, fallback `gemini-flash-lite-latest` — 2.5는 신규 사용자 404)
   - `TAVILY_API_KEY` (`tvly-...`, 검색 8쿼리)
   - `GOOGLE_SERVICE_ACCOUNT_FILE=game-event-agent-xxx.json` 또는 `GOOGLE_SERVICE_ACCOUNT_JSON`
   - `SHEET_ID` (예: `1dL2n...`), `CALENDAR_ID` (그룹 캘린더 `...@group.calendar.google.com` 또는 `primary`)
2. Google Cloud: Service Account 생성 → Sheets/Calendar/Drive API 활성화 → 해당 시트/캘린더에 `...@...iam.gserviceaccount.com` 편집자 공유 (그룹 캘린더는 SA가 자동 `calendarList.insert`로 등록)
3. `pip install -r requirements.txt` (protobuf 6.33.6 고정)
4. 로컬 테스트: `python -c "from agent import graph; print(graph.invoke({'messages':['test']}))"`  (키 없으면 dry-run)
5. LangGraph Studio: `langgraph dev`
6. Cron 등록: `python setup_cron.py`  (또는 `.env` CRON_SCHEDULE 수정)
7. 시트 확인: 하단 `events` 탭 (헤더: id/title/category/start_date/end_date/deadline/location/url/source/status/discovered_at/calendar_event_id/last_updated)

## 주기 변경
`.env` 한 줄만 수정:
```
CRON_SCHEDULE=0 0 * * *   # 매일
CRON_SCHEDULE=0 0 * * 1,4 # 월/목
```
후 `python setup_cron.py` 재실행 또는 Platform API로 update.

## 동작 규칙
- **마감 필터:** `deadline/end_date < today(Asia/Seoul)` 또는 Gemini `status=closed/cancelled`면 제외
- **해외 필터:** `location`에 `온라인` 포함 → 유지, `오프라인: 해외(미국/일본 등)` + 국내 키워드/`.kr` 없음 → 제외, 빈 location/혼합(오프라인+온라인)은 유지
- **중복:** `sha1(title+start_date+url)` 해시로 Sheets A열/Calendar eventId dedup

## 무료 티어
- Gemini 3.5-flash-lite (free): ~15 RPM / 1,000 RPD — 주 1회 8쿼리 × ~10 Gemini 호출 = 1% 미만
- Sheets: 300/min/project, 60/min/user — `events` 탭 `batchUpdate` 1회
- Calendar: 10,000/min/project, 600/min/user — 그룹 캘린더 자동 등록 후 `events.insert` (0.2초 간격)
