from __future__ import annotations

import json
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import SharedSession

from ..models import SessionRecord


class SQLiteSessionProvider:
    """Tenant-scoped UC SessionProvider persisted as neutral SharedSession JSON."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: str) -> None:
        self._sessions = sessions
        self._tenant_id = tenant_id
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, session_id: str) -> SharedSession:
        async with self._sessions() as session:
            record = await session.scalar(select(SessionRecord).where(
                SessionRecord.tenant_id == self._tenant_id,
                SessionRecord.session_id == session_id,
            ))
            if record is None:
                return SharedSession(session_id)
            payload = json.loads(record.payload_json)
            return SharedSession(
                session_id,
                facts=dict(payload.get("facts", {})),
                messages=list(payload.get("messages", [])),
                runtime_references=dict(payload.get("runtime_references", {})),
            )

    async def save(self, shared_session: SharedSession) -> None:
        payload = json.dumps({
            "facts": shared_session.facts,
            "messages": shared_session.messages,
            "runtime_references": shared_session.runtime_references,
        }, ensure_ascii=False)
        lock = self._locks.setdefault(shared_session.session_id, asyncio.Lock())
        async with lock:
            async with self._sessions() as session:
                record = await session.scalar(select(SessionRecord).where(
                    SessionRecord.tenant_id == self._tenant_id,
                    SessionRecord.session_id == shared_session.session_id,
                ))
                if record is None:
                    session.add(SessionRecord(tenant_id=self._tenant_id, session_id=shared_session.session_id, payload_json=payload))
                else:
                    record.payload_json = payload
                await session.commit()