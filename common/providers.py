"""可复用的中立 Provider 实现，适合 demo、测试和本地开发。"""

from __future__ import annotations

from common.contracts import AgentRequest, ContextItem, SharedSession


class InMemorySessionProvider:
    """仅保存中立 ``SharedSession`` 的进程内会话存储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SharedSession] = {}

    async def load(self, session_id: str) -> SharedSession:
        return self._sessions.setdefault(session_id, SharedSession(session_id))

    async def save(self, session: SharedSession) -> None:
        self._sessions[session.session_id] = session


class StaticContextProvider:
    """为每次调用提供一条固定的业务策略上下文。"""

    def __init__(self, label: str, content: str, priority: int = 10) -> None:
        self._item = ContextItem(label, content, priority)

    async def provide(self, request: AgentRequest, session: SharedSession) -> list[ContextItem]:
        return [self._item]