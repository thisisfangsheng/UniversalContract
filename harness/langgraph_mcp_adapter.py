"""将 Universal Contract 的 MCP 声明翻译为 LangGraph ReAct agent。

本模块是 LangGraph、LangChain MCP adapter 与通用 Harness 契约之间的唯一
边界。它负责发现 MCP 工具、按契约白名单过滤工具，并将 LangGraph 最终消息
归一化为 ``AgentResult``；ProviderHarness 与调用 demo 不接触框架私有对象。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

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
    from langchain_core.messages import HumanMessage
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
except ImportError as error:  # pragma: no cover - 依赖由使用方决定
    raise ImportError(
        "LangGraphMcpRuntimeAdapter 需要安装 langgraph、langchain-openai "
        "和 langchain-mcp-adapters。"
    ) from error


def _message_text(message: Any) -> str:
    """将 LangChain 消息的文本或内容块安全转换为 contract 文本输出。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, str) or isinstance(item, dict)
        ]
        return "\n".join(part for part in text_parts if part)
    return str(content)


class LangGraphMcpRunningAgent:
    """包装已构建的 LangGraph 图，使其满足中立的 ``RunningAgent`` 协议。"""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        # ContextItem 包含 ProviderHarness 保存的历史消息，因而本 runtime 不必
        # 读取或依赖 AgentScope、LangGraph 等任一框架的私有记忆格式。
        context_text = "\n".join(
            f"[{item.label}] {item.content}" for item in context.context_items
        )
        with timing_span("langgraph:agent-run"):
            state = await self._graph.ainvoke({
                "messages": [HumanMessage(
                    content=f"{context.request.input}\n\nContract context:\n{context_text}",
                )],
            })
        messages = state.get("messages", [])
        if not messages:
            raise RuntimeError("LangGraph 未返回任何消息。")
        output = _message_text(messages[-1])
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = None
        return AgentResult(
            status="completed",
            output=output,
            run_id=getattr(messages[-1], "id", None),
            data=data if isinstance(data, dict) else None,
        )

    async def run_stream(
        self,
        context: HarnessExecutionContext,
    ) -> AsyncIterator[AgentResult]:
        """当前以完整终态适配流式协议，避免暴露 LangGraph 专有事件格式。"""
        yield await self.run(context)


def _is_transient_connection_error(error: BaseException) -> bool:
    """Return whether an error only contains retryable HTTP transport failures."""
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _is_transient_connection_error(child) for child in error.exceptions
        )
    return False


async def _retry_transient_connection_error(
    operation: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 3,
) -> Any:
    """Retry a bounded number of transient MCP transport failures."""
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as error:
            if attempt == attempts - 1 or not _is_transient_connection_error(error):
                raise
            await asyncio.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


class _TransientMcpConnectionRetry:
    """LangChain MCP interceptor for short-lived connection failures."""

    async def __call__(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        with timing_span(f"mcp:{request.server_name}:{request.name}"):
            return await _retry_transient_connection_error(lambda: handler(request))


class LangGraphMcpRuntimeAdapter:
    """用 LangGraph ReAct 图实现 Harness 的 OpenAI 兼容 MCP agent。"""

    runtime_name = "langgraph-mcp"

    def __init__(self, default_serving: LlmServingSpec | None = None) -> None:
        self._default_serving = default_serving

    def capabilities(self) -> frozenset[Capability]:
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
        """发现受限 MCP 工具，并将它们装配进 LangGraph 的工具调用循环。"""
        serving = spec.llm_serving or self._default_serving
        if serving is None:
            raise ValueError("HarnessSpec.llm_serving 未配置，且 runtime 没有 default_serving。")

        servers = [server for server in spec.tools if server.load_tools]
        connections = {
            server.name: self._connection_from_server(server)
            for server in servers
        }
        client = MultiServerMCPClient(
            connections,
            tool_name_prefix=False,
            tool_interceptors=[_TransientMcpConnectionRetry()],
        )
        with timing_span("langgraph:mcp-tool-discovery"):
            discovered_tools = await _retry_transient_connection_error(client.get_tools)
        tools = [
            tool
            for tool in discovered_tools
            if self._tool_is_allowed(tool.name, servers)
        ]
        if not tools:
            raise RuntimeError("未发现任何符合 Harness MCP 工具权限声明的 LangGraph 工具。")

        # ChatOpenAI 接受 OpenAI 兼容网关地址；密钥只在本 adapter 创建的模型内存中存在。
        model = ChatOpenAI(
            model=serving.model,
            api_key=serving.api_key,
            base_url=serving.base_url,
            temperature=serving.temperature,
            timeout=serving.timeout_s,
            max_completion_tokens=serving.max_tokens,
            default_headers=dict(serving.extra_headers) or None,
        )
        graph = create_react_agent(model, tools, prompt=spec.instructions)
        return LangGraphMcpRunningAgent(graph)

    @staticmethod
    def _connection_from_server(server: McpServerSpec) -> dict[str, Any]:
        """将 contract 的 Streamable HTTP 声明映射为 LangChain MCP 连接配置。"""
        if server.transport is not McpTransport.STREAMABLE_HTTP:
            raise ValueError(
                f"LangGraphMcpRuntimeAdapter 仅支持 STREAMABLE_HTTP，"
                f"server {server.name!r} 的 transport={server.transport!s} 不受支持。"
            )
        parsed_url = urlsplit(server.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(
                f"MCP server {server.name!r} 的 url 必须是完整的 http(s) 地址。"
            )
        connection: dict[str, Any] = {
            "transport": "streamable_http",
            "url": server.url,
        }
        if server.headers:
            connection["headers"] = dict(server.headers)
        if server.request_timeout_s is not None:
            connection["timeout"] = server.request_timeout_s
        return connection

    @staticmethod
    def _tool_is_allowed(tool_name: str, servers: list[McpServerSpec]) -> bool:
        """执行 contract 的白名单/黑名单规则；黑名单优先于白名单。"""
        return any(
            (not server.allowed_tools or tool_name in server.allowed_tools)
            and tool_name not in server.blocked_tools
            for server in servers
        )

    async def close(self) -> None:
        """LangChain MCP adapter 按工具调用创建临时会话，runtime 本身无长驻连接。"""
        return None