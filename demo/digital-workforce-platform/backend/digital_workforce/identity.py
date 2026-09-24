"""平台侧身份目录与密码凭据校验参考实现。"""

from __future__ import annotations

from collections.abc import Sequence

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.identity import Membership, UserAccount

from .models import MembershipRecord, UserRecord


class SqlAlchemyUserDirectory:
    """以 SQLAlchemy 持久化用户和成员关系，并用 Argon2 校验密码。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._password_hasher = PasswordHasher()

    async def get_user(self, user_id: str) -> UserAccount | None:
        async with self._session_factory() as session:
            record = await session.get(UserRecord, user_id)
            return self._user_account(record) if record is not None else None

    async def memberships_of(self, user_id: str) -> Sequence[Membership]:
        async with self._session_factory() as session:
            records = (await session.scalars(
                select(MembershipRecord).where(MembershipRecord.user_id == user_id)
            )).all()
            return tuple(self._membership(record) for record in records)

    async def resolve_membership(self, user_id: str, tenant_id: str) -> Membership | None:
        async with self._session_factory() as session:
            record = await session.scalar(select(MembershipRecord).where(
                MembershipRecord.user_id == user_id,
                MembershipRecord.tenant_id == tenant_id,
            ))
            return self._membership(record) if record is not None else None

    async def verify(self, username: str, credential: str) -> UserAccount | None:
        async with self._session_factory() as session:
            record = await session.scalar(select(UserRecord).where(UserRecord.username == username))
            if record is None or record.status != "active":
                return None
            try:
                valid = self._password_hasher.verify(record.password_hash, credential)
            except (InvalidHashError, VerifyMismatchError):
                return None
            return self._user_account(record) if valid else None

    def hash_password(self, password: str) -> str:
        """注册流程使用的 Argon2 哈希入口；调用方不得持久化明文密码。"""
        return self._password_hasher.hash(password)

    @staticmethod
    def _user_account(record: UserRecord) -> UserAccount:
        return UserAccount(record.user_id, record.username, record.display_name, record.status)

    @staticmethod
    def _membership(record: MembershipRecord) -> Membership:
        return Membership(record.user_id, record.tenant_id, record.role)