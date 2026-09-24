"""平台 JWT 认证参考实现；HTTP 路由只依赖其中的中立 PrincipalContext。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from jwt.exceptions import InvalidTokenError

from common.contracts import AuthMode, PrincipalContext
from common.security import AuthenticationError


class InMemoryTokenRevocation:
    """本地开发和单实例部署使用的 token 吊销表。"""

    def __init__(self) -> None:
        self._tokens: set[str] = set()

    async def revoke(self, token: str) -> None:
        self._tokens.add(token)

    async def is_revoked(self, token: str) -> bool:
        return token in self._tokens


class JwtTokenService:
    """HS256 JWT 的 TokenService 参考实现。"""

    def __init__(self, secret: str, revocation: InMemoryTokenRevocation) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("JWT_SECRET 至少需要 32 字节")
        self._secret = secret
        self._revocation = revocation

    async def issue(self, principal: PrincipalContext, *, ttl_s: int) -> str:
        return self._encode(principal, ttl_s, "access")

    async def issue_refresh(self, principal: PrincipalContext, *, ttl_s: int) -> str:
        return self._encode(principal, ttl_s, "refresh")

    async def verify(self, token: str) -> PrincipalContext:
        if await self._revocation.is_revoked(token):
            raise AuthenticationError("token 已吊销")
        try:
            claims = jwt.decode(token, self._secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
        except InvalidTokenError as error:
            raise AuthenticationError("token 无效或已过期") from error
        return PrincipalContext(
            subject=str(claims["sub"]),
            tenant_id=str(claims.get("tenant_id", "")),
            roles=frozenset(str(item) for item in claims.get("roles", ())),
            scopes=frozenset(str(item) for item in claims.get("scopes", ())),
            auth_mode=AuthMode.BEARER,
        )

    async def verify_refresh(self, token: str) -> PrincipalContext:
        principal = await self.verify(token)
        try:
            claims = jwt.decode(token, self._secret, algorithms=["HS256"])
        except InvalidTokenError as error:
            raise AuthenticationError("refresh token 无效或已过期") from error
        if claims.get("token_type") != "refresh":
            raise AuthenticationError("token 不是 refresh token")
        return principal

    def _encode(self, principal: PrincipalContext, ttl_s: int, token_type: str) -> str:
        now = datetime.now(UTC)
        return jwt.encode({
            "sub": principal.subject,
            "tenant_id": principal.tenant_id,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
            "token_type": token_type,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_s),
        }, self._secret, algorithm="HS256")