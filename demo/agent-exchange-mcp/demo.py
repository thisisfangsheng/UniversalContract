"""在同一 Harness 会话中交替运行 AgentScope 与 LangGraph 高德 MCP agent。

本示例证明会话连续性属于 Universal Contract 的 ``SharedSession``，而不是
任何框架私有状态：AgentScope 写入的用户问题与回答会由 ProviderHarness 保存，
LangGraph 在后续轮次通过相同的 contract 上下文读取它；随后切回 AgentScope
仍可得到包含所有框架轮次的会话记录。

在仓库根目录 `.env` 或 shell 配置：
    UNIVERSAL_CONTRACT_LLM_BASE_URL
    UNIVERSAL_CONTRACT_LLM_API_KEY
    UNIVERSAL_CONTRACT_LLM_MODEL  # 可选，默认 gpt-4o-mini
    AMAP_MCP_ENDPOINT

在仓库根目录运行：
    /home/sean/.miniforge3/envs/uc/bin/python demo/agent-exchange-mcp/demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 当前文件位于 ``demo/agent-exchange-mcp/``，向上两层即仓库根目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.contracts import (
    AgentIdentity,
    AgentRequest,
    Capability,
    HarnessProviders,
    HarnessSpec,
    LlmServingSpec,
    McpServerSpec,
    McpTransport,
)
from common.providers import InMemorySessionProvider, StaticContextProvider
from harness import ProviderHarness
from harness.agentscope_mcp_adapter import AgentScopeMcpRuntimeAdapter
from harness.langgraph_mcp_adapter import LangGraphMcpRuntimeAdapter


def load_environment() -> None:
    """读取根目录 `.env`，并保持 shell 显式传入的变量优先。"""
    env_file = REPOSITORY_ROOT / ".env"
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
    """把本地 Serving 环境变量转成两个 runtime 共用的中立声明。"""
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
    """声明两个 runtime 都能各自翻译和执行的最小高德工具面。"""
    endpoint = os.getenv("AMAP_MCP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("缺少高德 MCP 配置: AMAP_MCP_ENDPOINT")
    return McpServerSpec(
        name="amap",
        transport=McpTransport.STREAMABLE_HTTP,
        url=endpoint,
        description="高德地图的地址地理编码、公交路径规划和周边地点检索能力。",
        allowed_tools=frozenset({
            "maps_geo",
            "maps_direction_transit_integrated",
            "maps_around_search",
        }),
        request_timeout_s=30,
    )


def shared_spec(serving: LlmServingSpec, amap_server: McpServerSpec) -> HarnessSpec:
    """创建由 AgentScope 与 LangGraph 共同遵守的一份静态 Harness 声明。"""
    return HarnessSpec(
        identity=AgentIdentity(
            agent_id="amap-runtime-exchange-agent",
            name="高德地图运行时交换 Agent",
            description="可在 AgentScope 与 LangGraph runtime 间切换的地图 Harness agent。",
        ),
        instructions=(
            "你是地图出行助手。需要实时地图信息时必须调用可用的高德 MCP 工具。"
            "后续提问中的“刚才”“那里”“目的地”等指代，应从 Contract context 的会话历史解析。"
            "回答简洁，并明确区分高德工具已确认的信息与无法确认的信息。"
        ),
        requested_capabilities=frozenset({Capability.TOOL_LOOP}),
        tools=(amap_server,),
        llm_serving=serving,
    )


def build_agents(
    spec: HarnessSpec,
    providers: HarnessProviders,
) -> tuple[ProviderHarness, ProviderHarness]:
    """为同一 contract 声明装配两个不同框架的运行时实现。"""
    return (
        ProviderHarness(spec, providers, AgentScopeMcpRuntimeAdapter(max_tool_rounds=8)),
        ProviderHarness(spec, providers, LangGraphMcpRuntimeAdapter()),
    )


async def run_turn(
    runtime_name: str,
    agent: ProviderHarness,
    sessions: InMemorySessionProvider,
    session_id: str,
    turn_number: int,
    user_input: str,
) -> None:
    """执行一轮并显示共享会话累积情况，方便观察 runtime 交换后的连续性。"""
    result = await agent.run(AgentRequest(
        request_id=f"runtime-exchange-turn-{turn_number}",
        session_id=session_id,
        input=user_input,
    ))
    session = await sessions.load(session_id)
    print(f"\n[{runtime_name}] User {turn_number}: {user_input}")
    print(f"[{runtime_name}] Agent: {result.output}")
    # 每轮会追加一条 user 和一条 assistant 消息；跨 runtime 的累计数是可观察证据。
    print(f"[contract] shared_session_messages={len(session.messages)}")


async def main() -> None:
    """执行 AgentScope -> LangGraph -> AgentScope 的五轮高德 MCP 连续对话。"""
    load_environment()
    serving = serving_from_environment()
    sessions = InMemorySessionProvider()
    spec = shared_spec(serving, amap_server_from_environment())
    providers = HarnessProviders(session=sessions, contexts=(StaticContextProvider(
        "amap-tool-policy",
        "请用中文回答。地点、路线或周边商户的实时信息必须调用已提供的高德工具。"
        "可以使用 contract.session.messages 中的前序结论，但不得编造工具未返回的信息。",
    ),))
    agentscope_agent, langgraph_agent = build_agents(spec, providers)
    for agent in (agentscope_agent, langgraph_agent):
        problems = await agent.validate()
        if problems:
            raise RuntimeError(f"Harness 声明不合法: {problems}")

    session_id = "agent-exchange-amap-mcp-demo"
    turns = (
        ("AgentScope", agentscope_agent, "请查询北京长春桥的位置，并把它作为之后路线规划的出发地。"),
        ("AgentScope", agentscope_agent, "请查询北京佳程广场的位置，并记住它是这次出行的目的地。"),
        ("LangGraph", langgraph_agent, "根据刚才确认的出发地和目的地，查询公共交通路线并给出主要换乘。"),
        ("LangGraph", langgraph_agent, "继续基于刚才的目的地，查询附近适合吃猪蹄的地方，给出几家候选。"),
        ("AgentScope", agentscope_agent, "回顾整个对话：出发地和目的地分别是什么？再用一句话概括刚才查询的路线和餐饮建议。"),
    )
    try:
        print(f"model={serving.model}; shared session={session_id}")
        for turn_number, (runtime_name, agent, user_input) in enumerate(turns, start=1):
            await run_turn(
                runtime_name,
                agent,
                sessions,
                session_id,
                turn_number,
                user_input,
            )
    finally:
        await agentscope_agent.close()
        await langgraph_agent.close()


if __name__ == "__main__":
    asyncio.run(main())