"""在进程内调用 REST A2A Worker，展示认证、授权及 tenant 会话隔离。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

DEMO_DIRECTORY = Path(__file__).resolve().parent
if str(DEMO_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(DEMO_DIRECTORY))

from app import app, sessions


async def main() -> None:
    payload = {
        "task_id": "rest-demo-task-1",
        "session_id": "customer-42",
        "instruction": "确认订单状态",
        "input": {"order_id": "ORD-42"},
        "idempotency_key": "rest-demo:1",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as client:
        unauthenticated = await client.post("/workers/echo/tasks", json=payload)
        print(f"without token: HTTP {unauthenticated.status_code}")
        accepted = await client.post(
            "/workers/echo/tasks", json=payload, headers={"Authorization": "Bearer demo-token"},
        )
        print(f"with token: HTTP {accepted.status_code}; response={accepted.json()}")
    session = await sessions.load("demo-tenant:customer-42")
    print(f"stored session_id={session.session_id}; messages={len(session.messages)}")


if __name__ == "__main__":
    asyncio.run(main())