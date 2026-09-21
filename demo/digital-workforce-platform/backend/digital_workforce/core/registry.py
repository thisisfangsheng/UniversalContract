from __future__ import annotations

import sys
import json
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[3]
UC_ROOT = Path(__file__).resolve().parents[5]
if str(UC_ROOT) not in sys.path:
    sys.path.insert(0, str(UC_ROOT))

from common.a2a import A2AWorkerEndpoint, A2AWorkerService, InMemoryA2ATransport
from common.contracts import AgentIdentity, AgentRequest, AgentResult, Capability, HarnessExecutionContext, HarnessProviders, HarnessSpec, LlmServingSpec, McpServerSpec, McpTransport, WorkerDescriptor
from common.skills import InlineSkillProvider, SkillRegistry
from harness.provider import ProviderHarness
from harness.openai_compatible import OpenAICompatibleRuntimeAdapter

from .presets import WORKER_PRESETS, WorkerPreset
from .sessions import SQLiteSessionProvider
from ..models import McpServerRecord, WorkerRecord


class OfflineAgent:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        request = context.request
        collaboration = request.metadata.get("collaboration", {})
        prior = collaboration.get("prior_results", {})
        task_input = collaboration.get("task_input", {})
        if task_input.get("requires_approval") and request.resume is None:
            return AgentResult("needs_approval", "等待人工审批后继续。", run_id=request.request_id, data={"approval": "required"})
        output = f"当前未配置 LLM 服务，{self.worker_id} 只能完成离线流程演示，无法回答问题：{request.input}"
        data = {"worker": self.worker_id, "prior_result_count": len(prior), "offline": True, "reason": "missing_llm_configuration"}
        return AgentResult("completed", output, run_id=request.request_id, data=data)

    async def run_stream(self, context: HarnessExecutionContext):
        yield await self.run(context)


class OfflineRuntime:
    runtime_name = "offline-contract"

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> OfflineAgent:
        return OfflineAgent(self.worker_id)

    async def close(self) -> None:
        return None


