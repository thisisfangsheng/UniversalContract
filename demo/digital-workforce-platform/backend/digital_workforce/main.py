from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from contextlib import asynccontextmanager
from typing import Any

import json

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .config import Settings, load_settings
from .core.presets import TEMPLATES
from .core.event_bridge import EventBridge
from .core.registry import WorkerRegistry
from .core.run_manager import WorkflowRunManager
from common.contracts import McpServerSpec, McpTransport
from common.skills import parse_skill_markdown
from .database import make_session_factory
from .models import Base, McpServerRecord, SkillRecord, WorkerRecord


class OrchestrationInput(BaseModel):
    goal: str = ""
    mode: str = "sequential"
    worker_ids: list[str] = Field(default_factory=list)
    session_id: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    context: list[dict[str, Any]] = Field(default_factory=list)
    template_id: str | None = None
    react: dict[str, Any] = Field(default_factory=dict)


class ApprovalInput(BaseModel):
    decision: str
    note: str = ""


class SkillInput(BaseModel):
    name: str
    content: str
    source: str = "inline"


class McpServerInput(BaseModel):
    name: str
    transport: str
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)


class WorkerInput(BaseModel):
    worker_id: str
    name: str
    description: str
    runtime_name: str = "offline-contract"
    capabilities: list[str] = Field(default_factory=list)
    skills: list[dict[str, str]] = Field(default_factory=list)
    mcp_server_names: list[str] = Field(default_factory=list)
    enabled: bool = True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine, session_factory = make_session_factory(settings.database_url)
    registry = WorkerRegistry(session_factory, settings.llm_serving())
    events = EventBridge(session_factory)
    manager = WorkflowRunManager(session_factory, registry, events)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield
        await engine.dispose()

    app = FastAPI(title="DigitalWorkforce API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.state.registry = registry
    app.state.manager = manager
    app.state.events = events

    def tenant(request: Request) -> str:
        if settings.api_token and request.headers.get("X-API-Token") != settings.api_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API token")
        return settings.default_tenant_id

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/workers")
    async def workers(request: Request) -> list[dict[str, Any]]:
        tenant_id = tenant(request)
        return [dict(registry.worker_view(item)) for item in await registry.list_workers(tenant_id)]

    @app.get("/api/workers/{worker_id}")
    async def worker(worker_id: str, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        try:
            return dict(registry.worker_view(await registry.get_worker(worker_id, tenant_id)))
        except KeyError as error:
            raise HTTPException(404, "worker not found") from error

    @app.post("/api/workers", status_code=201)
    async def create_worker(payload: WorkerInput, request: Request) -> dict[str, Any]:
        try:
            preset = await registry.register(tenant(request), payload.model_dump())
            return dict(registry.worker_view(preset))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.put("/api/workers/{worker_id}")
    async def update_worker(worker_id: str, payload: WorkerInput, request: Request) -> dict[str, Any]:
        try:
            preset = await registry.update(worker_id, tenant(request), payload.model_dump())
            return dict(registry.worker_view(preset))
        except KeyError as error:
            raise HTTPException(404, "custom worker not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.delete("/api/workers/{worker_id}", status_code=204)
    async def delete_worker(worker_id: str, request: Request) -> None:
        try:
            await registry.delete(worker_id, tenant(request))
        except KeyError as error:
            raise HTTPException(404, "custom worker not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/workers/{worker_id}/probe")
    async def probe_worker(worker_id: str, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        try:
            preset = await registry.get_worker(worker_id, tenant_id)
            await registry.probe(worker_id, tenant_id)
        except KeyError as error:
            raise HTTPException(404, "worker not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        binding = "未绑定 MCP" if not preset.mcp_server_names else f"已绑定 MCP: {', '.join(preset.mcp_server_names)}"
        return {"worker_id": worker_id, "ok": True, "runtime_name": preset.runtime_name, "message": f"{preset.runtime_name} 与 Worker 配置校验通过；{binding}。实际 MCP 连通性会在首个任务执行时验证。"}

    @app.get("/api/orchestrations/templates")
    async def templates(request: Request) -> list[dict[str, Any]]:
        tenant(request)
        return list(TEMPLATES.values())

    def skill_view(record: SkillRecord) -> dict[str, Any]:
        return {"name": record.name, "source": record.source, "content": record.content, "description": record.description}

    @app.get("/api/skills")
    async def skills(request: Request) -> list[dict[str, Any]]:
        tenant_id = tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(SkillRecord).where(SkillRecord.tenant_id == tenant_id).order_by(SkillRecord.name))).all()
            return [skill_view(record) for record in records]

    @app.post("/api/skills", status_code=201)
    async def create_skill(payload: SkillInput, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        document = parse_skill_markdown(payload.content, source=payload.source, default_name=payload.name)
        if not document.description:
            raise HTTPException(422, "SKILL.md frontmatter 必须提供 description")
        async with session_factory() as session:
            existing = await session.scalar(select(SkillRecord).where(SkillRecord.tenant_id == tenant_id, SkillRecord.name == payload.name))
            if existing is not None:
                raise HTTPException(409, "skill 已存在")
            record = SkillRecord(tenant_id=tenant_id, name=payload.name, source=payload.source, content=payload.content, description=document.description)
            session.add(record)
            await session.commit()
            return skill_view(record)

    @app.post("/api/skills/{name}/preview")
    async def preview_skill(name: str, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(SkillRecord).where(SkillRecord.tenant_id == tenant_id, SkillRecord.name == name))
            if record is None:
                raise HTTPException(404, "skill not found")
        document = parse_skill_markdown(record.content, source=record.source, default_name=record.name)
        return {"name": document.name, "description": document.description, "body": document.body, "digest": hashlib.sha256(document.body.encode()).hexdigest(), "allowed_tools": sorted(document.allowed_tools)}

    @app.delete("/api/skills/{name}", status_code=204)
    async def delete_skill(name: str, request: Request) -> None:
        tenant_id = tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(SkillRecord).where(SkillRecord.tenant_id == tenant_id, SkillRecord.name == name))
            if record is None:
                raise HTTPException(404, "skill not found")
            await session.delete(record)
            await session.commit()

    def mcp_view(record: McpServerRecord) -> dict[str, Any]:
        return {"name": record.name, "transport": record.transport, "url": record.url, "command": record.command, "args": json.loads(record.args_json), "allowed_tools": json.loads(record.allowed_tools_json), "blocked_tools": json.loads(record.blocked_tools_json)}

    @app.get("/api/mcp-servers")
    async def mcp_servers(request: Request) -> list[dict[str, Any]]:
        tenant_id = tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id).order_by(McpServerRecord.name))).all()
            return [mcp_view(record) for record in records]

    @app.post("/api/mcp-servers", status_code=201)
    async def create_mcp_server(payload: McpServerInput, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        if payload.transport not in {"streamable_http", "stdio", "websocket"}:
            raise HTTPException(422, "不支持的 MCP transport")
        if payload.transport == "streamable_http" and not payload.url:
            raise HTTPException(422, "STREAMABLE_HTTP MCP server 需要 url")
        async with session_factory() as session:
            if await session.scalar(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id, McpServerRecord.name == payload.name)):
                raise HTTPException(409, "MCP server 已存在")
            record = McpServerRecord(tenant_id=tenant_id, name=payload.name, transport=payload.transport, url=payload.url, command=payload.command, args_json=json.dumps(payload.args), allowed_tools_json=json.dumps(payload.allowed_tools), blocked_tools_json=json.dumps(payload.blocked_tools))
            session.add(record)
            await session.commit()
            return mcp_view(record)

    @app.put("/api/mcp-servers/{name}")
    async def update_mcp_server(name: str, payload: McpServerInput, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        if payload.name != name:
            raise HTTPException(422, "MCP Server 名称创建后不可修改")
        if payload.transport not in {"streamable_http", "stdio", "websocket"}:
            raise HTTPException(422, "不支持的 MCP transport")
        if payload.transport == "streamable_http" and not payload.url:
            raise HTTPException(422, "STREAMABLE_HTTP MCP server 需要 url")
        async with session_factory() as session:
            record = await session.scalar(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id, McpServerRecord.name == name))
            if record is None:
                raise HTTPException(404, "MCP server not found")
            record.transport = payload.transport
            record.url = payload.url
            record.command = payload.command
            record.args_json = json.dumps(payload.args)
            record.allowed_tools_json = json.dumps(payload.allowed_tools)
            record.blocked_tools_json = json.dumps(payload.blocked_tools)
            await session.commit()
            return mcp_view(record)

    @app.delete("/api/mcp-servers/{name}", status_code=204)
    async def delete_mcp_server(name: str, request: Request) -> None:
        tenant_id = tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(WorkerRecord).where(WorkerRecord.tenant_id == tenant_id))).all()
            bound_workers = [record.worker_id for record in records if name in json.loads(record.config_json).get("mcp_server_names", [])]
            if bound_workers:
                raise HTTPException(409, f"MCP Server 仍被员工绑定: {', '.join(bound_workers)}；请先编辑员工解除绑定")
            record = await session.scalar(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id, McpServerRecord.name == name))
            if record is None:
                raise HTTPException(404, "MCP server not found")
            await session.delete(record)
            await session.commit()

    @app.post("/api/mcp-servers/{name}/probe")
    async def probe_mcp_server(name: str, request: Request) -> dict[str, Any]:
        tenant_id = tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id, McpServerRecord.name == name))
            if record is None:
                raise HTTPException(404, "MCP server not found")
            if record.transport != "streamable_http":
                return {"name": name, "ok": False, "tools": [], "message": "当前平台仅能探测 STREAMABLE_HTTP MCP Server。"}
            server = McpServerSpec(name=record.name, transport=McpTransport(record.transport), url=record.url, command=record.command, args=tuple(json.loads(record.args_json)), allowed_tools=frozenset(json.loads(record.allowed_tools_json)), blocked_tools=frozenset(json.loads(record.blocked_tools_json)))
        try:
            await registry._probe_mcp_servers((server,))
            return {"name": name, "ok": True, "tools": sorted(server.allowed_tools), "message": "MCP initialize 与 tools/list 校验通过。"}
        except ValueError as error:
            return {"name": name, "ok": False, "tools": [], "message": str(error)}

    @app.post("/api/orchestrations", status_code=201)
    async def create_orchestration(payload: OrchestrationInput, request: Request) -> dict[str, str]:
        tenant_id = tenant(request)
        try:
            return await manager.create(payload.model_dump(), tenant_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/orchestrations/{orchestration_id}/continue", status_code=201)
    async def continue_orchestration(orchestration_id: str, payload: OrchestrationInput, request: Request) -> dict[str, str]:
        try:
            return await manager.continue_from(orchestration_id, payload.model_dump(), tenant(request))
        except KeyError as error:
            raise HTTPException(404, "orchestration not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/orchestrations")
    async def orchestrations(request: Request) -> list[dict[str, Any]]:
        return await manager.list(tenant(request))

    @app.get("/api/orchestrations/export")
    async def export_orchestrations(request: Request) -> Response:
        records = await manager.list(tenant(request))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["orchestration_id", "goal", "mode", "status", "session_id", "created_at"])
        for record in records:
            writer.writerow([record["orchestration_id"], record["goal"], record["mode"], record["status"], record["session_id"], record["created_at"]])
        return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=orchestrations.csv"})

    @app.get("/api/orchestrations/{orchestration_id}")
    async def orchestration(orchestration_id: str, request: Request) -> dict[str, Any]:
        item = await manager.get(orchestration_id, tenant(request))
        if item is None:
            raise HTTPException(404, "orchestration not found")
        return item

    @app.get("/api/orchestrations/{orchestration_id}/session")
    async def orchestration_session(orchestration_id: str, request: Request) -> dict[str, Any]:
        item = await manager.get(orchestration_id, tenant(request))
        if item is None:
            raise HTTPException(404, "orchestration not found")
        return item["session"] or {"session_id": item["session_id"], "facts": {}, "messages": []}

    @app.get("/api/orchestrations/{orchestration_id}/events")
    async def orchestration_events(orchestration_id: str, request: Request):
        tenant_id = tenant(request)
        if await manager.get(orchestration_id, tenant_id) is None:
            raise HTTPException(404, "orchestration not found")
        try:
            after = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            raise HTTPException(422, "Last-Event-ID 必须是整数")

        async def event_stream():
            async for event in events.stream(tenant_id, orchestration_id, after):
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/orchestrations/{orchestration_id}/approvals/{task_id}")
    async def approve(orchestration_id: str, task_id: str, payload: ApprovalInput, request: Request) -> dict[str, Any]:
        try:
            return await manager.approve(orchestration_id, task_id, payload.decision, payload.note, tenant(request))
        except KeyError as error:
            raise HTTPException(404, "pending approval not found") from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/orchestrations/{orchestration_id}/cancel")
    async def cancel(orchestration_id: str, request: Request) -> dict[str, str]:
        if not await manager.cancel(orchestration_id, tenant(request)):
            raise HTTPException(404, "orchestration not found")
        return {"status": "cancelled"}

    return app


app = create_app()