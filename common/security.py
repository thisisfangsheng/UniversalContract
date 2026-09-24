"""A2A 服务的中立认证与授权扩展点。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .contracts import AuthMode, PrincipalContext


class AuthenticationError(Exception):
    """调用方尚未被验证，HTTP 层应映射为 401。"""


class AuthorizationError(Exception):
    """调用方已认证但无权访问资源，HTTP 层应映射为 403。"""


class TokenService(Protocol):
    """token 的签发与验证抽象；JWT、opaque token 与 cookie 均可实现。"""

    async def issue(self, principal: PrincipalContext, *, ttl_s: int) -> str: ...

    async def verify(self, token: str) -> PrincipalContext: ...


class TokenRevocation(Protocol):
    """token 吊销抽象；被吊销的 token 验证必须失败。"""

    async def revoke(self, token: str) -> None: ...

    async def is_revoked(self, token: str) -> bool: ...


class Authenticator(Protocol):
    """将部署层凭据归一为中立 PrincipalContext。"""

    async def authenticate(self, headers: Mapping[str, str]) -> PrincipalContext: ...


class Authorizer(Protocol):
    """根据已验证身份决定其能否执行指定 Worker。"""

    async def authorize(self, principal: PrincipalContext, worker_id: str) -> bool: ...


class NoAuthAuthenticator:
    """只供本地开发使用的显式无认证模式。"""

    def __init__(self, tenant_id: str = "local", subject: str = "local-user") -> None:
        self._principal = PrincipalContext(subject, tenant_id, auth_mode=AuthMode.NONE)

    async def authenticate(self, headers: Mapping[str, str]) -> PrincipalContext:
        return self._principal


class StaticBearerAuthenticator:
    """测试或受控开发环境使用的 bearer token 认证器。"""

    def __init__(self, tokens: Mapping[str, PrincipalContext]) -> None:
        self._tokens = dict(tokens)

    async def authenticate(self, headers: Mapping[str, str]) -> PrincipalContext:
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("缺少 Bearer token")
        principal = self._tokens.get(token)
        if principal is None:
            raise AuthenticationError("Bearer token 无效")
        return principal


class TrustedGatewayAuthenticator:
    """仅接受携带共享网关凭据的、已由网关验证的身份 header。"""

    def __init__(self, gateway_secret: str) -> None:
        if not gateway_secret:
            raise ValueError("gateway_secret 不能为空")
        self._gateway_secret = gateway_secret

    async def authenticate(self, headers: Mapping[str, str]) -> PrincipalContext:
        if headers.get("x-a2a-gateway-secret") != self._gateway_secret:
            raise AuthenticationError("请求不来自可信 API Gateway")
        subject = headers.get("x-a2a-subject", "")
        tenant_id = headers.get("x-a2a-tenant", "")
        if not subject or not tenant_id:
            raise AuthenticationError("可信网关未提供 subject 或 tenant")
        roles = frozenset(filter(None, headers.get("x-a2a-roles", "").split()))
        scopes = frozenset(filter(None, headers.get("x-a2a-scopes", "").split()))
        return PrincipalContext(subject, tenant_id, roles, scopes, AuthMode.GATEWAY)


class ScopeAuthorizer:
    """要求全局或指定 Worker execute scope 的最小授权策略。"""

    def __init__(self, required_scope: str = "a2a:execute") -> None:
        self._required_scope = required_scope

    async def authorize(self, principal: PrincipalContext, worker_id: str) -> bool:
        return (
            self._required_scope in principal.scopes
            or f"worker:{worker_id}:execute" in principal.scopes
        )


class AllowAllAuthorizer:
    """仅供显式本地无认证 profile 使用的授权器。"""

    async def authorize(self, principal: PrincipalContext, worker_id: str) -> bool:
        return True


def tenant_session_id(principal: PrincipalContext, session_id: str) -> str:
    """构造只供存储使用的 tenant 作用域会话键。"""
    if not principal.tenant_id:
        raise AuthorizationError("已认证身份缺少 tenant_id")
    if not session_id:
        raise ValueError("session_id 不能为空")
    return f"{principal.tenant_id}:{session_id}"
