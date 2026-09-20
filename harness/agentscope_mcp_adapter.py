"""将 Universal Contract 的 MCP 声明翻译为 AgentScope 原生 Toolkit。

本模块是 AgentScope MCP 集成的唯一框架边界：``HarnessSpec.tools`` 中的
中立 ``McpServerSpec`` 在这里转换为 ``MCPClient``、``HttpMCPConfig`` 与
``Toolkit``。真正的工具发现、模型工具调用循环和 MCP 协议执行均由
AgentScope ``Agent`` 完成；ProviderHarness 与 demo 都不直接操作 MCP。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from common.contracts import (
    AgentResult,
    Capability,
    HarnessExecutionContext,
    HarnessProviders,
    HarnessSpec,
    LlmServingSpec,
    McpServerSpec,
    McpTransport,
    RunningAgent,
)
from common.timing import timing_span

try:
    from agentscope.agent import Agent, ReActConfig
    from agentscope.event import (
        ConfirmResult,
        RequireExternalExecutionEvent,
        RequireUserConfirmEvent,
        UserConfirmResultEvent,
    )
    from agentscope.mcp import HttpMCPConfig, MCPClient
    from agentscope.message import Msg, UserMsg
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from agentscope.tool import Toolkit
except ImportError as error:  # pragma: no cover - 依赖由使用方决定
    raise ImportError(
        "AgentScopeMcpRuntimeAdapter 需要安装 agentscope 与 mcp。"
    ) from error


class AgentScopeMcpRunningAgent:
    """自动确认契约已授权的 MCP 调用，并继续 AgentScope 的原生工具循环。"""

    def __init__(self, agent: Agent, mcp_tool_prefixes: frozenset[str]) -> None:
        self._agent = agent
        self._mcp_tool_prefixes = mcp_tool_prefixes

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        """将 contract 上下文映射为消息，并处理 AgentScope 要求的 MCP 确认事件。"""
        injected_context = "\n".join(
            f"[{item.label}] {item.content}" for item in context.context_items
        )
        next_input: UserMsg | UserConfirmResultEvent = UserMsg(
            name="orchestrator",
            content=f"{context.request.input}\n\nContract context:\n{injected_context}",
        )

        with timing_span("agentscope:agent-run"):
            while True:
                final_reply: Msg | None = None
                confirmation_event: RequireUserConfirmEvent | None = None
                async for item in self._agent.reply_stream(
                    next_input,
                    yield_final_msg=True,
                ):
                    if isinstance(item, Msg):
                        final_reply = item
                    elif isinstance(item, RequireUserConfirmEvent):
                        confirmation_event = item
                    elif isinstance(item, RequireExternalExecutionEvent):
                        raise RuntimeError(
                            "AgentScope 请求由宿主执行工具；此 adapter 仅支持由 AgentScope "
                            "本地执行的 MCPTool。"
                        )

                if confirmation_event is None:
                    if final_reply is None:
                        raise RuntimeError("AgentScope 未产出最终回复。")
                    output = final_reply.get_text_content() or ""
                    try:
                        data = json.loads(output)
                    except json.JSONDecodeError:
                        data = None
                    return AgentResult(
                        status="completed",
                        output=output,
                        run_id=final_reply.id,
                        data=data if isinstance(data, dict) else None,
                    )

                unrecognized_tools = [
                    tool_call.name
                    for tool_call in confirmation_event.tool_calls
                    if not tool_call.name.startswith(tuple(self._mcp_tool_prefixes))
                ]
                if unrecognized_tools:
                    raise RuntimeError(
                        f"AgentScope 请求确认未被契约授权的工具: {unrecognized_tools}"
                    )
                next_input = UserConfirmResultEvent(
                    reply_id=confirmation_event.reply_id,
                    confirm_results=[
                        ConfirmResult(confirmed=True, tool_call=tool_call)
                        for tool_call in confirmation_event.tool_calls
                    ],
                )

    async def run_stream(
        self,
        context: HarnessExecutionContext,
    ) -> AsyncIterator[AgentResult]:
        """先以完整结果适配流式契约；token 级转发可由后续实现扩展。"""
        yield await self.run(context)


class AgentScopeMcpRuntimeAdapter:
    """以 AgentScope MCP Toolkit 实现 Harness 的工具循环能力。

    当前实现支持 Streamable HTTP MCP。每个 HTTP client 使用无状态模式，
    因此 AgentScope 会在每次工具调用期间自行创建和释放协议会话；adapter
    本身不持有跨请求的网络连接。AgentScope 的 ``Agent`` 对象仍由
    ProviderHarness 缓存，从而保留其框架原生的对话状态。
    """

    runtime_name = "agentscope-mcp"

    def __init__(
        self,
        default_serving: LlmServingSpec | None = None,
        *,
        max_tool_rounds: int = 6,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds 必须大于 0。")
        self._default_serving = default_serving
        self._max_tool_rounds = max_tool_rounds

    def capabilities(self) -> frozenset[Capability]:
        """声明 Harness 启动前校验所需的 AgentScope 原生能力。"""
        return frozenset({
            Capability.DURABLE_SESSION,
            Capability.STRUCTURED_OUTPUT,
            Capability.TOOL_LOOP,
        })

    async def build(
        self,
        spec: HarnessSpec,
        providers: HarnessProviders,
    ) -> RunningAgent:
        """将 MCP 声明转换为 Toolkit，再创建具备原生工具循环的 Agent。"""
        serving = spec.llm_serving or self._default_serving
        if serving is None:
            raise ValueError("HarnessSpec.llm_serving 未配置，且 runtime 没有 default_serving。")

        mcp_clients = [
            self._build_mcp_client(server)
            for server in spec.tools
            if server.load_tools
        ]
        toolkit = Toolkit(mcps=mcp_clients)
        model = OpenAIChatModel(
            credential=OpenAICredential(
                api_key=serving.api_key,
                base_url=serving.base_url,
            ),
            model=serving.model,
            parameters=OpenAIChatModel.Parameters(temperature=serving.temperature),
            stream=False,
        )
        agent = Agent(
            name=spec.identity.agent_id,
            system_prompt=spec.instructions,
            model=model,
            toolkit=toolkit,
            react_config=ReActConfig(max_iters=self._max_tool_rounds),
        )
        return AgentScopeMcpRunningAgent(
            agent,
            frozenset(f"mcp__{server.name}__" for server in spec.tools if server.load_tools),
        )

    def _build_mcp_client(self, server: McpServerSpec) -> MCPClient:
        """将单个 Streamable HTTP 声明映射成受最小权限约束的 MCPClient。"""
        if server.transport is not McpTransport.STREAMABLE_HTTP:
            raise ValueError(
                f"AgentScopeMcpRuntimeAdapter 仅支持 STREAMABLE_HTTP，"
                f"server {server.name!r} 的 transport={server.transport!s} 不受支持。"
            )
        if not server.url:
            raise ValueError(f"MCP server {server.name!r} 缺少 url。")
        parsed_url = urlsplit(server.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(
                f"MCP server {server.name!r} 的 url 必须是完整的 http(s) 地址；"
                "请检查环境变量是否包含 https:// 前缀，且没有使用未展开的变量引用。"
            )

        # Contract 规定黑名单优先。AgentScope 不允许 enable/disable 重叠，
        # 因而先从白名单中移除被禁用项，再交给其原生过滤机制执行。
        enabled_tools = (
            sorted(server.allowed_tools - server.blocked_tools)
            if server.allowed_tools
            else None
        )
        disabled_tools = sorted(server.blocked_tools) or None
        return MCPClient(
            name=server.name,
            is_stateful=False,
            mcp_config=HttpMCPConfig(
                url=server.url,
                headers=dict(server.headers) or None,
                timeout=server.request_timeout_s,
            ),
            enable_tools=enabled_tools,
            disable_tools=disabled_tools,
            execution_timeout=server.request_timeout_s,
        )

    async def close(self) -> None:
        """无状态 HTTP MCP 不保留连接；资源由每次 AgentScope 工具调用释放。"""
        return None