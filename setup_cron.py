"""LangGraph Platform Cron 등록 — CRON_SCHEDULE env로 주기 변경 용이."""
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 0 * * 1")  # 매주 월 00:00 UTC

async def main():
    try:
        from langgraph_sdk import get_client
    except ImportError:
        print("langgraph-sdk not installed. Run: pip install langgraph-sdk")
        return

    client = get_client(url=os.getenv("LANGGRAPH_API_URL", "http://localhost:2024"))

    # 기존 cron 조회 후 삭제 (중복 방지)
    try:
        existing = await client.crons.search()
        for c in existing:
            if c.get("schedule") == CRON_SCHEDULE:
                print(f"Existing cron found: {c['id']} schedule={c['schedule']}")
    except Exception as e:
        print(f"Search cron failed (may be no server): {e}")

    print(f"Creating cron: schedule='{CRON_SCHEDULE}' (UTC) — 월 09:00 KST 기준은 '0 0 * * 1'")
    print("Examples:")
    print("  매일 09:00 KST        -> 0 0 * * *")
    print("  월/목 09:00 KST       -> 0 0 * * 1,4")
    print("  매월 1일 09:00 KST    -> 0 0 1 * *")
    print("")
    print("To change: edit .env CRON_SCHEDULE and re-run this script, or:")
    print("  await client.crons.update(cron_id, schedule='0 0 * * *')")

    try:
        cron = await client.crons.create(
            assistant_id="agent",
            schedule=CRON_SCHEDULE,
            input={"messages": ["weekly crawl"]},
        )
        print(f"Cron created: {cron}")
    except Exception as e:
        print(f"Cron create failed (is LangGraph API running? 'langgraph up' or Cloud): {e}")
        print("Local dev without Postgres: cron won't fire — use OS cron or APScheduler as fallback.")

if __name__ == "__main__":
    asyncio.run(main())
