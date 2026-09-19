"""AgentScope 对接 Universal Contract Harness 的标准运行时适配器。

本模块是唯一允许同时依赖 Universal Contract 与 AgentScope 的边界层。它将
``HarnessSpec`` 翻译为 AgentScope ``Agent``，并将 ``Agent.reply()`` 的原生
消息归一化为 contract ``AgentResult``；编排器、A2A 与会话层不需要了解
AgentScope 的任何类型。

此基础适配器覆盖纯模型调用、共享会话与 JSON 结构化输出。若 HarnessSpec
声明 MCP server 或 Skill，则会明确失败；生产项目应在此基础上扩展 Toolkit
翻译，且只在扩展实现中声明 ``Capability.TOOL_LOOP``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from common.contracts import (
    AgentResult,
    Capability,
    HarnessExecutionContext,
    HarnessProviders,
    HarnessSpec,
    RunningAgent,
)

try:
    from agentscope.agent import Agent
    from agentscope.model import ChatModelBase
    from agentscope.message import UserMsg
except ImportError as error:  # pragma: no cover - 依赖由使用方决定
    raise ImportError(
        "AgentScopeRuntimeAdapter 需要安装 agentscope，或将其 src 目录加入 PYTHONPATH。"
    ) from error


class AgentScopeRunningAgent:
    """将一个 AgentScope ``Agent`` 包装为 contract ``RunningAgent``。"""

    def __init__(self, agent: Agent) -> None:
        # 框架原生对象只保留在 runtime adapter 的运行侧。
        self._agent = agent

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        # ContextItem 已由 ProviderHarness 按优先级排序，其中 contract.* 条目
        # 自动携带协作快照和持久会话；适配器只做统一的原生消息映射。
        injected_context = "\n".join(
            f"[{item.label}] {item.content}" for item in context.context_items
        )
        # 将中立请求和中立上下文转换为 AgentScope 可消费的 UserMsg。
        message = UserMsg(
            name="orchestrator",
            content=f"{context.request.input}\n\nContract context:\n{injected_context}",
        )
        # AgentScope 负责模型调用及其内部状态；共享业务会话仍由 ProviderHarness 管理。
        reply = await self._agent.reply(message)
        output = reply.get_text_content() or ""

        # 约定：JSON 对象文本同步填充给 AgentResult.data，非 JSON 输出仍保留为文本。
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = None

        return AgentResult(
            status="completed",
            output=output,
            run_id=reply.id,
            data=data if isinstance(data, dict) else None,
        )

    async def run_stream(
        self,
        context: HarnessExecutionContext,
    ) -> AsyncIterator[AgentResult]:
        # 基础实现以一个完整结果适配流式协议；可在子类中替换为 token 级转发。
        yield await self.run(context)


class AgentScopeRuntimeAdapter:
    """AgentScope 接入 ``HarnessRuntimeAdapter`` 的标准基础实现。"""

    # ProviderHarness 用此稳定名称记录本 runtime 的 run_id。
    runtime_name = "agentscope"

    def __init__(self, model: ChatModelBase) -> None:
        # 每个 adapter 持有一个由调用方配置好的 AgentScope 聊天模型。
        self._model = model

    def capabilities(self) -> frozenset[Capability]:
        # Durable session 由 contract 的 SessionProvider 实现；JSON 对象映射为结构化输出。
        return frozenset({Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT})

    async def build(
        self,
        spec: HarnessSpec,
        providers: HarnessProviders,
    ) -> RunningAgent:
        # 不支持的声明必须显式报错，避免调用者误以为 MCP/Skill 已被加载。
        if spec.tools or spec.skills:
            raise ValueError("AgentScopeRuntimeAdapter 基础实现尚未翻译 MCP 或 Skill 声明。")

        # 用唯一的中立 spec 字段构造 AgentScope 原生 agent。
        agent = Agent(
            name=spec.identity.agent_id,
            system_prompt=spec.instructions,
            model=self._model,
        )
        return AgentScopeRunningAgent(agent)

    async def close(self) -> None:
        # 基础 AgentScope Agent 没有统一的关闭 API；资源型扩展可覆盖此方法。
        return None