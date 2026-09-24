from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from digital_workforce.config import Settings
from digital_workforce.main import create_app


async def _wait_for_terminal(client: httpx.AsyncClient, orchestration_id: str) -> dict:
    for _ in range(40):
        response = await client.get(f"/api/orchestrations/{orchestration_id}")
        response.raise_for_status()
        payload = response.json()
        if payload["status"] not in {"queued", "running"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("orchestration did not reach a terminal state")


async def _wait_for_event(app, orchestration_id: str, event_type: str) -> list[dict]:
    for _ in range(40):
        events = await app.state.events.records("default", orchestration_id)
        if any(event["type"] == event_type for event in events):
            return events
        await asyncio.sleep(0.02)
    raise AssertionError(f"event {event_type} was not emitted")


async def run_tests() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database_url = f"sqlite+aiosqlite:///{Path(directory) / 'platform.db'}"
        app = create_app(Settings(database_url=database_url, api_token=None))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                workers = (await client.get("/api/workers")).json()
                assert {item["worker_id"] for item in workers} >= {"timekeeper", "scout", "writer"}
                assert next(item for item in workers if item["worker_id"] == "writer")["skill_catalog"]
                custom = await client.post("/api/workers", json={"worker_id": "briefing", "name": "简报助手", "description": "输出简洁业务简报", "capabilities": ["briefing"], "skills": [{"name": "brief-style", "description": "简报格式"}]})
                assert custom.status_code == 201, custom.text
                assert (await client.post("/api/workers/briefing/probe")).json()["ok"] is True
                assert (await client.get("/api/workers/briefing")).json()["mcp_tools"] == []
                updated = await client.put("/api/workers/briefing", json={"worker_id": "briefing", "name": "升级简报助手", "description": "输出升级后的业务简报", "runtime_name": "offline-contract", "capabilities": ["briefing", "reporting"]})
                assert updated.status_code == 200, updated.text
                assert updated.json()["name"] == "升级简报助手"
                custom_run = await client.post("/api/orchestrations", json={"goal": "生成一份简报", "mode": "sequential", "worker_ids": ["briefing"]})
                assert (await _wait_for_terminal(client, custom_run.json()["orchestration_id"]))["status"] == "completed"
                assert (await client.delete("/api/workers/briefing")).status_code == 204
                assert (await client.get("/api/workers/briefing")).status_code == 404
                assert (await client.post("/api/orchestrations", json={"goal": "不应执行", "mode": "sequential", "worker_ids": ["briefing"]})).status_code == 422

                unavailable_runtime = await client.post("/api/workers", json={"worker_id": "tool-user", "name": "工具助手", "description": "使用 MCP", "runtime_name": "agentscope-mcp", "mcp_server_names": ["missing"]})
                assert unavailable_runtime.status_code == 422

                skill = await client.post("/api/skills", json={"name": "catalog-test", "content": "---\nname: catalog-test\ndescription: 目录测试\n---\n保持证据边界。"})
                assert skill.status_code == 201
                preview = await client.post("/api/skills/catalog-test/preview")
                assert preview.json()["description"] == "目录测试"
                server = await client.post("/api/mcp-servers", json={"name": "time", "transport": "streamable_http", "url": "http://127.0.0.1:1/mcp"})
                assert server.status_code == 201
                probe = await client.post("/api/mcp-servers/time/probe")
                assert probe.status_code == 200 and probe.json()["ok"] is False
                revised_server = await client.put("/api/mcp-servers/time", json={"name": "time", "transport": "streamable_http", "url": "http://127.0.0.1:1/time", "allowed_tools": ["now"]})
                assert revised_server.status_code == 200 and revised_server.json()["url"] == "http://127.0.0.1:1/time"
                invalid_binding = await client.post("/api/workers", json={"worker_id": "offline-tool", "name": "离线工具员工", "description": "无效绑定", "runtime_name": "offline-contract", "mcp_server_names": ["time"]})
                assert invalid_binding.status_code == 422
                assert (await client.delete("/api/mcp-servers/time")).status_code == 204

                created = await client.post("/api/orchestrations", json={
                    "template_id": "sc-a-fact-brief",
                    "goal": "调研 A 公司",
                    "mode": "concurrent",
                    "worker_ids": ["writer"],
                    "facts": {"company": "A 公司"},
                    "context": [{"label": "evidence", "content": "使用可追溯事实"}],
                })
                assert created.status_code == 201, created.text
                created_payload = created.json()
                assert created_payload["session_id"].startswith("ses_")
                result = await _wait_for_terminal(client, created_payload["orchestration_id"])
                assert result["status"] == "completed"
                assert "未配置 LLM 服务" in result["result"]["worker_results"][0]["output"]["text"]
                assert result["mode"] == "sequential"  # Template has precedence.
                assert result["effective"]["worker_ids"] == ["timekeeper", "scout"]
                assert result["effective"]["facts"]["context:evidence"] == "使用可追溯事实"
                assert len(result["result"]["worker_results"]) == 2
                session = (await client.get(f"/api/orchestrations/{created_payload['orchestration_id']}/session")).json()
                assert len(session["messages"]) == 4
                continued = await client.post(f"/api/orchestrations/{created_payload['orchestration_id']}/continue", json={"goal": "根据前面的调研补充一份结论", "mode": "sequential", "worker_ids": ["writer"]})
                assert continued.status_code == 201, continued.text
                continuation = await _wait_for_terminal(client, continued.json()["orchestration_id"])
                assert continuation["status"] == "completed"
                assert continuation["session_id"] == created_payload["session_id"]
                assert continuation["continued_from"] == created_payload["orchestration_id"]
                assert continuation["effective"]["worker_ids"] == ["writer"]

                concurrent = await client.post("/api/orchestrations", json={
                    "goal": "并行扫描", "mode": "concurrent", "worker_ids": ["scout", "analyst"],
                })
                concurrent_result = await _wait_for_terminal(client, concurrent.json()["orchestration_id"])
                assert concurrent_result["status"] == "completed"
                assert concurrent_result["mode"] == "concurrent"
                assert len(concurrent_result["result"]["worker_results"]) == 2
                export = await client.get("/api/orchestrations/export")
                assert export.status_code == 200 and "orchestration_id" in export.text and "调研 A 公司" in export.text

                react = await client.post("/api/orchestrations", json={
                    "template_id": "sc-c-iterative-report", "goal": "形成迭代报告",
                })
                react_result = await _wait_for_terminal(client, react.json()["orchestration_id"])
                assert react_result["status"] == "completed"
                assert react_result["mode"] == "react"
                assert len(react_result["result"]["rounds"]) == 2
                assert react_result["result"]["rounds"][1]["plan"]["round"] == 2
                assert [item["worker_id"] for item in react_result["result"]["rounds"][1]["results"]] == ["writer"]

                approval_run = await client.post("/api/orchestrations", json={
                    "template_id": "sc-d-approval-release", "goal": "审批后发布简报",
                })
                approval_id = approval_run.json()["orchestration_id"]
                pending = await _wait_for_terminal(client, approval_id)
                assert pending["status"] == "waiting_approval"
                task_id = pending["result"]["worker_results"][0]["task_id"]
                events = await _wait_for_event(app, approval_id, "approval_required")
                assert [event["type"] for event in events if event["type"] != "heartbeat"][:3] == ["plan", "plan", "task_dispatch"]
                resolved = await client.post(f"/api/orchestrations/{approval_id}/approvals/{task_id}", json={"decision": "approve", "note": "可以发布"})
                assert resolved.status_code == 200, resolved.text
                resumed = await _wait_for_terminal(client, approval_id)
                assert resumed["status"] == "completed"
                events = await app.state.events.records("default", approval_id)
                assert any(event["type"] == "approval_resolved" for event in events)
                assert sum(event["type"] == "task_dispatch" for event in events) >= 3
                duplicate = await client.post(f"/api/orchestrations/{approval_id}/approvals/{task_id}", json={"decision": "approve"})
                assert duplicate.status_code == 409

        restarted = create_app(Settings(database_url=database_url, api_token=None))
        async with restarted.router.lifespan_context(restarted):
            transport = httpx.ASGITransport(app=restarted)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                restored = (await client.get(f"/api/orchestrations/{created_payload['orchestration_id']}")).json()
                assert restored["status"] == "completed"
                restored_session = (await client.get(f"/api/orchestrations/{created_payload['orchestration_id']}/session")).json()
                assert len(restored_session["messages"]) == 4


async def run_auth_tests() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database_url = f"sqlite+aiosqlite:///{Path(directory) / 'auth.db'}"
        app = create_app(Settings(database_url=database_url, api_token=None, auth_profile="token", jwt_secret="offline-test-secret-at-least-32-bytes"))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/api/workers")).status_code == 401
                alice = await client.post("/api/auth/register", json={
                    "username": "alice", "password": "a-secure-password", "display_name": "Alice",
                    "tenant_id": "tenant-a", "tenant_name": "Tenant A",
                })
                assert alice.status_code == 201, alice.text
                alice_token = alice.json()["access_token"]
                alice_headers = {"Authorization": f"Bearer {alice_token}"}
                created = await client.post("/api/workers", headers=alice_headers, json={
                    "worker_id": "private-briefing", "name": "Private", "description": "Alice only",
                })
                assert created.status_code == 201, created.text
                assert (await client.post("/api/auth/logout", headers=alice_headers)).status_code == 204
                relogin = await client.post("/api/auth/login", json={"username": "alice", "password": "a-secure-password"})
                assert relogin.status_code == 200, relogin.text
                assert relogin.json()["tenant_id"] == "tenant-a"
                relogin_headers = {"Authorization": f"Bearer {relogin.json()['access_token']}", "X-Tenant": relogin.json()["tenant_id"]}
                assert {item["worker_id"] for item in (await client.get("/api/workers", headers=relogin_headers)).json()} >= {"private-briefing"}
                bob = await client.post("/api/auth/register", json={
                    "username": "bob", "password": "another-secure-password", "display_name": "Bob",
                    "tenant_id": "tenant-b", "tenant_name": "Tenant B",
                })
                assert bob.status_code == 201, bob.text
                bob_headers = {"Authorization": f"Bearer {bob.json()['access_token']}"}
                assert (await client.get("/api/workers/private-briefing", headers=bob_headers)).status_code == 404
                assert (await client.get("/api/workers", headers={**bob_headers, "X-Tenant": "tenant-a"})).status_code == 403
                assert (await client.get("/api/workers", headers=alice_headers)).status_code == 401


if __name__ == "__main__":
    asyncio.run(run_tests())
    asyncio.run(run_auth_tests())
    print("ALL PLATFORM M3 CHECKS PASSED")