"""将 usecase.yaml 解析为已注册 Harness agent 的 HTTP A2A 编排运行时。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from common.a2a import A2AWorkerEndpoint, A2AWorkerService, HttpA2ATransport
from common.contracts import (
    AgentIdentity,
    Capability,
    HarnessProviders,
    HarnessSpec,
    LlmServingSpec,
    McpServerSpec,
    McpTransport,
    OrchestrationMode,
    OrchestrationRequest,
    WorkerDescriptor,
)
from common.providers import InMemorySessionProvider, StaticContextProvider
from common.security import AllowAllAuthorizer, NoAuthAuthenticator
from common.timing import timing_span
from deployment.fastapi_a2a import create_a2a_app
from harness import ProviderHarness
from harness.agentscope_mcp_adapter import AgentScopeMcpRuntimeAdapter
from harness.langgraph_mcp_adapter import LangGraphMcpRuntimeAdapter
from orchestration import create_react_workflow
from orchestration.magentic import DeterministicMagenticManager


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    template: str
    endpoint_path: str
    mcp: Mapping[str, Any]


@dataclass(frozen=True)
class UseCaseConfig:
    use_case_id: str
    session_id: str
    goal: str
    facts: Mapping[str, Any]
    context_label: str
    context_content: str
    context_priority: int
    workers: Sequence[WorkerConfig]
    engine: str
    max_round_count: int
    max_stall_count: int
    max_reset_count: int


@dataclass(frozen=True)
class AgentTemplate:
    description: str
    build_harness: Callable[[WorkerConfig, LlmServingSpec, HarnessProviders], ProviderHarness]


def load_environment(repository_root: Path) -> None:
    """读取本地 .env，并保持 shell 显式设置的变量优先。"""
    env_file = repository_root / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if separator and name and name.replace("_", "").isalnum():
            os.environ.setdefault(name, value.strip().strip("'\""))


def serving_from_environment() -> LlmServingSpec:
    base_url = os.getenv("UNIVERSAL_CONTRACT_LLM_BASE_URL")
    api_key = os.getenv("UNIVERSAL_CONTRACT_LLM_API_KEY")
    missing = [
        name
        for name, value in (
            ("UNIVERSAL_CONTRACT_LLM_BASE_URL", base_url),
            ("UNIVERSAL_CONTRACT_LLM_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少 OpenAI 兼容 Serving 配置: {', '.join(missing)}")
    return LlmServingSpec(
        model=os.getenv("UNIVERSAL_CONTRACT_LLM_MODEL", "gpt-4o-mini"),
        base_url=base_url,
        api_key=api_key,
        temperature=0.1,
    )


def load_use_case(path: Path) -> UseCaseConfig:
    """解析并校验该 demo 支持的最小 use case 配置。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("use case 配置必须是 YAML 对象")
    workers = raw.get("workers")
    context = raw.get("context")
    orchestration = raw.get("orchestration")
    if not isinstance(workers, list) or not workers:
        raise ValueError("workers 必须是非空列表")
    if not isinstance(context, dict) or not isinstance(orchestration, dict):
        raise ValueError("context 与 orchestration 必须是对象")
    parsed_workers = tuple(
        WorkerConfig(
            worker_id=_required_string(item, "worker_id"),
            template=_required_string(item, "template"),
            endpoint_path=_required_string(item, "endpoint_path"),
            mcp=_mapping(item.get("mcp"), f"worker {item.get('worker_id', '<unknown>')}.mcp"),
        )
        for item in workers
        if isinstance(item, dict)
    )
    if len(parsed_workers) != len(workers):
        raise ValueError("workers 的每个元素必须是对象")
    worker_ids = [worker.worker_id for worker in parsed_workers]
    if len(set(worker_ids)) != len(worker_ids):
        raise ValueError("workers.worker_id 必须唯一")
    paths = [worker.endpoint_path for worker in parsed_workers]
    if len(set(paths)) != len(paths) or not all(path.startswith("/") for path in paths):
        raise ValueError("workers.endpoint_path 必须是唯一且以 / 开头的路径")
    return UseCaseConfig(
        use_case_id=_required_string(raw, "id"),
        session_id=_required_string(raw, "session_id"),
        goal=_required_string(raw, "goal"),
        facts=_mapping(raw.get("facts", {}), "facts"),
        context_label=_required_string(context, "label"),
        context_content=_required_string(context, "content"),
        context_priority=_integer(context.get("priority", 10), "context.priority"),
        workers=parsed_workers,
        engine=_required_string(orchestration, "engine"),
        max_round_count=_integer(orchestration.get("max_round_count", 2), "max_round_count"),
        max_stall_count=_integer(orchestration.get("max_stall_count", 1), "max_stall_count"),
        max_reset_count=_integer(orchestration.get("max_reset_count", 0), "max_reset_count"),
    )


