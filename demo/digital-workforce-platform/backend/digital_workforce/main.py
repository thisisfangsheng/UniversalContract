from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import uuid
from contextlib import asynccontextmanager
from typing import Any

import json

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.contracts import AuthMode, McpServerSpec, McpTransport, PrincipalContext
from common.security import AuthenticationError, AuthorizationError
from .config import Settings, load_settings
from .core.event_bridge import EventBridge
from .core.registry import WorkerRegistry
from .core.run_manager import WorkflowRunManager
from common.skills import parse_skill_markdown
from .database import make_session_factory
from .auth import InMemoryTokenRevocation, JwtTokenService
from .identity import SqlAlchemyUserDirectory
from .models import Base, CatalogMcpServerRecord, CatalogSkillRecord, CatalogWorkerRecord, MembershipRecord, ProcessTemplateRecord, TenantContextRecord, TenantRecord, UserRecord


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


class RegistrationInput(BaseModel):
    username: str
    password: str = Field(min_length=12)
    display_name: str
    tenant_id: str = Field(min_length=1)
    tenant_name: str = Field(min_length=1)


class LoginInput(BaseModel):
    username: str
    password: str
    tenant_id: str = ""


class RefreshInput(BaseModel):
    refresh_token: str


class MembershipInput(BaseModel):
    username: str
    role: str = "member"


class TenantContextInput(BaseModel):
    system_prompt: str = Field(max_length=4000)


class ProcessTemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    status: str = "draft"
    mode: str = "sequential"
    worker_ids: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    react: dict[str, Any] = Field(default_factory=dict)


