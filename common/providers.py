"""可复用的中立 Provider 实现，适合 demo、测试和本地开发。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from common.contracts import AgentRequest, ContextItem, SharedSession
from common.identity import Membership, UserAccount


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


class InMemoryUserDirectory:
    """供离线测试与本地开发使用的用户、成员关系目录。"""

    def __init__(self, users: Sequence[UserAccount] = (), memberships: Sequence[Membership] = ()) -> None:
        self._users = {user.user_id: user for user in users}
        self._memberships = tuple(memberships)

    async def get_user(self, user_id: str) -> UserAccount | None:
        return self._users.get(user_id)

    async def memberships_of(self, user_id: str) -> Sequence[Membership]:
        return tuple(item for item in self._memberships if item.user_id == user_id)

    async def resolve_membership(self, user_id: str, tenant_id: str) -> Membership | None:
        return next(
            (item for item in self._memberships if item.user_id == user_id and item.tenant_id == tenant_id),
            None,
        )


class StaticCredentialVerifier:
    """供离线测试使用的固定凭据校验器，生产环境应替换为安全实现。"""

    def __init__(self, credentials: Mapping[tuple[str, str], UserAccount]) -> None:
        self._credentials = dict(credentials)

    async def verify(self, username: str, credential: str) -> UserAccount | None:
        return self._credentials.get((username, credential))