def _required_string(value: Mapping[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return candidate


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _mcp_server(worker: WorkerConfig, *, name: str, description: str) -> McpServerSpec:
    allowed_tools = worker.mcp.get("allowed_tools")
    if not isinstance(allowed_tools, list) or not all(isinstance(item, str) for item in allowed_tools):
        raise ValueError(f"worker {worker.worker_id} 的 mcp.allowed_tools 必须是字符串列表")
    url = worker.mcp.get("url")
    endpoint_env = worker.mcp.get("endpoint_env")
    if endpoint_env is not None:
        if not isinstance(endpoint_env, str):
            raise ValueError(f"worker {worker.worker_id} 的 mcp.endpoint_env 必须是字符串")
        url = os.getenv(endpoint_env)
        if not url:
            raise RuntimeError(f"worker {worker.worker_id} 缺少 MCP endpoint 环境变量: {endpoint_env}")
    if not isinstance(url, str) or not url:
        raise ValueError(f"worker {worker.worker_id} 必须配置 mcp.url 或 mcp.endpoint_env")
    return McpServerSpec(
        name=name,
        transport=McpTransport.STREAMABLE_HTTP,
        url=url,
        description=description,
        allowed_tools=frozenset(allowed_tools),
        request_timeout_s=_integer(worker.mcp.get("request_timeout_s", 30), "mcp.request_timeout_s"),
    )


def _build_amap_trip(worker: WorkerConfig, serving: LlmServingSpec, providers: HarnessProviders) -> ProviderHarness:
    description = (
        "上海游玩与天气专家。使用高德 MCP 查询上海 POI 和未来天气，选择具体日期；"
        "不查询或推荐航班。"
    )
    return ProviderHarness(
        HarnessSpec(
            identity=AgentIdentity(worker.worker_id, "AgentScope 上海游玩天气 Agent", description),
            instructions=(
                "你是上海游玩和未来一周天气查询专家。先使用 maps_text_search 查询适合游玩的上海 POI，"
                "再使用 maps_weather 查询上海未来一周天气；根据返回的天气选择一个具体日期。"
                "只报告工具明确返回的事实，不得查询航班或编造数据。"
            ),
            requested_capabilities=frozenset({Capability.TOOL_LOOP}),
            tools=(_mcp_server(worker, name="amap", description="高德地点检索与天气查询服务。"),),
            llm_serving=serving,
        ),
        providers,
        AgentScopeMcpRuntimeAdapter(max_tool_rounds=8),
    )


def _build_maqami_flight(worker: WorkerConfig, serving: LlmServingSpec, providers: HarnessProviders) -> ProviderHarness:
    description = (
        "北京到上海单程航班报价专家。读取前序协作快照中的日期，查询当天 BJS 到 SHA 的直飞报价；"
        "不预订、取消、支付或重新选择日期。"
    )
    return ProviderHarness(
        HarnessSpec(
            identity=AgentIdentity(worker.worker_id, "LangGraph 北京上海航班 Agent", description),
            instructions=(
                "你是北京到上海单程航班信息专家。从 Contract context 中定位前序 worker 已选日期，"
                "调用 post_flights_rates 查询当天 BJS 到 SHA、1 名成人、CNY 的直飞报价。"
                "只报告工具明确返回的事实；不得预订、取消、支付或重新选择日期。"
            ),
            requested_capabilities=frozenset({Capability.TOOL_LOOP}),
            tools=(_mcp_server(worker, name="maqami-flight", description="Maqami 公开航班报价服务。"),),
            llm_serving=serving,
        ),
        providers,
        LangGraphMcpRuntimeAdapter(),
    )


AGENT_CATALOG: Mapping[str, AgentTemplate] = {
    "amap-trip": AgentTemplate("上海 POI 与天气查询", _build_amap_trip),
    "maqami-flight": AgentTemplate("北京到上海航班报价查询", _build_maqami_flight),
}


class ConfiguredUseCase:
    """已由配置实例化的 Worker、FastAPI A2A 服务和 orchestration 运行句柄。"""

    def __init__(
        self,
        config: UseCaseConfig,
        harnesses: Sequence[ProviderHarness],
        endpoints: Sequence[A2AWorkerEndpoint],
        client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._harnesses = harnesses
        self._endpoints = endpoints
        self._client = client

    async def run(self, goal: str | None = None):
        request = OrchestrationRequest(
            workflow_id=self._config.use_case_id,
            session_id=self._config.session_id,
            goal=goal or self._config.goal,
            facts=self._config.facts,
            mode=OrchestrationMode.REACT,
        )
        manager = DeterministicMagenticManager(
            [endpoint.descriptor.worker_id for endpoint in self._endpoints],
            task_text=request.goal,
        )
        orchestration = create_react_workflow(
            self._endpoints,
            engine=self._config.engine,
            session_id=request.session_id,
            run_id=request.workflow_id,
            manager=manager,
            max_round_count=self._config.max_round_count,
            max_stall_count=self._config.max_stall_count,
            max_reset_count=self._config.max_reset_count,
            task_context=request.facts,
        )
        with timing_span("workflow:total"):
            stream = orchestration.workflow.run(request.goal, stream=True)
            async for _event in stream:
                pass
            final_response = await stream.get_final_response()
        return await orchestration.finalize(request, final_response)

    async def close(self) -> None:
        await asyncio.gather(*(harness.close() for harness in self._harnesses))
        await self._client.aclose()


def build_use_case(config: UseCaseConfig, serving: LlmServingSpec) -> ConfiguredUseCase:
    """按 catalog 选择已知模板，并用 FastAPI HTTP 路由暴露其 A2A 服务。"""
    if config.engine != "magentic":
        raise ValueError("本 demo 的已知依赖流程仅支持 orchestration.engine=magentic")
    sessions = InMemorySessionProvider()
    providers = HarnessProviders(session=sessions, contexts=(StaticContextProvider(
        config.context_label,
        config.context_content,
        config.context_priority,
    ),))
    harnesses: list[ProviderHarness] = []
    services: dict[str, A2AWorkerService] = {}
    descriptions: dict[str, str] = {}
    for worker in config.workers:
        template = AGENT_CATALOG.get(worker.template)
        if template is None:
            raise ValueError(f"未知 agent template: {worker.template!r}; 可选 {sorted(AGENT_CATALOG)}")
        harness = template.build_harness(worker, serving, providers)
        harnesses.append(harness)
        services[worker.endpoint_path] = A2AWorkerService(harness, worker.worker_id)
        descriptions[worker.worker_id] = template.description
    app = create_a2a_app(
        services,
        authenticator=NoAuthAuthenticator(),
        authorizer=AllowAllAuthorizer(),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-a2a")
    transport = HttpA2ATransport(client)
    endpoints = tuple(
        A2AWorkerEndpoint(
            WorkerDescriptor(worker.worker_id, f"http://local-a2a{worker.endpoint_path}",
                             frozenset({worker.template}), descriptions[worker.worker_id]),
            transport,
        )
        for worker in config.workers
    )
    return ConfiguredUseCase(config, harnesses, endpoints, client)
