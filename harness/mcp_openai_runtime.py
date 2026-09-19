"""支持 Streamable HTTP MCP 工具调用的 OpenAI 兼容 Harness 运行时。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from common.contracts import (
    AgentResult, Capability, HarnessExecutionContext, HarnessProviders, HarnessSpec,
    LlmServingSpec, McpServerSpec, McpTransport, RunningAgent,
)


def _tool_result_text(result: Any) -> str:
    """将 MCP 工具调用结果归一化为可安全交给模型的文本。"""
    # MCP 工具可选择返回结构化内容；优先保留其 JSON 结构，便于模型继续引用字段。
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    # 没有结构化内容时，标准 MCP 响应由多个内容块组成，需要逐项抽取文本。
    content = getattr(result, "content", ())
    parts = []
    for item in content:
        # TextContent 有 text 属性；其他内容类型退化为字符串，避免丢失工具反馈。
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
    # 多内容块使用换行拼接，以保留各块之间的语义边界。
    return "\n".join(parts)


class _McpOpenAIAgent:
    def __init__(
        self,
        spec: HarnessSpec,
        serving: LlmServingSpec,
        max_tool_rounds: int,
    ) -> None:
        # 保存中立声明；具体 MCP 连接和 OpenAI 客户端均按单次 run 生命周期创建。
        self._spec = spec
        # Serving 只在本运行时内部用于构造 SDK 客户端，不会写入会话或 A2A 载荷。
        self._serving = serving
        # 工具调用轮数是确定性预算，防止模型反复请求工具导致无界执行。
        self._max_tool_rounds = max_tool_rounds

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        # 延迟导入可使未安装 MCP SDK 的纯离线用户仍能使用其他 Harness runtime。
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            from openai import AsyncOpenAI
        except ImportError as error:  # pragma: no cover - 部署环境依赖
            raise ImportError(
                "McpOpenAICompatibleRuntimeAdapter 需要安装 mcp 和 openai（pip install -r requirements.txt）。"
            ) from error

        # 当前实现将一个 Harness agent 绑定到一个 Streamable HTTP MCP server，
        # 以便其允许工具集、超时与连接生命周期都可明确审计。
        server = self._single_streamable_server()
        # server 的超时优先；未声明时依次退回 Serving 超时和 30 秒默认值。
        timeout = server.request_timeout_s or self._serving.timeout_s or 30
        # 建立 MCP 双向消息流。退出 async with 时 SDK 会停止接收任务并关闭 HTTP 资源。
        async with streamablehttp_client(
            server.url,
            headers=dict(server.headers) or None,
            timeout=timeout,
        ) as (read_stream, write_stream, _get_session_id):
            # ClientSession 承担 initialize、tools/list 与 tools/call 的协议状态管理。
            async with ClientSession(read_stream, write_stream) as mcp_session:
                # 初始化会协商 MCP 版本与 server capabilities，必须先于任何工具操作。
                await mcp_session.initialize()
                # 运行时发现 server 实际暴露的工具，不能假设声明时的工具目录仍然有效。
                listed_tools = (await mcp_session.list_tools()).tools
                # 仅保留满足白名单且不在黑名单中的工具；黑名单优先由 _tool_is_allowed 保证。
                tool_map = {
                    tool.name: tool
                    for tool in listed_tools
                    if self._tool_is_allowed(tool.name, server)
                }
                if not tool_map:
                    raise RuntimeError(
                        f"MCP server {server.name!r} 未发现允许使用的工具；请检查 allowed_tools 配置。"
                    )
                # 让模型在受限工具表内循环调用，直至生成自然语言终态。
                output = await self._complete_with_tools(mcp_session, tool_map, context)
            # MCP 连接正常关闭后，将最终文本包装为框架无关的 AgentResult。
        return AgentResult("completed", output=output)

    async def run_stream(self, context: HarnessExecutionContext):
        # 基础适配器只产生一次完整结果；其返回形状仍符合 Harness 的流式协议。
        yield await self.run(context)

    def _single_streamable_server(self) -> McpServerSpec:
        # 多 server 路由尚未实现，因此在构造前显式拒绝模糊配置。
        if len(self._spec.tools) != 1:
            raise ValueError("McpOpenAICompatibleRuntimeAdapter 当前要求每个 HarnessSpec 恰好声明一个 MCP server。")
        server = self._spec.tools[0]
        # 此 runtime 只实现官方 SDK 的 Streamable HTTP transport，不静默降级其他传输。
        if server.transport != McpTransport.STREAMABLE_HTTP:
            raise ValueError("McpOpenAICompatibleRuntimeAdapter 目前仅支持 STREAMABLE_HTTP MCP server。")
        if not server.url:
            raise ValueError("MCP server url 不能为空。")
        return server

    @staticmethod
    def _tool_is_allowed(tool_name: str, server: McpServerSpec) -> bool:
        # 空白名单表示 server 的全部工具均可用；非空时名称必须被显式列出。
        return (
            (not server.allowed_tools or tool_name in server.allowed_tools)
            and tool_name not in server.blocked_tools
        )

    async def _complete_with_tools(self, mcp_session: Any, tool_map: Mapping[str, Any], context: HarnessExecutionContext) -> str:
        from openai import AsyncOpenAI

        # Harness 已排序上下文；按标签展开后随用户任务一并交给模型。
        context_text = "\n".join(f"[{item.label}] {item.content}" for item in context.context_items)
        # 初始消息只含中立请求、注入上下文和 agent 静态指令。
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._spec.instructions},
            {
                "role": "user",
                "content": f"{context.request.input}\n\nContract context:\n{context_text}",
            },
        ]
        # 将运行时发现的 MCP inputSchema 映射为 OpenAI-compatible function schema。
        tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description or name,
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
            }
            for name, tool in tool_map.items()
        ]
        # 具体 OpenAI SDK 客户端只存在于该框架 runtime 内，保持 Harness contract 无 SDK 依赖。
        client = AsyncOpenAI(
            api_key=self._serving.api_key,
            base_url=self._serving.base_url,
            timeout=self._serving.timeout_s,
            default_headers=dict(self._serving.extra_headers),
        )
        try:
            # 每轮最多允许模型返回一批工具调用；总轮数受构造参数的确定性上限约束。
            for tool_round in range(self._max_tool_rounds):
                response = await client.chat.completions.create(
                    model=self._serving.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self._serving.temperature,
                    max_tokens=self._serving.max_tokens,
                )
                message = response.choices[0].message
                # 空工具调用表示模型已基于现有上下文和工具结果生成最终答案。
                tool_calls = message.tool_calls or ()
                # 把 assistant 原始消息保留在历史中，使后续 tool 结果能关联到对应调用。
                messages.append(message.model_dump(exclude_none=True))
                if not tool_calls:
                    return message.content or "模型未返回最终说明。"
                if tool_round == self._max_tool_rounds - 1:
                    # 最后一轮已不能继续访问外部工具；明确指示模型仅总结已取得的证据。
                    messages.append({
                        "role": "user",
                        "content": (
                            "The MCP tool-call budget is exhausted. Do not call tools again. "
                            "Give a concise final answer using only the tool results already returned."
                        ),
                    })
                    final_response = await client.chat.completions.create(
                        model=self._serving.model,
                        messages=messages,
                        temperature=self._serving.temperature,
                        max_tokens=self._serving.max_tokens,
                    )
                    return final_response.choices[0].message.content or "模型未返回最终说明。"
                # 同一模型回复可能并列请求多个工具；逐项校验、调用并回灌结果。
                for call in tool_calls:
                    if call.function.name not in tool_map:
                        raise RuntimeError(f"模型请求了未允许的 MCP 工具: {call.function.name}")
                    try:
                        # function.arguments 是模型生成的 JSON 字符串，必须先解析并检查对象形状。
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError as error:
                        raise RuntimeError(f"模型为工具 {call.function.name} 生成了无效 JSON 参数。") from error
                    if not isinstance(arguments, dict):
                        raise RuntimeError(f"模型为工具 {call.function.name} 生成的参数必须是 JSON 对象。")
                    # 只有通过权限校验的函数名和 JSON 对象参数才能跨出进程调用 MCP server。
                    tool_result = await mcp_session.call_tool(call.function.name, arguments)
                    # OpenAI 工具协议要求 tool_call_id 与此前 assistant 消息中的调用 ID 精确关联。
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _tool_result_text(tool_result),
                        }
                    )
        finally:
            # 无论工具、模型或协议调用是否抛错，均关闭 SDK HTTP 客户端释放连接池资源。
            await client.close()
        raise RuntimeError("模型工具调用循环意外结束。")


class McpOpenAICompatibleRuntimeAdapter:
    """将一个 ``McpServerSpec`` 翻译为真实的 OpenAI 函数调用循环。"""

    runtime_name = "mcp-openai-compatible"

    def __init__(self, default_serving: LlmServingSpec | None = None, *, max_tool_rounds: int = 6) -> None:
        # 可由外部提供运行时默认 Serving；HarnessSpec 自己的 Serving 优先级更高。
        self._default_serving = default_serving
        # 该值传给实际 RunningAgent，成为单次模型工具循环的硬上限。
        self._max_tool_rounds = max_tool_rounds

    def capabilities(self) -> frozenset[Capability]:
        # 只有真实执行模型-工具-模型循环的 adapter 才声明 TOOL_LOOP。
        return frozenset({Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT, Capability.TOOL_LOOP})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> RunningAgent:
        # 本实现尚无 Skill 文件发现和注入机制，必须显式失败，防止调用方误判已加载。
        if spec.skills:
            raise ValueError("McpOpenAICompatibleRuntimeAdapter 尚不支持 Skill 声明。")
        # agent 专属配置覆盖 adapter 默认配置，允许同一进程内不同 Harness 使用不同服务。
        serving = spec.llm_serving or self._default_serving
        if serving is None:
            raise ValueError("HarnessSpec.llm_serving 未配置，且 runtime 也没有 default_serving。")
        return _McpOpenAIAgent(spec, serving, self._max_tool_rounds)

    async def close(self) -> None:
        # 连接资源在每次 RunningAgent.run() 的上下文管理器中关闭，adapter 本身无长驻资源。
        return None