class WorkerRegistry:
    """Tenant-scoped Harness registry. Workers share one SessionProvider per tenant."""

    def __init__(self, sessions, llm_serving: LlmServingSpec | None = None) -> None:
        self._sessions = sessions
        self._llm_serving = llm_serving
        self._providers: dict[str, SQLiteSessionProvider] = {}
        self._presets = {preset.worker_id: preset for preset in WORKER_PRESETS}

    def session_provider_for(self, tenant_id: str) -> SQLiteSessionProvider:
        return self._providers.setdefault(tenant_id, SQLiteSessionProvider(self._sessions, tenant_id))

    @property
    def llm_serving(self) -> LlmServingSpec | None:
        return self._llm_serving

    async def list_workers(self, tenant_id: str) -> Sequence[WorkerPreset]:
        async with self._sessions() as session:
            records = (await session.scalars(select(WorkerRecord).where(WorkerRecord.tenant_id == tenant_id))).all()
        custom = {record.worker_id: self._record_to_preset(record) for record in records}
        return tuple({**self._presets, **custom}.values())

    async def get_worker(self, worker_id: str, tenant_id: str) -> WorkerPreset:
        async with self._sessions() as session:
            record = await session.get(WorkerRecord, {"worker_id": worker_id, "tenant_id": tenant_id})
        preset = self._record_to_preset(record) if record is not None else self._presets.get(worker_id)
        if preset is None:
            raise KeyError(worker_id)
        return preset

    async def register(self, tenant_id: str, payload: Mapping[str, Any]) -> WorkerPreset:
        worker_id = str(payload["worker_id"]).strip()
        if not worker_id or not worker_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("worker_id 只能包含字母、数字、-、_")
        skills = tuple((str(item["name"]), str(item["description"]), "catalog") for item in payload.get("skills", []) if item.get("name") and item.get("description"))
        preset = WorkerPreset(worker_id, str(payload["name"]), str(payload["description"]), str(payload.get("runtime_name", "offline-contract")), tuple(payload.get("capabilities", [])), skills, tuple(payload.get("mcp_server_names", [])), bool(payload.get("enabled", True)))
        await self._validate_preset(preset, tenant_id)
        async with self._sessions() as session:
            existing = await session.get(WorkerRecord, {"worker_id": worker_id, "tenant_id": tenant_id})
            if existing is not None or worker_id in self._presets:
                raise ValueError("worker_id 已存在")
            session.add(WorkerRecord(worker_id=worker_id, tenant_id=tenant_id, display_name=preset.name, description=preset.description, runtime_name=preset.runtime_name, enabled=preset.enabled, config_json=json.dumps({"capabilities": preset.capabilities, "skills": preset.skills, "mcp_server_names": preset.mcp_server_names}, ensure_ascii=False)))
            await session.commit()
        return preset

    async def delete(self, worker_id: str, tenant_id: str) -> None:
        if worker_id in self._presets:
            raise ValueError("内置员工由出厂预设管理，不能删除")
        async with self._sessions() as session:
            record = await session.get(WorkerRecord, {"worker_id": worker_id, "tenant_id": tenant_id})
            if record is None:
                raise KeyError(worker_id)
            await session.delete(record)
            await session.commit()

    async def update(self, worker_id: str, tenant_id: str, payload: Mapping[str, Any]) -> WorkerPreset:
        if worker_id in self._presets:
            raise ValueError("内置员工由出厂预设管理，不能在控制台编辑")
        if str(payload.get("worker_id", worker_id)) != worker_id:
            raise ValueError("worker_id 创建后不可修改")
        async with self._sessions() as session:
            record = await session.get(WorkerRecord, {"worker_id": worker_id, "tenant_id": tenant_id})
            if record is None:
                raise KeyError(worker_id)
        skills = tuple((str(item["name"]), str(item["description"]), "catalog") for item in payload.get("skills", []) if item.get("name") and item.get("description"))
        preset = WorkerPreset(worker_id, str(payload["name"]), str(payload["description"]), str(payload.get("runtime_name", "offline-contract")), tuple(payload.get("capabilities", [])), skills, tuple(payload.get("mcp_server_names", [])), bool(payload.get("enabled", True)))
        await self._validate_preset(preset, tenant_id)
        async with self._sessions() as session:
            record = await session.get(WorkerRecord, {"worker_id": worker_id, "tenant_id": tenant_id})
            if record is None:
                raise KeyError(worker_id)
            record.display_name = preset.name
            record.description = preset.description
            record.runtime_name = preset.runtime_name
            record.enabled = preset.enabled
            record.config_json = json.dumps({"capabilities": preset.capabilities, "skills": preset.skills, "mcp_server_names": preset.mcp_server_names}, ensure_ascii=False)
            await session.commit()
        return preset

    async def probe(self, worker_id: str, tenant_id: str) -> list[str]:
        preset = await self.get_worker(worker_id, tenant_id)
        await self._validate_preset(preset, tenant_id)
        return []

    async def endpoints_for(self, worker_ids: Sequence[str], tenant_id: str) -> list[A2AWorkerEndpoint]:
        presets = [await self.get_worker(worker_id, tenant_id) for worker_id in worker_ids]
        missing = [preset.worker_id for preset in presets if not preset.enabled]
        if missing:
            raise KeyError(", ".join(missing))
        services: dict[str, A2AWorkerService] = {}
        endpoint_specs: list[tuple[WorkerPreset, ProviderHarness, str]] = []
        session = self.session_provider_for(tenant_id)
        for preset in presets:
            skill_specs = []
            for name, description, _origin in preset.skills:
                from common.contracts import SkillSpec
                skill_specs.append(SkillSpec(name, "inline", content=f"---\nname: {name}\ndescription: {description}\n---\n遵守 {description}。"))
            skill_registry = SkillRegistry([InlineSkillProvider(skill_specs)]) if skill_specs else None
            spec = await self._harness_spec_for(preset, tenant_id, skill_specs)
            harness = ProviderHarness(spec, HarnessProviders(session=session, skills=skill_registry), self._runtime_for(preset))
            problems = await harness.validate()
            if problems:
                raise ValueError("; ".join(problems))
            address = f"memory://{preset.worker_id}"
            services[address] = A2AWorkerService(harness, preset.worker_id)
            endpoint_specs.append((preset, harness, address))
        transport = InMemoryA2ATransport(services)
        return [
            A2AWorkerEndpoint(WorkerDescriptor(preset.worker_id, address, frozenset(preset.capabilities), preset.description), transport)
            for preset, _harness, address in endpoint_specs
        ]

    async def _validate_preset(self, preset: WorkerPreset, tenant_id: str) -> None:
        session = self.session_provider_for(tenant_id)
        spec = await self._harness_spec_for(preset, tenant_id, ())
        problems = await ProviderHarness(spec, HarnessProviders(session=session), self._runtime_for(preset)).validate()
        if problems:
            raise ValueError("; ".join(problems))
        if spec.tools and preset.runtime_name != "offline-contract":
            await self._probe_mcp_servers(spec.tools)

    @staticmethod
    async def _probe_mcp_servers(servers: Sequence[McpServerSpec]) -> None:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as error:
            raise ValueError("真实 MCP runtime 需要安装 mcp SDK") from error
        for server in servers:
            if server.transport != McpTransport.STREAMABLE_HTTP:
                continue
            timeout = server.request_timeout_s or 15
            try:
                async with asyncio.timeout(timeout):
                    async with streamablehttp_client(server.url, timeout=timeout) as (read_stream, write_stream, _session_id):
                        async with ClientSession(read_stream, write_stream) as mcp_session:
                            await mcp_session.initialize()
                            tools = (await mcp_session.list_tools()).tools
                allowed = [tool.name for tool in tools if not server.allowed_tools or tool.name in server.allowed_tools]
                if not allowed:
                    raise ValueError("未发现符合 allowed_tools 策略的工具")
            except ValueError:
                raise
            except Exception as error:
                raise ValueError(f"MCP Server {server.name} 连通性检查失败: {type(error).__name__}: {error}") from error

    def _runtime_for(self, preset: WorkerPreset):
        if preset.runtime_name == "offline-contract" or (self._llm_serving is None and preset.worker_id in self._presets):
            return OfflineRuntime(preset.worker_id)
        if preset.runtime_name == "openai-compatible":
            return OpenAICompatibleRuntimeAdapter(self._llm_serving)
        if preset.runtime_name == "mcp-openai-compatible":
            from harness.mcp_openai_runtime import McpOpenAICompatibleRuntimeAdapter
            return McpOpenAICompatibleRuntimeAdapter(self._llm_serving)
        if preset.runtime_name == "agentscope-mcp":
            from harness.agentscope_mcp_adapter import AgentScopeMcpRuntimeAdapter
            return AgentScopeMcpRuntimeAdapter(self._llm_serving)
        if preset.runtime_name == "langgraph-mcp":
            from harness.langgraph_mcp_adapter import LangGraphMcpRuntimeAdapter
            return LangGraphMcpRuntimeAdapter(self._llm_serving)
        raise ValueError(f"不支持的 runtime_name: {preset.runtime_name}")

    async def _harness_spec_for(self, preset: WorkerPreset, tenant_id: str, skills) -> HarnessSpec:
        servers = await self._mcp_servers_for(preset.mcp_server_names, tenant_id)
        if preset.runtime_name == "offline-contract" and servers:
            raise ValueError("offline-contract runtime 不支持 MCP；请选择 MCP runtime")
        if preset.runtime_name == "openai-compatible" and servers:
            raise ValueError("openai-compatible runtime 不支持 MCP；请选择 MCP runtime")
        if preset.runtime_name != "offline-contract" and self._llm_serving is None and preset.worker_id not in self._presets:
            raise ValueError("真实 runtime 需要配置 UNIVERSAL_CONTRACT_LLM_API_KEY")
        if preset.runtime_name == "mcp-openai-compatible" and (len(servers) != 1 or servers[0].transport != McpTransport.STREAMABLE_HTTP):
            raise ValueError("mcp-openai-compatible 要求绑定且仅绑定一个 STREAMABLE_HTTP MCP Server")
        if preset.runtime_name in {"agentscope-mcp", "langgraph-mcp"} and (not servers or any(server.transport != McpTransport.STREAMABLE_HTTP for server in servers)):
            raise ValueError(f"{preset.runtime_name} 当前只支持至少一个 STREAMABLE_HTTP MCP Server")
        capabilities = {Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT}
        if servers:
            capabilities.add(Capability.TOOL_LOOP)
        return HarnessSpec(AgentIdentity(preset.worker_id, preset.name, preset.description), f"你是{preset.name}。请根据任务与协作上下文完成工作。", frozenset(capabilities), tools=servers, skills=tuple(skills), llm_serving=self._llm_serving)

    async def _mcp_servers_for(self, names: Sequence[str], tenant_id: str) -> tuple[McpServerSpec, ...]:
        if not names:
            return ()
        async with self._sessions() as session:
            records = (await session.scalars(select(McpServerRecord).where(McpServerRecord.tenant_id == tenant_id, McpServerRecord.name.in_(names)))).all()
        found = {record.name: record for record in records}
        missing = [name for name in names if name not in found]
        if missing:
            raise ValueError(f"未找到已绑定的 MCP Server: {', '.join(missing)}")
        return tuple(McpServerSpec(name=record.name, transport=McpTransport(record.transport), url=record.url, command=record.command, args=tuple(json.loads(record.args_json)), allowed_tools=frozenset(json.loads(record.allowed_tools_json)), blocked_tools=frozenset(json.loads(record.blocked_tools_json))) for record in (found[name] for name in names))

    @staticmethod
    def _record_to_preset(record: WorkerRecord) -> WorkerPreset:
        config = json.loads(record.config_json)
        return WorkerPreset(record.worker_id, record.display_name, record.description, record.runtime_name, tuple(config.get("capabilities", [])), tuple(tuple(item) for item in config.get("skills", [])), tuple(config.get("mcp_server_names", [])), record.enabled)

    def worker_view(self, preset: WorkerPreset) -> Mapping[str, Any]:
        return {**asdict(preset), "editable": preset.worker_id not in self._presets, "mcp_tools": list(preset.mcp_server_names), "skill_catalog": [
            {"name": name, "description": description, "origin": origin}
            for name, description, origin in preset.skills
        ]}