class ExecutionAccessInput(BaseModel):
    username: str
    access_level: str = "viewer"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.auth_profile not in {"local", "token"}:
        raise ValueError("AUTH_PROFILE 仅支持 local 或 token")
    engine, session_factory = make_session_factory(settings.database_url)
    registry = WorkerRegistry(session_factory, settings.llm_serving())
    events = EventBridge(session_factory)
    manager = WorkflowRunManager(session_factory, registry, events)
    identities = SqlAlchemyUserDirectory(session_factory)
    revocation = InMemoryTokenRevocation()
    token_service = JwtTokenService(settings.jwt_secret, revocation) if settings.auth_profile == "token" else None

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
    app.state.identities = identities
    app.state.token_service = token_service
    access_cookie_name = "digital_workforce_access"

    def principal(request: Request) -> PrincipalContext:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authenticated principal")
        return principal

    def tenant(request: Request) -> str:
        return principal(request).tenant_id

    async def resolve_principal(request: Request) -> PrincipalContext:
        if settings.auth_profile == "local":
            return PrincipalContext("local-user", settings.default_tenant_id, frozenset({"owner"}), auth_mode=AuthMode.NONE)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            token = request.cookies.get(access_cookie_name, "")
        if not token:
            raise AuthenticationError("缺少 Bearer token")
        assert token_service is not None
        authenticated = await token_service.verify(token)
        account = await identities.get_user(authenticated.subject)
        if account is None or account.status != "active":
            raise AuthenticationError("用户不存在或已停用")
        requested_tenant = request.headers.get("x-tenant", authenticated.tenant_id)
        membership = await identities.resolve_membership(account.user_id, requested_tenant)
        if membership is None:
            raise AuthorizationError("用户不属于请求的租户")
        return PrincipalContext(account.user_id, membership.tenant_id, frozenset({membership.role}), authenticated.scopes, AuthMode.BEARER)

    async def token_pair(principal: PrincipalContext) -> dict[str, str | int]:
        assert token_service is not None
        return {
            "access_token": await token_service.issue(principal, ttl_s=settings.access_token_ttl_s),
            "refresh_token": await token_service.issue_refresh(principal, ttl_s=settings.refresh_token_ttl_s),
            "token_type": "bearer",
            "expires_in": settings.access_token_ttl_s,
            "tenant_id": principal.tenant_id,
        }

    def set_access_cookie(response: Response, tokens: dict[str, str | int]) -> None:
        response.set_cookie(
            key=access_cookie_name,
            value=str(tokens["access_token"]),
            max_age=settings.access_token_ttl_s,
            httponly=True,
            samesite="lax",
        )

    @app.middleware("http")
    async def authenticate_api_requests(request: Request, call_next):
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/") and request.url.path != "/api/health":
            try:
                request.state.principal = await resolve_principal(request)
            except AuthenticationError as error:
                return JSONResponse({"detail": str(error)}, status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
            except AuthorizationError as error:
                return JSONResponse({"detail": str(error)}, status_code=status.HTTP_403_FORBIDDEN)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/register", status_code=201)
    async def register(payload: RegistrationInput, response: Response) -> dict[str, Any]:
        if settings.auth_profile != "token":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="local profile 不提供注册")
        async with session_factory() as session:
            if await session.scalar(select(UserRecord).where(UserRecord.username == payload.username)):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")
            if await session.get(TenantRecord, payload.tenant_id):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tenant_id 已存在")
            account = UserRecord(
                user_id=f"usr_{uuid.uuid4().hex}", username=payload.username, display_name=payload.display_name,
                password_hash=identities.hash_password(payload.password), status="active",
            )
            session.add(TenantRecord(tenant_id=payload.tenant_id, display_name=payload.tenant_name, status="active"))
            session.add(account)
            session.add(MembershipRecord(user_id=account.user_id, tenant_id=payload.tenant_id, role="owner"))
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户或租户已存在") from error
        principal = PrincipalContext(account.user_id, payload.tenant_id, frozenset({"owner"}), auth_mode=AuthMode.BEARER)
        tokens = await token_pair(principal)
        set_access_cookie(response, tokens)
        return {"user": {"user_id": account.user_id, "username": account.username, "display_name": account.display_name}, "tenant_id": payload.tenant_id, **tokens}

    @app.post("/api/auth/login")
    async def login(payload: LoginInput, response: Response) -> dict[str, str | int]:
        if settings.auth_profile != "token":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="local profile 不提供登录")
        account = await identities.verify(payload.username, payload.password)
        if account is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误", headers={"WWW-Authenticate": "Bearer"})
        memberships = await identities.memberships_of(account.user_id)
        tenant_id = payload.tenant_id or (memberships[0].tenant_id if memberships else "")
        membership = await identities.resolve_membership(account.user_id, tenant_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户不属于请求的租户")
        tokens = await token_pair(PrincipalContext(account.user_id, tenant_id, frozenset({membership.role}), auth_mode=AuthMode.BEARER))
        set_access_cookie(response, tokens)
        return tokens

    @app.post("/api/auth/refresh")
    async def refresh(payload: RefreshInput, response: Response) -> dict[str, str | int]:
        if token_service is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="local profile 不提供刷新")
        try:
            principal = await token_service.verify_refresh(payload.refresh_token)
        except AuthenticationError as error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error), headers={"WWW-Authenticate": "Bearer"}) from error
        membership = await identities.resolve_membership(principal.subject, principal.tenant_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户不属于 token 租户")
        await revocation.revoke(payload.refresh_token)
        tokens = await token_pair(PrincipalContext(principal.subject, membership.tenant_id, frozenset({membership.role}), auth_mode=AuthMode.BEARER))
        set_access_cookie(response, tokens)
        return tokens

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, response: Response) -> None:
        if token_service is None:
            return None
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            token = request.cookies.get(access_cookie_name, "")
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer token", headers={"WWW-Authenticate": "Bearer"})
        await revocation.revoke(token)
        response.delete_cookie(access_cookie_name)

    @app.get("/api/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        principal = await resolve_principal(request)
        memberships = await identities.memberships_of(principal.subject)
        return {"user_id": principal.subject, "tenant_id": principal.tenant_id, "roles": sorted(principal.roles), "memberships": [{"tenant_id": item.tenant_id, "role": item.role} for item in memberships]}

    @app.post("/api/tenants/{tenant_id}/memberships", status_code=201)
    async def add_membership(tenant_id: str, payload: MembershipInput, request: Request) -> dict[str, str]:
        principal = await resolve_principal(request)
        if principal.tenant_id != tenant_id or "owner" not in principal.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅租户 owner 可管理成员")
        if payload.role not in {"owner", "member", "viewer"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="role 仅支持 owner、member 或 viewer")
        async with session_factory() as session:
            account = await session.scalar(select(UserRecord).where(UserRecord.username == payload.username))
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
            if await session.scalar(select(MembershipRecord).where(MembershipRecord.user_id == account.user_id, MembershipRecord.tenant_id == tenant_id)):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="成员关系已存在")
            session.add(MembershipRecord(user_id=account.user_id, tenant_id=tenant_id, role=payload.role))
            await session.commit()
        return {"user_id": account.user_id, "tenant_id": tenant_id, "role": payload.role}

    @app.get("/api/tenant-context")
    async def tenant_context(request: Request) -> dict[str, str]:
        tenant_id = tenant(request)
        async with session_factory() as session:
            context = await session.get(TenantContextRecord, tenant_id)
        return {"tenant_id": tenant_id, "system_prompt": context.system_prompt if context else ""}

    @app.put("/api/tenant-context")
    async def update_tenant_context(payload: TenantContextInput, request: Request) -> dict[str, str]:
        authenticated = principal(request)
        if "owner" not in authenticated.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅租户 owner 可修改系统提示词")
        async with session_factory() as session:
            context = await session.get(TenantContextRecord, authenticated.tenant_id)
            if context is None:
                context = TenantContextRecord(tenant_id=authenticated.tenant_id, system_prompt=payload.system_prompt)
                session.add(context)
            else:
                context.system_prompt = payload.system_prompt
            await session.commit()
        return {"tenant_id": authenticated.tenant_id, "system_prompt": payload.system_prompt}

    def process_template_view(record: ProcessTemplateRecord) -> dict[str, Any]:
        definition = json.loads(record.definition_json)
        return {
            "id": record.template_id,
            "name": record.name,
            "description": record.description,
            "status": record.status,
            "version": record.version,
            "mode": definition["mode"],
            "worker_ids": definition["worker_ids"],
            "facts": definition.get("facts", {}),
            "react": definition.get("react", {}),
        }

    async def validate_process_template(payload: ProcessTemplateInput, tenant_id: str) -> None:
        if payload.status not in {"draft", "published"}:
            raise HTTPException(status_code=422, detail="status 仅支持 draft 或 published")
        if payload.mode not in {"sequential", "concurrent", "handoff", "react"}:
            raise HTTPException(status_code=422, detail="不支持的编排模式")
        if not payload.worker_ids:
            raise HTTPException(status_code=422, detail="流程模板至少需要一个数字员工")
        try:
            for worker_id in payload.worker_ids:
                await registry.get_worker(worker_id, tenant_id)
        except KeyError as error:
            raise HTTPException(status_code=422, detail=f"未知数字员工: {error.args[0]}") from error

    @app.get("/api/process-templates")
    async def process_templates(request: Request) -> list[dict[str, Any]]:
        authenticated = principal(request)
        async with session_factory() as session:
            statement = select(ProcessTemplateRecord).where(ProcessTemplateRecord.tenant_id == authenticated.tenant_id)
            if "owner" not in authenticated.roles:
                statement = statement.where(ProcessTemplateRecord.status == "published")
            records = (await session.scalars(statement.order_by(ProcessTemplateRecord.name))).all()
        return [process_template_view(record) for record in records]

    @app.post("/api/process-templates", status_code=201)
    async def create_process_template(payload: ProcessTemplateInput, request: Request) -> dict[str, Any]:
        authenticated = principal(request)
        if "owner" not in authenticated.roles:
            raise HTTPException(status_code=403, detail="仅租户 owner 可创建流程模板")
        await validate_process_template(payload, authenticated.tenant_id)
        record = ProcessTemplateRecord(
            template_id=f"pt_{uuid.uuid4().hex}", tenant_id=authenticated.tenant_id,
            name=payload.name, description=payload.description, status=payload.status,
            definition_json=json.dumps({"mode": payload.mode, "worker_ids": payload.worker_ids, "facts": payload.facts, "react": payload.react}, ensure_ascii=False),
            created_by_user_id=authenticated.subject,
        )
        async with session_factory() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise HTTPException(status_code=409, detail="该租户已存在同名流程模板") from error
        return process_template_view(record)

    @app.put("/api/process-templates/{template_id}")
    async def update_process_template(template_id: str, payload: ProcessTemplateInput, request: Request) -> dict[str, Any]:
        authenticated = principal(request)
        if "owner" not in authenticated.roles:
            raise HTTPException(status_code=403, detail="仅租户 owner 可修改流程模板")
        await validate_process_template(payload, authenticated.tenant_id)
        async with session_factory() as session:
            record = await session.get(ProcessTemplateRecord, template_id)
            if record is None or record.tenant_id != authenticated.tenant_id:
                raise HTTPException(status_code=404, detail="流程模板不存在")
            record.name = payload.name
            record.description = payload.description
            record.status = payload.status
            record.version += 1
            record.definition_json = json.dumps({"mode": payload.mode, "worker_ids": payload.worker_ids, "facts": payload.facts, "react": payload.react}, ensure_ascii=False)
            await session.commit()
        return process_template_view(record)

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
        return await process_templates(request)

    def skill_view(record: CatalogSkillRecord) -> dict[str, Any]:
        return {"name": record.name, "source": record.source, "content": record.content, "description": record.description}

    @app.get("/api/skills")
    async def skills(request: Request) -> list[dict[str, Any]]:
        tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(CatalogSkillRecord).order_by(CatalogSkillRecord.name))).all()
            return [skill_view(record) for record in records]

    @app.post("/api/skills", status_code=201)
    async def create_skill(payload: SkillInput, request: Request) -> dict[str, Any]:
        tenant(request)
        document = parse_skill_markdown(payload.content, source=payload.source, default_name=payload.name)
        if not document.description:
            raise HTTPException(422, "SKILL.md frontmatter 必须提供 description")
        async with session_factory() as session:
            existing = await session.scalar(select(CatalogSkillRecord).where(CatalogSkillRecord.name == payload.name))
            if existing is not None:
                raise HTTPException(409, "skill 已存在")
            record = CatalogSkillRecord(name=payload.name, source=payload.source, content=payload.content, description=document.description)
            session.add(record)
            await session.commit()
            return skill_view(record)

    @app.post("/api/skills/{name}/preview")
    async def preview_skill(name: str, request: Request) -> dict[str, Any]:
        tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(CatalogSkillRecord).where(CatalogSkillRecord.name == name))
            if record is None:
                raise HTTPException(404, "skill not found")
        document = parse_skill_markdown(record.content, source=record.source, default_name=record.name)
        return {"name": document.name, "description": document.description, "body": document.body, "digest": hashlib.sha256(document.body.encode()).hexdigest(), "allowed_tools": sorted(document.allowed_tools)}

    @app.delete("/api/skills/{name}", status_code=204)
    async def delete_skill(name: str, request: Request) -> None:
        tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(CatalogSkillRecord).where(CatalogSkillRecord.name == name))
            if record is None:
                raise HTTPException(404, "skill not found")
            await session.delete(record)
            await session.commit()

    def mcp_view(record: CatalogMcpServerRecord) -> dict[str, Any]:
        return {"name": record.name, "transport": record.transport, "url": record.url, "command": record.command, "args": json.loads(record.args_json), "allowed_tools": json.loads(record.allowed_tools_json), "blocked_tools": json.loads(record.blocked_tools_json)}

    @app.get("/api/mcp-servers")
    async def mcp_servers(request: Request) -> list[dict[str, Any]]:
        tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(CatalogMcpServerRecord).order_by(CatalogMcpServerRecord.name))).all()
            return [mcp_view(record) for record in records]

    @app.post("/api/mcp-servers", status_code=201)
    async def create_mcp_server(payload: McpServerInput, request: Request) -> dict[str, Any]:
        tenant(request)
        if payload.transport not in {"streamable_http", "stdio", "websocket"}:
            raise HTTPException(422, "不支持的 MCP transport")
        if payload.transport == "streamable_http" and not payload.url:
            raise HTTPException(422, "STREAMABLE_HTTP MCP server 需要 url")
        async with session_factory() as session:
            if await session.scalar(select(CatalogMcpServerRecord).where(CatalogMcpServerRecord.name == payload.name)):
                raise HTTPException(409, "MCP server 已存在")
            record = CatalogMcpServerRecord(name=payload.name, transport=payload.transport, url=payload.url, command=payload.command, args_json=json.dumps(payload.args), allowed_tools_json=json.dumps(payload.allowed_tools), blocked_tools_json=json.dumps(payload.blocked_tools))
            session.add(record)
            await session.commit()
            return mcp_view(record)

    @app.put("/api/mcp-servers/{name}")
    async def update_mcp_server(name: str, payload: McpServerInput, request: Request) -> dict[str, Any]:
        tenant(request)
        if payload.name != name:
            raise HTTPException(422, "MCP Server 名称创建后不可修改")
        if payload.transport not in {"streamable_http", "stdio", "websocket"}:
            raise HTTPException(422, "不支持的 MCP transport")
        if payload.transport == "streamable_http" and not payload.url:
            raise HTTPException(422, "STREAMABLE_HTTP MCP server 需要 url")
        async with session_factory() as session:
            record = await session.scalar(select(CatalogMcpServerRecord).where(CatalogMcpServerRecord.name == name))
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
        tenant(request)
        async with session_factory() as session:
            records = (await session.scalars(select(CatalogWorkerRecord))).all()
            bound_workers = [record.worker_id for record in records if name in json.loads(record.config_json).get("mcp_server_names", [])]
            if bound_workers:
                raise HTTPException(409, f"MCP Server 仍被员工绑定: {', '.join(bound_workers)}；请先编辑员工解除绑定")
            record = await session.scalar(select(CatalogMcpServerRecord).where(CatalogMcpServerRecord.name == name))
            if record is None:
                raise HTTPException(404, "MCP server not found")
            await session.delete(record)
            await session.commit()

    @app.post("/api/mcp-servers/{name}/probe")
    async def probe_mcp_server(name: str, request: Request) -> dict[str, Any]:
        tenant(request)
        async with session_factory() as session:
            record = await session.scalar(select(CatalogMcpServerRecord).where(CatalogMcpServerRecord.name == name))
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
        authenticated = principal(request)
        try:
            return await manager.create(payload.model_dump(), authenticated.tenant_id, authenticated.subject)
        except (KeyError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/orchestrations/{orchestration_id}/continue", status_code=201)
    async def continue_orchestration(orchestration_id: str, payload: OrchestrationInput, request: Request) -> dict[str, str]:
        authenticated = principal(request)
        try:
            return await manager.continue_from(orchestration_id, payload.model_dump(), authenticated.tenant_id, authenticated.subject)
        except KeyError as error:
            raise HTTPException(404, "orchestration not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/orchestrations")
    async def orchestrations(request: Request) -> list[dict[str, Any]]:
        authenticated = principal(request)
        return await manager.list(authenticated.tenant_id, authenticated.subject)

    @app.get("/api/orchestrations/export")
    async def export_orchestrations(request: Request) -> Response:
        authenticated = principal(request)
        records = await manager.list(authenticated.tenant_id, authenticated.subject)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["orchestration_id", "goal", "mode", "status", "session_id", "created_at"])
        for record in records:
            writer.writerow([record["orchestration_id"], record["goal"], record["mode"], record["status"], record["session_id"], record["created_at"]])
        return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=orchestrations.csv"})

    @app.get("/api/orchestrations/{orchestration_id}")
    async def orchestration(orchestration_id: str, request: Request) -> dict[str, Any]:
        authenticated = principal(request)
        item = await manager.get(orchestration_id, authenticated.tenant_id, authenticated.subject)
        if item is None:
            raise HTTPException(404, "orchestration not found")
        return item

    @app.get("/api/orchestrations/{orchestration_id}/session")
    async def orchestration_session(orchestration_id: str, request: Request) -> dict[str, Any]:
        authenticated = principal(request)
        item = await manager.get(orchestration_id, authenticated.tenant_id, authenticated.subject)
        if item is None:
            raise HTTPException(404, "orchestration not found")
        return item["session"] or {"session_id": item["session_id"], "facts": {}, "messages": []}

    @app.get("/api/orchestrations/{orchestration_id}/events")
    async def orchestration_events(orchestration_id: str, request: Request):
        authenticated = principal(request)
        tenant_id = authenticated.tenant_id
        if await manager.get(orchestration_id, tenant_id, authenticated.subject) is None:
            raise HTTPException(404, "orchestration not found")
        try:
            after = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            raise HTTPException(422, "Last-Event-ID 必须是整数")

        async def event_stream():
            async for event in events.stream(tenant_id, orchestration_id, after):
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/orchestrations/{orchestration_id}/access", status_code=201)
    async def grant_orchestration_access(orchestration_id: str, payload: ExecutionAccessInput, request: Request) -> dict[str, str]:
        authenticated = principal(request)
        async with session_factory() as session:
            user = await session.scalar(select(UserRecord).where(UserRecord.username == payload.username))
            membership = None if user is None else await session.scalar(select(MembershipRecord).where(MembershipRecord.tenant_id == authenticated.tenant_id, MembershipRecord.user_id == user.user_id))
        if membership is None:
            raise HTTPException(status_code=422, detail="授权用户不是当前租户成员")
        try:
            await manager.grant_access(orchestration_id, authenticated.tenant_id, authenticated.subject, user.user_id, payload.access_level)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="orchestration not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"orchestration_id": orchestration_id, "username": payload.username, "access_level": payload.access_level}

    @app.post("/api/orchestrations/{orchestration_id}/approvals/{task_id}")
    async def approve(orchestration_id: str, task_id: str, payload: ApprovalInput, request: Request) -> dict[str, Any]:
        authenticated = principal(request)
        try:
            return await manager.approve(orchestration_id, task_id, payload.decision, payload.note, authenticated.tenant_id, authenticated.subject)
        except KeyError as error:
            raise HTTPException(404, "pending approval not found") from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/orchestrations/{orchestration_id}/cancel")
    async def cancel(orchestration_id: str, request: Request) -> dict[str, str]:
        authenticated = principal(request)
        if not await manager.cancel(orchestration_id, authenticated.tenant_id, authenticated.subject):
            raise HTTPException(404, "orchestration not found")
        return {"status": "cancelled"}

    return app


app = create_app()