"""使用 Magentic REACT 编排 AgentScope 高德与 LangGraph Maqami 两个 Harness worker。

流程由 Magentic manager 根据两个 WorkerDescriptor 的 agent card 自主拆分：
1. AgentScope 地图 worker 查询上海 POI 和未来一周天气，并选出适合游玩的日期；
2. LangGraph 航班 worker 从前序协作快照中读取已选日期，使用 Maqami 查询北京到上海单程航班报价；
3. Magentic 汇总两个 worker 的真实工具结果。

必填环境变量：
    UNIVERSAL_CONTRACT_LLM_BASE_URL
    UNIVERSAL_CONTRACT_LLM_API_KEY
    AMAP_MCP_ENDPOINT
可选环境变量：
    UNIVERSAL_CONTRACT_LLM_MODEL  # 默认 gpt-4o-mini

Maqami 使用匿名 Streamable HTTP endpoint，不需要 API key。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

# 脚本位于 ``demo/react-orchestration-2-agent/``，向上两层是仓库根目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.a2a import A2AWorkerEndpoint, A2AWorkerService, InMemoryA2ATransport
from common.contracts import (
    AgentIdentity,
    Capability,
    ContextItem,
    HarnessProviders,
    HarnessSpec,
    LlmServingSpec,
    McpServerSpec,
    McpTransport,
    OrchestrationMode,
    OrchestrationRequest,
    SharedSession,
    WorkerDescriptor,
)
from common.timing import timing_span
from harness import ProviderHarness
from harness.agentscope_mcp_adapter import AgentScopeMcpRuntimeAdapter
from harness.langgraph_mcp_adapter import LangGraphMcpRuntimeAdapter
from orchestration import create_react_workflow
from orchestration.magentic import DeterministicMagenticManager


MAQAMI_MCP_ENDPOINT = "https://mcp.maqami.co/"


def load_environment() -> None:
    """加载根目录 `.env`，但 shell 已显式指定的值始终优先。"""
    env_file = REPOSITORY_ROOT / ".env"
    if not env_file.is_file():
        return
    # 只支持本 demo 所需的简单 KEY=VALUE 形态，避免增加 dotenv 运行依赖。
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
    """构造两个 worker 与 Magentic manager 共用的中立 OpenAI 兼容 Serving 声明。"""
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


def amap_server_from_environment() -> McpServerSpec:
    """声明高德 worker 使用的受限 MCP 工具集合。"""
    endpoint = os.getenv("AMAP_MCP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("缺少 AMAP_MCP_ENDPOINT；请配置高德 Streamable HTTP MCP 地址。")
    return McpServerSpec(
        name="amap",
        transport=McpTransport.STREAMABLE_HTTP,
        url=endpoint,
        description="高德地点检索与天气查询服务。",
        allowed_tools=frozenset({"maps_text_search", "maps_weather"}),
        request_timeout_s=30,
    )


def maqami_flight_server() -> McpServerSpec:
    """声明匿名 Maqami MCP，并仅允许读取航班报价。"""
    return McpServerSpec(
        name="maqami-flight",
        transport=McpTransport.STREAMABLE_HTTP,
        url=MAQAMI_MCP_ENDPOINT,
        description="Maqami 航班报价服务，可查询指定日期的公开航班行程与价格。",
        allowed_tools=frozenset({"post_flights_rates"}),
        request_timeout_s=60,
    )


class InMemorySessions:
    """两个框架 worker 共用的中立会话存储，不持有任何框架的对象。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SharedSession] = {}

    async def load(self, session_id: str) -> SharedSession:
        return self._sessions.setdefault(session_id, SharedSession(session_id))

    async def save(self, session: SharedSession) -> None:
        self._sessions[session.session_id] = session


class EvidenceOnlyContext:
    """要求 worker 只将 MCP 实际返回的事实作为结论，防止用模型常识填补实时信息。"""

    async def provide(self, request, session: SharedSession) -> Sequence[ContextItem]:
        return [ContextItem(
            "evidence-policy",
            "请用中文回答。实时 POI、天气和航班信息必须来自本 worker 可用 MCP 工具。"
            "前序 worker 结果仅可作为下一次查询的输入事实；没有工具证据时必须明确说明无法确认。",
            priority=10,
        )]


def build_agentscope_map_worker(
    sessions: InMemorySessions,
    serving: LlmServingSpec,
    amap_server: McpServerSpec,
) -> tuple[ProviderHarness, A2AWorkerEndpoint]:
    """构建仅负责上海 POI 与天气的 AgentScope Harness，并包成 A2A worker。"""
    worker_id = "agentscope-amap-trip-worker"
    description = (
        "上海游玩与天气专家。使用高德 MCP 的 maps_text_search 查找适合游玩的上海 POI，"
        "并用 maps_weather 查询未来一周天气。必须从天气结果中选出一个具体日期，说明选择理由、"
        "POI 候选及其位置。它不查询或推荐航班。"
    )
    harness = ProviderHarness(
        HarnessSpec(
            identity=AgentIdentity(worker_id, "AgentScope 上海游玩天气 Agent", description),
            instructions=(
                "你是上海游玩和未来一周天气查询专家。先使用 maps_text_search 查询适合游玩的上海 POI，"
                "再使用 maps_weather 查询上海未来一周天气；根据返回的天气选择一个最适合游玩的具体日期。"
                "你的最终回答必须清楚列出推荐日期、天气依据和 POI 候选。不得查询航班，也不得编造数据。"
            ),
            requested_capabilities=frozenset({Capability.TOOL_LOOP}),
            tools=(amap_server,),
            llm_serving=serving,
        ),
        HarnessProviders(session=sessions, contexts=(EvidenceOnlyContext(),)),
        AgentScopeMcpRuntimeAdapter(max_tool_rounds=8),
    )
    endpoint_url = f"memory://{worker_id}"
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor(worker_id, endpoint_url, frozenset({"amap-poi-weather"}), description),
        InMemoryA2ATransport({endpoint_url: A2AWorkerService(harness, worker_id)}),
    )
    return harness, endpoint


