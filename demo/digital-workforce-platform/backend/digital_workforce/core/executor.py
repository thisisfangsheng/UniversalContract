from __future__ import annotations

import hashlib
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
UC_ROOT = Path(__file__).resolve().parents[5]
if str(UC_ROOT) not in sys.path:
    sys.path.insert(0, str(UC_ROOT))

from common.contracts import LeaderPolicy, OrchestrationMode, OrchestrationPlan, OrchestrationRequest, Reflection, ReflectionPolicy, RoundRecord, WorkerDescriptor, WorkerTask

from .presets import TEMPLATES


def resolve_request(payload: dict[str, Any]) -> dict[str, Any]:
    template_id = payload.get("template_id")
    template = TEMPLATES.get(template_id) if template_id else None
    if template_id and template is None:
        raise ValueError(f"未知 template_id: {template_id}")
    source = template or {}
    mode = source.get("mode", payload.get("mode", "sequential"))
    worker_ids = list(source.get("worker_ids", payload.get("worker_ids") or []))
    if not worker_ids:
        raise ValueError("worker_ids 不能为空，或提供 template_id")
    try:
        OrchestrationMode(mode)
    except ValueError as error:
        raise ValueError(f"不支持的 mode: {mode}") from error
    facts = {**dict(source.get("facts", {})), **dict(payload.get("facts") or {})}
    for item in payload.get("context") or []:
        label = item.get("label")
        content = item.get("content")
        if not isinstance(label, str) or not isinstance(content, str):
            raise ValueError("context 每项需要 label 与 content 字符串")
        facts[f"context:{label}"] = content
    return {
        "template_id": template_id,
        "mode": mode,
        "worker_ids": worker_ids,
        "goal": str(payload.get("goal") or source.get("goal") or ""),
        "facts": facts,
        "react": source.get("react", payload.get("react") or {}),
        "session_id": payload.get("session_id") or f"ses_{uuid.uuid4().hex}",
    }


class DeterministicLeader(LeaderPolicy):
    """Offline planner used by M1. It preserves UC mode semantics without an LLM."""

    async def create_plan(self, request: OrchestrationRequest, workers: Sequence[WorkerDescriptor]) -> OrchestrationPlan:
        tasks: list[WorkerTask] = []
        previous_task_id: str | None = None
        for index, worker in enumerate(workers, start=1):
            task_id = f"{request.workflow_id}:task:{index}"
            key = hashlib.sha256(f"{request.workflow_id}:{task_id}".encode()).hexdigest()[:16]
            depends_on = (previous_task_id,) if previous_task_id and request.mode != OrchestrationMode.CONCURRENT else ()
            tasks.append(WorkerTask(
                task_id=task_id,
                worker_id=worker.worker_id,
                instruction=f"围绕目标完成你的职责：{request.goal}",
                input={"goal": request.goal, "requires_approval": request.mode == OrchestrationMode.HANDOFF and worker.worker_id == "writer"},
                idempotency_key=key,
                depends_on=depends_on,
            ))
            previous_task_id = task_id
        return OrchestrationPlan(f"plan-{request.workflow_id}", request.mode, tuple(tasks), "all")


class DeterministicReflector(ReflectionPolicy):
    """Offline REACT policy: execute, revise from the round output, then converge."""

    async def reflect(
        self,
        request: OrchestrationRequest,
        history: Sequence[RoundRecord],
        workers: Sequence[WorkerDescriptor],
    ) -> Reflection:
        if any(result.status != "completed" for record in history for result in record.results):
            return Reflection("failed", "离线反思器检测到未完成回执")
        if len(history) == 1:
            worker_ids = {worker.worker_id for worker in workers}
            reviser = "writer" if "writer" in worker_ids else history[-1].results[-1].worker_id
            task_id = f"{request.workflow_id}:react:revision:r2"
            return Reflection(
                "replan",
                "首轮结果已生成，需要根据协作回执进行一次整合与修订",
                new_tasks=(WorkerTask(
                    task_id=task_id,
                    worker_id=reviser,
                    instruction="阅读首轮协作回执与会话上下文，整合分歧并输出修订后的最终结论。",
                    input={"goal": request.goal, "react_phase": "revision"},
                    idempotency_key=hashlib.sha256(f"{request.workflow_id}:{task_id}".encode()).hexdigest()[:16],
                ),),
                next_facts={"react:first_round": "已完成，等待修订整合"},
            )
        return Reflection("done", "离线反思器确认首轮任务已完成")


class ResumePlanLeader(LeaderPolicy):
    """Deterministic resume leader that runs only stored successor workers."""

    def __init__(self, tasks: Sequence[WorkerTask]) -> None:
        self._tasks = tuple(tasks)

    async def create_plan(self, request: OrchestrationRequest, workers: Sequence[WorkerDescriptor]) -> OrchestrationPlan:
        return OrchestrationPlan(f"plan-{request.workflow_id}", request.mode, self._tasks, "all", round=2)


def result_payload(result: Any) -> dict[str, Any]:
    return {
        "workflow_id": result.workflow_id,
        "status": result.status,
        "summary": result.summary,
        "worker_results": [asdict(item) for item in result.worker_results],
        "rounds": [asdict(item) for item in result.rounds],
    }
