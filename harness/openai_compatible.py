"""OpenAI 兼容 Serving 的 Harness runtime 与编排策略实现。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from common.contracts import (
    AgentResult, HarnessExecutionContext, HarnessProviders, HarnessSpec,
    LeaderPolicy, LlmServingSpec, OrchestrationMode, OrchestrationPlan,
    OrchestrationRequest, Reflection, ReflectionPolicy, RoundRecord,
    RunningAgent, WorkerDescriptor, WorkerTask,
)

Completion = Callable[[LlmServingSpec, Sequence[Mapping[str, str]]], Awaitable[str]]


async def openai_chat_completion(
    serving: LlmServingSpec,
    messages: Sequence[Mapping[str, str]],
    *,
    json_object: bool = False,
) -> str:
    """通过 OpenAI Python SDK 调用任意 Chat Completions 兼容端点。"""
    try:
        from openai import AsyncOpenAI
    except ImportError as error:  # pragma: no cover - depends on deployment extras
        raise ImportError("OpenAICompatibleRuntimeAdapter 需要安装 openai（pip install -r requirements.txt）。") from error

    client = AsyncOpenAI(
        api_key=serving.api_key,
        base_url=serving.base_url,
        timeout=serving.timeout_s,
        default_headers=dict(serving.extra_headers),
    )
    try:
        request = {
            "model": serving.model,
            "messages": list(messages),
            "temperature": serving.temperature,
            "max_tokens": serving.max_tokens,
        }
        if json_object:
            request["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(
            **request,  # type: ignore[arg-type]
        )
        return response.choices[0].message.content or ""
    finally:
        await client.close()


class _OpenAICompatibleAgent:
    def __init__(self, spec: HarnessSpec, serving: LlmServingSpec, complete: Completion) -> None:
        self._spec = spec
        self._serving = serving
        self._complete = complete

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        context_text = "\n".join(f"[{item.label}] {item.content}" for item in context.context_items)
        output = await self._complete(self._serving, (
            {"role": "system", "content": self._spec.instructions},
            {"role": "user", "content": f"{context.request.input}\n\nContract context:\n{context_text}"},
        ))
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = None
        return AgentResult("completed", output, data=data if isinstance(data, dict) else None)

    async def run_stream(self, context: HarnessExecutionContext):
        yield await self.run(context)


class OpenAICompatibleRuntimeAdapter:
    """将 HarnessSpec.llm_serving 翻译为 OpenAI 兼容 Chat Completions 调用。"""

    runtime_name = "openai-compatible"

    def __init__(self, default_serving: LlmServingSpec | None = None, *, complete: Completion = openai_chat_completion) -> None:
        self._default_serving = default_serving
        self._complete = complete

    def capabilities(self):
        from common.contracts import Capability
        return frozenset({Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> RunningAgent:
        if spec.tools:
            raise ValueError("OpenAICompatibleRuntimeAdapter 基础实现尚未翻译 MCP 声明。")
        serving = spec.llm_serving or self._default_serving
        if serving is None:
            raise ValueError("HarnessSpec.llm_serving 未配置，且 runtime 也没有 default_serving。")
        return _OpenAICompatibleAgent(spec, serving, self._complete)

    async def close(self) -> None:
        return None


def _json_response(text: str) -> Mapping[str, Any]:
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    if not text:
        raise ValueError(
            "LLM 返回了空响应，无法生成结构化编排结果；请确认模型支持 Chat Completions "
            "及 JSON object response_format，并检查模型名与 Serving 网关配置。"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("LLM 必须返回 JSON 对象。") from error
    if not isinstance(value, dict):
        raise ValueError("LLM 必须返回 JSON 对象。")
    return value


def _worker_task_from_json(
    task: Any,
    *,
    workflow_id: str,
    task_index: int,
    round_number: int,
) -> WorkerTask:
    """将 LLM 的业务决策规范化为任务；可靠性标识由确定性代码生成。"""
    if not isinstance(task, Mapping):
        raise ValueError("LLM 的 tasks/new_tasks 每一项都必须是 JSON 对象。")
    instruction = (
        task.get("instruction")
        or task.get("task")
        or task.get("description")
        or task.get("task_description")
        or task.get("objective")
        or task.get("content")
    )
    worker_id = task.get("worker_id") or task.get("worker") or task.get("assignee")
    missing = [
        name for name, value in (
            ("worker_id", worker_id),
            ("instruction", instruction),
        ) if not value
    ]
    if missing:
        raise ValueError(
            f"LLM 返回的任务缺少业务字段 {missing}；任务必须包含 worker_id（或 worker/assignee）"
            "和 instruction（或 task/description/objective/content）。"
        )
    input_value = task.get("input", {})
    if not isinstance(input_value, Mapping):
        raise ValueError("LLM 返回的任务 input 必须是 JSON 对象。")
    return WorkerTask(
        task_id=str(task.get("task_id") or f"{workflow_id}-r{round_number}-t{task_index}"),
        worker_id=str(worker_id),
        instruction=str(instruction),
        input=dict(input_value),
        idempotency_key=str(task.get("idempotency_key") or f"{workflow_id}:{worker_id}:r{round_number}:t{task_index}"),
        depends_on=tuple(task.get("depends_on", ())),
    )


class OpenAICompatibleLeader(LeaderPolicy):
    """使用 Serving 生成单轮或 REACT 第一轮计划，随后转换为中立契约。"""

    def __init__(self, serving: LlmServingSpec, *, complete: Completion | None = None) -> None:
        self._serving = serving
        self._complete = complete

    async def create_plan(self, request: OrchestrationRequest, workers: Sequence[WorkerDescriptor]) -> OrchestrationPlan:
        catalog = [{"worker_id": item.worker_id, "capabilities": sorted(item.capabilities)} for item in workers]
        prompt = {
            "goal": request.goal, "facts": request.facts, "mode": request.mode,
            "workers": catalog,
            "response_schema": {"tasks": [{"worker_id": "catalog worker id", "instruction": "specific task text", "input": {}, "depends_on": []}], "completion_rule": "all|any"},
        }
        messages = (
            {"role": "system", "content": "You are an orchestration planner. Return only valid JSON matching response_schema."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        )
        text = (
            await openai_chat_completion(self._serving, messages, json_object=True)
            if self._complete is None else await self._complete(self._serving, messages)
        )
        payload = _json_response(text)
        tasks = tuple(
            _worker_task_from_json(task, workflow_id=request.workflow_id, task_index=index, round_number=1)
            for index, task in enumerate(payload.get("tasks", ()), start=1)
        )
        return OrchestrationPlan(
            plan_id=str(payload.get("plan_id", f"plan-{request.workflow_id}")), mode=request.mode, tasks=tasks,
            completion_rule=str(payload.get("completion_rule", "all")),
        )


class OpenAICompatibleReflector(ReflectionPolicy):
    """使用 Serving 决定 REACT 是否追加任务；流程护栏仍由 ReflectExecutor 执行。"""

    def __init__(self, serving: LlmServingSpec, *, complete: Completion | None = None) -> None:
        self._serving = serving
        self._complete = complete

    async def reflect(self, request: OrchestrationRequest, history: Sequence[RoundRecord], workers: Sequence[WorkerDescriptor]) -> Reflection:
        prompt = {
            "goal": request.goal, "facts": request.facts,
            "history": [{"round": item.round, "results": [{"task_id": result.task_id, "worker_id": result.worker_id, "status": result.status, "output": result.output, "error": result.error} for result in item.results]} for item in history],
            "workers": [{"worker_id": item.worker_id, "capabilities": sorted(item.capabilities)} for item in workers],
            "response_schema": {"decision": "done|failed|replan", "reason": "string", "new_tasks": [{"worker_id": "catalog worker id", "instruction": "specific next task", "input": {}}], "next_facts": {}},
        }
        messages = (
            {"role": "system", "content": "You are an orchestration reflector. Return only valid JSON matching response_schema."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
        )
        text = (
            await openai_chat_completion(self._serving, messages, json_object=True)
            if self._complete is None else await self._complete(self._serving, messages)
        )
        payload = _json_response(text)
        next_round = len(history) + 1
        tasks = tuple(
            _worker_task_from_json(
                task, workflow_id=request.workflow_id, task_index=index, round_number=next_round,
            )
            for index, task in enumerate(payload.get("new_tasks", ()), start=1)
        )
        return Reflection(
            decision=str(payload["decision"]), reason=str(payload.get("reason", "")),
            new_tasks=tasks, next_facts=dict(payload.get("next_facts", {})),
        )  # type: ignore[arg-type]