"""通过 Universal Contract Harness 运行 AgentScope 高德 MCP 智能体。

本示例从仓库根目录 `.env` 读取以下配置：
    UNIVERSAL_CONTRACT_LLM_BASE_URL
    UNIVERSAL_CONTRACT_LLM_API_KEY
    UNIVERSAL_CONTRACT_LLM_MODEL  # 可选，默认 gpt-4o-mini
    AMAP_MCP_ENDPOINT

在仓库根目录运行：
    conda run -n uc python demo/agentscope-harness-mcp/demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 当前文件位于 ``demo/agentscope-harness-mcp/``，向上两层即仓库根目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# 直接执行脚本时，显式加入根目录以导入仓库内的 common 与 harness 包。
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


def load_environment() -> None:
    """读取本地 `.env`，且不覆盖调用 shell 已设置的环境变量。"""
    env_file = REPOSITORY_ROOT / ".env"
    if not env_file.is_file():
        return
    # 仅解析常见的 KEY=VALUE 格式，避免演示引入额外 dotenv 依赖。
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
    """将非敏感环境变量名对应的 Serving 配置转为中立契约。"""
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
    """构造高德 MCP 的中立声明，不在代码或输出中泄漏 endpoint。"""
    endpoint = os.getenv("AMAP_MCP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("缺少高德 MCP 配置: AMAP_MCP_ENDPOINT")
    return McpServerSpec(
        name="amap",
        transport=McpTransport.STREAMABLE_HTTP,
        url=endpoint,
        description="高德地图：地址地理编码、公共交通路径规划与周边地点检索。",
        # 只开放本示例实际需要的工具，避免将完整高德工具面暴露给模型。
        allowed_tools=frozenset({
            "maps_geo",
            "maps_direction_transit_integrated",
            "maps_around_search",
        }),
        request_timeout_s=30,
    )


def build_agent(
    serving: LlmServingSpec,
    amap_server: McpServerSpec,
    sessions: InMemorySessionProvider,
) -> ProviderHarness:
    """以中立的 Harness 声明装配 AgentScope MCP runtime。"""
    return ProviderHarness(
        HarnessSpec(
            identity=AgentIdentity(
                agent_id="agentscope-amap-agent",
                name="AgentScope 高德地图 Agent",
                description="通过 AgentScope 原生 MCP Toolkit 执行地图查询的 Harness agent。",
            ),
            instructions=(
                "你是地图出行助手。需要实时地图信息时，主动调用可用高德 MCP 工具。"
                "同一会话的后续问题应复用前文已经确认的地点；回答简明，且不要编造工具未返回的数据。"
            ),
            requested_capabilities=frozenset({Capability.TOOL_LOOP}),
            tools=(amap_server,),
            llm_serving=serving,
        ),
        HarnessProviders(session=sessions, contexts=(StaticContextProvider(
            "amap-tool-policy",
            "请用中文回答。涉及地点、路线或周边商户的实时信息时，必须调用已提供的高德工具；"
            "只依据工具结果回答，无法确认的信息请明确说明。",
        ),)),
        # 具体 adapter 将 McpServerSpec 翻译成 MCPClient -> Toolkit -> Agent。
        AgentScopeMcpRuntimeAdapter(max_tool_rounds=8),
    )


async def main() -> None:
    """在一个持久 Harness 会话中执行三轮依赖高德 MCP 能力的真实对话。"""
    load_environment()
    serving = serving_from_environment()
    amap_server = amap_server_from_environment()
    agent = build_agent(serving, amap_server, InMemorySessionProvider())
    problems = await agent.validate()
    if problems:
        raise RuntimeError(f"Harness 声明不合法: {problems}")

    # 三轮问题共享 session_id。第二、三轮既能利用 Harness 的中立会话上下文，
    # 也会复用 AgentScope 缓存 Agent 所保有的框架原生对话状态。
    session_id = "agentscope-amap-mcp-demo"
    turns = (
        "北京长春桥在哪里？请查询并简要说明它的位置。",
        "从刚才的长春桥到北京佳程广场，公共交通怎么走？请给出主要换乘信息。",
        "佳程广场附近有什么适合吃猪蹄的地方？请查询后给出几家候选。",
    )
    try:
        # 仅输出可公开的模型名和会话标识，MCP endpoint 与密钥绝不输出。
        print(f"AgentScope model={serving.model}; MCP multi-turn session={session_id}")
        for turn_number, user_input in enumerate(turns, start=1):
            result = await agent.run(AgentRequest(
                request_id=f"agentscope-amap-turn-{turn_number}",
                session_id=session_id,
                input=user_input,
            ))
            print(f"\nUser {turn_number}: {user_input}")
            print(f"Agent: {result.output}")
    finally:
        # 将关闭职责交还给 Harness，再由 adapter 按自身资源模型完成释放。
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())