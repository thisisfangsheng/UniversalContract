"""通过 UniversalContract Harness 契约运行 AgentScope 智能体。

本示例从仓库根目录 `.env` 读取 OpenAI 兼容 Serving：
    UNIVERSAL_CONTRACT_LLM_BASE_URL
    UNIVERSAL_CONTRACT_LLM_API_KEY
    UNIVERSAL_CONTRACT_LLM_MODEL  # 可选，默认 gpt-4o-mini

在仓库根目录运行：
    conda run -n uc python demo/agentscope-harness/demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 从当前文件的 ``demo/agentscope-harness/`` 逐级回退两层，得到仓库根目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# 直接执行本文件时，Python 只会把当前目录加入模块搜索路径；这里显式加入根目录，
# 使 ``common`` 和 ``harness`` 这两个仓库内包可以被稳定导入。
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from common.contracts import (
    AgentIdentity,
    AgentRequest,
    HarnessProviders,
    HarnessSpec,
    LlmServingSpec,
)
from common.providers import InMemorySessionProvider, StaticContextProvider
from harness import ProviderHarness
from harness.agentscope_adapter import AgentScopeRuntimeAdapter


def load_environment() -> None:
    """读取仓库根目录 `.env`，shell 中已设置的值具有更高优先级。"""
    # `.env` 仅是本地开发便利配置，不是必须存在的运行依赖。
    env_file = REPOSITORY_ROOT / ".env"
    if not env_file.is_file():
        return
    # 逐行解析简单的 KEY=VALUE 形式，避免为演示增加额外 dotenv 依赖。
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 跳过空行和注释行，保持与常见 shell 环境文件一致。
        if not line or line.startswith("#"):
            continue
        # 支持 ``export KEY=VALUE``，以便同一份文件也可被 shell source。
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        # 仅接受合法变量名，去除最外层引号，并用 setdefault 保留 shell 优先级。
        if separator and name and name.replace("_", "").isalnum():
            os.environ.setdefault(name, value.strip().strip("'\""))


def serving_from_environment() -> LlmServingSpec:
    """把环境变量转换为 HarnessSpec 可携带的中立 Serving 声明。"""
    # 只读取名称，不记录或打印密钥值，避免将敏感配置泄漏到日志。
    base_url = os.getenv("UNIVERSAL_CONTRACT_LLM_BASE_URL")
    api_key = os.getenv("UNIVERSAL_CONTRACT_LLM_API_KEY")
    # 两项连接信息必须同时存在；收集缺项可使启动错误一次性可操作。
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
    # LlmServingSpec 属于框架无关契约，具体 AgentScope 参数转换留在 build_agent。
    return LlmServingSpec(
        model=os.getenv("UNIVERSAL_CONTRACT_LLM_MODEL", "gpt-4o-mini"),
        base_url=base_url,
        api_key=api_key,
        temperature=0.2,
    )


def build_agent(serving: LlmServingSpec, sessions: InMemorySessionProvider) -> ProviderHarness:
    """在适配器装配边界将 Harness Serving 声明翻译为 AgentScope 原生模型。"""
    # OpenAICredential 是 AgentScope 的认证对象；配置仍来自上层中立的 LlmServingSpec。
    model = OpenAIChatModel(
        credential=OpenAICredential(api_key=serving.api_key, base_url=serving.base_url),
        model=serving.model,
        parameters=OpenAIChatModel.Parameters(temperature=serving.temperature),
        stream=False,
    )
    return ProviderHarness(
        # HarnessSpec 只描述身份、指令和 Serving，不暴露 AgentScope 对象给通用层。
        HarnessSpec(
            identity=AgentIdentity(
                agent_id="agentscope-conversation-agent",
                name="AgentScope 对话 Agent",
                description="通过 UniversalContract Harness 执行的 AgentScope 多轮对话 agent。",
            ),
            instructions=(
                "You are a helpful assistant. Maintain the current conversation and answer each request "
                "using the supplied contract context."
            ),
            llm_serving=serving,
        ),
        # 会话存取和策略上下文均按 contract Provider 接口注入 Harness 生命周期。
        HarnessProviders(session=sessions, contexts=(StaticContextProvider(
            "conversation-policy",
            "Answer in Chinese. Be concise and preserve relevant facts from earlier turns.",
        ),)),
        # 此处是唯一持有 AgentScope 原生模型的具体框架适配器。
        AgentScopeRuntimeAdapter(model),
    )


async def main() -> None:
    """构造一个 Harness agent，并以同一会话标识执行三轮真实模型对话。"""
    # 先读取本地配置，再将其转为中立 Serving 声明；缺失配置会在网络调用前失败。
    load_environment()
    serving = serving_from_environment()
    sessions = InMemorySessionProvider()
    agent = build_agent(serving, sessions)
    # 在发出任何模型请求前检查 Harness 声明与 runtime 能力是否相容。
    problems = await agent.validate()
    if problems:
        raise RuntimeError(f"Harness 声明不合法: {problems}")

    # 三个请求共享一个 session_id，ProviderHarness 会将每轮输入和输出累积到 SharedSession。
    session_id = "agentscope-harness-demo"
    turns = (
        "我叫小林，正在计划周末去北京。请记住我的名字和目的地。",
        "我刚才叫什么名字，计划去哪里？",
        "请根据刚才的信息，用一句话给我一个周末出行建议。",
    )
    try:
        # 不输出 base URL 和 API key，只显示模型名与非敏感会话标识。
        print(f"AgentScope model={serving.model}; multi-turn session={session_id}")
        for turn_number, user_input in enumerate(turns, start=1):
            # 每轮使用新的 request_id 便于审计，同时复用 session_id 保持对话连续性。
            result = await agent.run(AgentRequest(
                request_id=f"agentscope-turn-{turn_number}",
                session_id=session_id,
                input=user_input,
            ))
            # AgentResult.output 是 adapter 对 AgentScope 原生回复归一化后的文本。
            print(f"\nUser {turn_number}: {user_input}")
            print(f"Agent: {result.output}")
    finally:
        # 无论模型调用是否成功，都透传 Harness.close() 以释放 runtime 可能持有的资源。
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