def build_langgraph_flight_worker(
    sessions: InMemorySessions,
    serving: LlmServingSpec,
    search_server: McpServerSpec,
) -> tuple[ProviderHarness, A2AWorkerEndpoint]:
    """构建只查询航班的 LangGraph Harness，并包成 A2A worker。"""
    worker_id = "langgraph-maqami-flight-worker"
    description = (
        "北京到上海单程航班报价专家。读取协作快照中地图 worker 选出的具体日期，再用 Maqami MCP "
        "查询当天单程航班报价。报告工具结果明确提供的航班号、航司、起降时间、机场、价格与行李额度，"
        "并注明报价有效期与来源限制。它不选择旅游 POI，也不评估天气。"
    )
    harness = ProviderHarness(
        HarnessSpec(
            identity=AgentIdentity(worker_id, "LangGraph 北京上海航班 Agent", description),
            instructions=(
                "你是北京到上海单程航班信息专家。先从 Contract context 中定位前序地图 worker 已选出的具体出行日期，"
                "再调用 Maqami 的 post_flights_rates 查询该日期北京到上海的单程航班报价。查询时使用城市代码 "
                "BJS 到 SHA、1 名成人、CNY，且只查询直飞。只报告工具结果明确给出的航班事实，例如航班号、"
                "航司、起降时间、机场、价格与行李额度；无法确认的字段必须说明。不得预订、取消、支付、重新选择日期、POI 或天气。"
            ),
            requested_capabilities=frozenset({Capability.TOOL_LOOP}),
            tools=(search_server,),
            llm_serving=serving,
        ),
        HarnessProviders(session=sessions, contexts=(EvidenceOnlyContext(),)),
        LangGraphMcpRuntimeAdapter(),
    )
    endpoint_url = f"memory://{worker_id}"
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor(worker_id, endpoint_url, frozenset({"maqami-flight-rates"}), description),
        InMemoryA2ATransport({endpoint_url: A2AWorkerService(harness, worker_id)}),
    )
    return harness, endpoint


async def main(goal: str) -> None:
    """用 LLM 驱动的 Magentic REACT manager 协调两个受限 MCP worker。"""
    load_environment()
    serving = serving_from_environment()
    sessions = InMemorySessions()
    map_harness, map_endpoint = build_agentscope_map_worker(
        sessions,
        serving,
        amap_server_from_environment(),
    )
    flight_harness, flight_endpoint = build_langgraph_flight_worker(
        sessions,
        serving,
        maqami_flight_server(),
    )
    for harness in (map_harness, flight_harness):
        problems = await harness.validate()
        if problems:
            raise RuntimeError(f"Harness 声明不合法: {problems}")

    request = OrchestrationRequest(
        workflow_id="react-two-agent-trip",
        session_id="react-two-agent-trip-session",
        goal=goal,
        facts={"origin": "北京", "destination": "上海", "trip_type": "单程"},
        mode=OrchestrationMode.REACT,
    )
    # manager 只能看到两张 WorkerDescriptor card，不能直接访问任一 Harness 或 MCP。
    # 复杂意图存在严格数据依赖：航班日期必须由天气 worker 先选出，因此把这条
    # 工作流级约束交给 manager；具体查询内容仍由各自 agent card 与 Harness 决定。
    manager = DeterministicMagenticManager(
        [map_endpoint.descriptor.worker_id, flight_endpoint.descriptor.worker_id],
        task_text=request.goal,
    )
    orchestration = create_react_workflow(
        [map_endpoint, flight_endpoint],
        engine="magentic",
        session_id=request.session_id,
        run_id=request.workflow_id,
        manager=manager,
        max_round_count=2,
        max_stall_count=1,
        max_reset_count=0,
        task_context=request.facts,
    )
    try:
        with timing_span("workflow:total"):
            stream = orchestration.workflow.run(request.goal, stream=True)
            async for _event in stream:
                pass
            final_response = await stream.get_final_response()
        result = await orchestration.finalize(request, final_response)
        print(f"workflow={result.workflow_id} status={result.status}")
        print(result.summary)
        print(f"worker_results={len(result.worker_results)} rounds={len(result.rounds)}")
    finally:
        await map_harness.close()
        await flight_harness.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Magentic REACT 双 MCP worker 出行规划示例")
    parser.add_argument(
        "goal",
        nargs="?",
        default="看看上海有什么好玩的，以及之后一周天气如何，选一个天气好的日子，查一下当天单程北京到上海的航班信息。",
        help="需要协调地图天气与航班查询的复杂意图。",
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.goal))