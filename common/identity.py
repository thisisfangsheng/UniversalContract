"""用户、租户及凭据校验的中立身份契约。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Tenant:
    """租户：资源与数据隔离的基本单位。"""

    tenant_id: str
    display_name: str
    status: str


@dataclass(frozen=True)
class UserAccount:
    """用户账户；密码或其他原始凭据不属于该契约。"""

    user_id: str
    username: str
    display_name: str
    status: str


@dataclass(frozen=True)
class Membership:
    """用户与租户的归属关系；role 的可选值由实现侧决定。"""

    user_id: str
    tenant_id: str
    role: str


class UserDirectory(Protocol):
    """身份事实的唯一来源，用于校验用户与租户的归属关系。"""

    async def get_user(self, user_id: str) -> UserAccount | None: ...

    async def memberships_of(self, user_id: str) -> Sequence[Membership]: ...

    async def resolve_membership(self, user_id: str, tenant_id: str) -> Membership | None: ...


class CredentialVerifier(Protocol):
    """校验凭据材料并返回用户；哈希或第三方认证方式由实现决定。"""

    async def verify(self, username: str, credential: str) -> UserAccount | None: ...