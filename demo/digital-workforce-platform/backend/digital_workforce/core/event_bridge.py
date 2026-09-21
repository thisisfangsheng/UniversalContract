from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import WorkerResult, WorkerTask

from ..models import EventRecord


class EventBridge:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._locks: dict[str, asyncio.Lock] = {}

    async def emit(self, tenant_id: str, orchestration_id: str, event_type: str, payload: Mapping[str, Any]) -> int:
        lock = self._locks.setdefault(orchestration_id, asyncio.Lock())
        async with lock:
            async with self._sessions() as session:
                current = await session.scalar(select(func.max(EventRecord.seq)).where(EventRecord.orchestration_id == orchestration_id))
                sequence = int(current or 0) + 1
                session.add(EventRecord(
                    tenant_id=tenant_id,
                    orchestration_id=orchestration_id,
                    seq=sequence,
                    type=event_type,
                    payload_json=json.dumps(dict(payload), ensure_ascii=False, default=str),
                ))
                await session.commit()
                return sequence

    async def records(self, tenant_id: str, orchestration_id: str, after: int = 0) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(EventRecord).where(
                EventRecord.tenant_id == tenant_id,
                EventRecord.orchestration_id == orchestration_id,
                EventRecord.seq > after,
            ).order_by(EventRecord.seq))).all()
            return [self._view(row) for row in rows]

    async def stream(self, tenant_id: str, orchestration_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        last_sequence = after
        while True:
            records = await self.records(tenant_id, orchestration_id, last_sequence)
            if records:
                for record in records:
                    last_sequence = record["seq"]
                    yield record
            else:
                yield {"seq": last_sequence, "ts": datetime.now(timezone.utc).isoformat(), "type": "heartbeat", "payload": {}}
            await asyncio.sleep(1)

    @staticmethod
    def _view(record: EventRecord) -> dict[str, Any]:
        return {
            "seq": record.seq,
            "ts": record.created_at.isoformat() if record.created_at else datetime.now(timezone.utc).isoformat(),
            "type": record.type,
            "payload": json.loads(record.payload_json),
        }


class EventedWorkerEndpoint:
    """Protocol-preserving wrapper around a UC WorkerEndpoint."""

    def __init__(self, endpoint, bridge: EventBridge, tenant_id: str, orchestration_id: str) -> None:
        self.descriptor = endpoint.descriptor
        self._endpoint = endpoint
        self._bridge = bridge
        self._tenant_id = tenant_id
        self._orchestration_id = orchestration_id

    async def execute(self, task: WorkerTask, session_id: str) -> WorkerResult:
        await self._bridge.emit(self._tenant_id, self._orchestration_id, "task_dispatch", {
            "task_id": task.task_id,
            "worker_id": task.worker_id,
            "idempotency_key": task.idempotency_key,
            "resume": task.resume is not None,
        })
        result = await self._endpoint.execute(task, session_id)
        await self._bridge.emit(self._tenant_id, self._orchestration_id, "task_receipt", {
            "task_id": result.task_id,
            "worker_id": result.worker_id,
            "status": result.status,
            "output": dict(result.output),
            "error": result.error,
        })
        return result