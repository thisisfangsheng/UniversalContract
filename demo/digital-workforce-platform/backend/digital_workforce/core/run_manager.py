from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[3]
UC_ROOT = Path(__file__).resolve().parents[5]
if str(UC_ROOT) not in sys.path:
    sys.path.insert(0, str(UC_ROOT))

from common.contracts import OrchestrationMode, OrchestrationRequest, WorkerTask
from orchestration import create_workflow

from ..models import ApprovalRecord, AuditRecord, OrchestrationRecord
from .event_bridge import EventBridge, EventedWorkerEndpoint
from .executor import DeterministicLeader, DeterministicReflector, ResumePlanLeader, resolve_request, result_payload
from .registry import WorkerRegistry


class WorkflowRunManager:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], registry: WorkerRegistry, events: EventBridge) -> None:
        self._sessions = sessions
        self._registry = registry
        self._events = events
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def create(self, payload: dict[str, Any], tenant_id: str) -> dict[str, str]:
        effective = resolve_request(payload)
        continued_from = payload.get("continued_from")
        if continued_from:
            effective["continued_from"] = str(continued_from)
        if not effective["goal"].strip():
            raise ValueError("goal 不能为空")
        for worker_id in effective["worker_ids"]:
            worker = await self._registry.get_worker(worker_id, tenant_id)
            if not worker.enabled:
                raise ValueError(f"worker {worker_id} 已下线")
        orchestration_id = f"orc_{uuid.uuid4().hex}"
        record = OrchestrationRecord(
            orchestration_id=orchestration_id,
            tenant_id=tenant_id,
            workflow_id=f"wf_{orchestration_id}",
            session_id=effective["session_id"],
            goal=effective["goal"],
            mode=effective["mode"],
            effective_json=json.dumps(effective, ensure_ascii=False),
        )
        async with self._sessions() as session:
            session.add(record)
            session.add(AuditRecord(tenant_id=tenant_id, orchestration_id=orchestration_id, kind="queued", payload_json=json.dumps(effective, ensure_ascii=False)))
            await session.commit()
        await self._events.emit(tenant_id, orchestration_id, "plan", {"status": "queued", "effective": effective})
        self._tasks[orchestration_id] = asyncio.create_task(self._run(orchestration_id))
        return {"orchestration_id": orchestration_id, "session_id": effective["session_id"], "status": "queued"}

    async def continue_from(self, orchestration_id: str, payload: dict[str, Any], tenant_id: str) -> dict[str, str]:
        async with self._sessions() as session:
            parent = await session.get(OrchestrationRecord, orchestration_id)
            if parent is None or parent.tenant_id != tenant_id:
                raise KeyError(orchestration_id)
            parent_effective = json.loads(parent.effective_json)
        continuation = {
            "goal": payload.get("goal", ""),
            "mode": payload.get("mode") or parent.mode,
            "worker_ids": payload.get("worker_ids") or parent_effective.get("worker_ids", []),
            "session_id": parent.session_id,
            "facts": {**parent_effective.get("facts", {}), **dict(payload.get("facts") or {})},
            "context": payload.get("context") or [],
            "react": payload.get("react") or parent_effective.get("react", {}),
            "continued_from": orchestration_id,
        }
        return await self.create(continuation, tenant_id)

    async def _run(self, orchestration_id: str) -> None:
        async with self._sessions() as session:
            record = await session.get(OrchestrationRecord, orchestration_id)
            if record is None or record.status == "cancelled":
                return
            record.status = "running"
            await session.commit()
            effective = json.loads(record.effective_json)
            tenant_id = record.tenant_id
        try:
            endpoints = await self._registry.endpoints_for(effective["worker_ids"], tenant_id)
            endpoints = [EventedWorkerEndpoint(endpoint, self._events, tenant_id, orchestration_id) for endpoint in endpoints]
            request = OrchestrationRequest(record.workflow_id, record.session_id, record.goal, effective["facts"], OrchestrationMode(record.mode))
            leader = DeterministicLeader()
            plan = await leader.create_plan(request, [endpoint.descriptor for endpoint in endpoints])
            await self._events.emit(tenant_id, orchestration_id, "plan", {"plan_id": plan.plan_id, "tasks": [
                {"task_id": task.task_id, "worker_id": task.worker_id, "instruction": task.instruction, "depends_on": task.depends_on}
                for task in plan.tasks
            ]})
            react_engine = "builtin"
            manager_agent = None
            if request.mode == OrchestrationMode.REACT and self._registry.llm_serving is not None:
                from orchestration.magentic import create_openai_compatible_manager_agent
                react_engine = "magentic"
                manager_agent = create_openai_compatible_manager_agent(
                    self._registry.llm_serving,
                    f"当前用户目标是：{request.goal}\n"
                    "先根据参与者的能力形成计划；每轮阅读回执，必要时补充、修订或交叉验证，"
                    "只有目标已满足时才结束。不要按固定顺序逐个点名。",
                )
            workflow = create_workflow(
                request,
                endpoints,
                leader=leader,
                reflector=DeterministicReflector() if request.mode == OrchestrationMode.REACT else None,
                react_engine=react_engine if request.mode == OrchestrationMode.REACT else "magentic",
                manager_agent=manager_agent,
                max_rounds=int(effective.get("react", {}).get("max_rounds", 3)),
            )
            if request.mode == OrchestrationMode.REACT:
                workflow.reset()
                if react_engine == "magentic":
                    manager_input = f"用户目标：{request.goal}\n业务事实：{json.dumps(dict(request.facts), ensure_ascii=False)}"
                    stream = workflow.workflow.run(manager_input, stream=True)
                    async for _event in stream:
                        pass
                    run_result = await stream.get_final_response()
                else:
                    run_result = await workflow.workflow.run(request)
                result = await workflow.finalize(request, run_result)
            else:
                run_result = await workflow.run(request)
                result = list(run_result.get_outputs())[-1]
            payload = result_payload(result)
            provider = self._registry.session_provider_for(tenant_id)
            shared_session = await provider.load(record.session_id)
            session_payload = {
                "session_id": shared_session.session_id,
                "facts": shared_session.facts,
                "messages": shared_session.messages,
                "runtime_references": shared_session.runtime_references,
            }
            async with self._sessions() as session:
                current = await session.get(OrchestrationRecord, orchestration_id)
                if current is None or current.status == "cancelled":
                    return
                current.status = result.status
                current.result_json = json.dumps(payload, ensure_ascii=False, default=str)
                current.session_json = json.dumps(session_payload, ensure_ascii=False, default=str)
                current.completed_at = datetime.now(timezone.utc)
                session.add(AuditRecord(tenant_id=tenant_id, orchestration_id=orchestration_id, kind="result", payload_json=current.result_json))
                if result.status == "needs_approval":
                    pending = next(item for item in result.worker_results if item.status == "needs_approval")
                    source_task = next(task for task in plan.tasks if task.task_id == pending.task_id)
                    session.add(ApprovalRecord(
                        tenant_id=tenant_id,
                        orchestration_id=orchestration_id,
                        task_id=source_task.task_id,
                        worker_id=source_task.worker_id,
                        idempotency_key=source_task.idempotency_key,
                        task_json=json.dumps({"task": self._task_payload(source_task), "worker_ids": effective["worker_ids"], "facts": effective["facts"]}, ensure_ascii=False),
                    ))
                    current.status = "waiting_approval"
                await session.commit()
            if result.status == "needs_approval":
                await self._events.emit(tenant_id, orchestration_id, "approval_required", {
                    "task_id": pending.task_id, "worker_id": pending.worker_id, "instruction": source_task.instruction,
                })
            await self._events.emit(tenant_id, orchestration_id, "aggregate", {"result": payload})
        except Exception as error:
            async with self._sessions() as session:
                current = await session.get(OrchestrationRecord, orchestration_id)
                if current is not None:
                    current.status = "failed"
                    current.error = str(error)
                    current.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                    await self._events.emit(tenant_id, orchestration_id, "failed", {"error": str(error)})

    async def get(self, orchestration_id: str, tenant_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            record = await session.get(OrchestrationRecord, orchestration_id)
            if record is None or record.tenant_id != tenant_id:
                return None
            return self._view(record)

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            records = (await session.scalars(select(OrchestrationRecord).where(OrchestrationRecord.tenant_id == tenant_id).order_by(OrchestrationRecord.created_at.desc()))).all()
            return [self._view(record) for record in records]

    async def approve(self, orchestration_id: str, task_id: str, decision: str, note: str, tenant_id: str) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision 必须为 approve 或 reject")
        async with self._sessions() as session:
            approval = await session.scalar(select(ApprovalRecord).where(
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.orchestration_id == orchestration_id,
                ApprovalRecord.task_id == task_id,
            ))
            record = await session.get(OrchestrationRecord, orchestration_id)
            if approval is None or record is None:
                raise KeyError(task_id)
            if approval.status != "pending":
                raise RuntimeError("审批已处理，禁止重复投递")
            approval.status = "resolved"
            approval.decision_json = json.dumps({"decision": decision, "note": note}, ensure_ascii=False)
            approval.resolved_at = datetime.now(timezone.utc)
            record.status = "running"
            await session.commit()
        await self._events.emit(tenant_id, orchestration_id, "approval_resolved", {"task_id": task_id, "decision": decision, "note": note})
        self._tasks[orchestration_id] = asyncio.create_task(self._resume(orchestration_id, task_id, decision, note))
        return {"status": "running", "task_id": task_id}

    async def _resume(self, orchestration_id: str, task_id: str, decision: str, note: str) -> None:
        async with self._sessions() as session:
            record = await session.get(OrchestrationRecord, orchestration_id)
            approval = await session.scalar(select(ApprovalRecord).where(ApprovalRecord.orchestration_id == orchestration_id, ApprovalRecord.task_id == task_id))
            if record is None or approval is None:
                return
            tenant_id = record.tenant_id
            stored = json.loads(approval.task_json)
        source = stored["task"]
        task = WorkerTask(source["task_id"], source["worker_id"], source["instruction"], source["input"], source["idempotency_key"], tuple(source.get("depends_on", ())), resume={"approved": decision == "approve", "note": note})
        endpoints = await self._registry.endpoints_for([task.worker_id], tenant_id)
        resumed_endpoint = EventedWorkerEndpoint(endpoints[0], self._events, tenant_id, orchestration_id)
        receipt = await resumed_endpoint.execute(task, record.session_id)
        successor_ids = stored["worker_ids"][stored["worker_ids"].index(task.worker_id) + 1:]
        successor_receipts = []
        if decision == "approve" and successor_ids:
            successors = await self._registry.endpoints_for(successor_ids, tenant_id)
            evented = [EventedWorkerEndpoint(endpoint, self._events, tenant_id, orchestration_id) for endpoint in successors]
            resume_request = OrchestrationRequest(f"{record.workflow_id}:resume1", record.session_id, record.goal, {**stored["facts"], "resume_receipt": dict(receipt.output)}, OrchestrationMode.SEQUENTIAL)
            resume_tasks = tuple(WorkerTask(
                task_id=f"{resume_request.workflow_id}:task:{index}", worker_id=worker_id,
                instruction=f"审批完成后继续：{record.goal}", input={"goal": record.goal},
                idempotency_key=f"{orchestration_id}:resume:{index}",
            ) for index, worker_id in enumerate(successor_ids, start=1))
            workflow = create_workflow(resume_request, evented, leader=ResumePlanLeader(resume_tasks))
            run_result = await workflow.run(resume_request)
            resumed = list(run_result.get_outputs())[-1]
            successor_receipts = list(resumed.worker_results)
        result = {"status": "completed" if receipt.status == "completed" and decision == "approve" else "failed", "worker_results": [self._result_payload(receipt), *[self._result_payload(item) for item in successor_receipts] ]}
        async with self._sessions() as session:
            current = await session.get(OrchestrationRecord, orchestration_id)
            if current is not None:
                current.status = result["status"]
                current.result_json = json.dumps(result, ensure_ascii=False, default=str)
                current.completed_at = datetime.now(timezone.utc)
                session.add(AuditRecord(tenant_id=tenant_id, orchestration_id=orchestration_id, kind="approval_resume", payload_json=current.result_json))
                await session.commit()
        await self._events.emit(tenant_id, orchestration_id, "aggregate", {"result": result})

    async def cancel(self, orchestration_id: str, tenant_id: str) -> bool:
        async with self._sessions() as session:
            record = await session.get(OrchestrationRecord, orchestration_id)
            if record is None or record.tenant_id != tenant_id:
                return False
            record.status = "cancelled"
            task = self._tasks.get(orchestration_id)
            if task is not None:
                task.cancel()
            await session.commit()
            await self._events.emit(tenant_id, orchestration_id, "failed", {"status": "cancelled"})
            return True

    @staticmethod
    def _task_payload(task: WorkerTask) -> dict[str, Any]:
        return {"task_id": task.task_id, "worker_id": task.worker_id, "instruction": task.instruction, "input": dict(task.input), "idempotency_key": task.idempotency_key, "depends_on": task.depends_on}

    @staticmethod
    def _result_payload(result) -> dict[str, Any]:
        return {"task_id": result.task_id, "worker_id": result.worker_id, "status": result.status, "output": dict(result.output), "error": result.error}

    @staticmethod
    def _view(record: OrchestrationRecord) -> dict[str, Any]:
        return {
            "orchestration_id": record.orchestration_id,
            "workflow_id": record.workflow_id,
            "session_id": record.session_id,
            "goal": record.goal,
            "mode": record.mode,
            "status": record.status,
            "effective": json.loads(record.effective_json),
            "continued_from": json.loads(record.effective_json).get("continued_from"),
            "result": json.loads(record.result_json) if record.result_json else None,
            "session": json.loads(record.session_json) if record.session_json else None,
            "error": record.error,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
