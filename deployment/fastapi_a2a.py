"""将 A2AWorkerService 暴露为具有认证、授权和 tenant 隔离的 FastAPI 应用。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from common.a2a import A2AWorkerService
from common.contracts import RequestContext
from common.security import (
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    Authorizer,
    tenant_session_id,
)


def create_a2a_app(
    services: Mapping[str, A2AWorkerService],
    *,
    authenticator: Authenticator,
    authorizer: Authorizer,
) -> FastAPI:
    """注册受保护的 A2A Worker 路由。

    ``services`` 的 key 是公开 HTTP path。认证信息只保留在 RequestContext；
    传入 Harness 的 session_id 则按 tenant 重写，确保共享存储中的数据隔离。
    """
    app = FastAPI(title="Universal Contract A2A Worker")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    for path, service in services.items():
        def endpoint_for(bound_service: A2AWorkerService) -> Any:
            async def execute(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
                try:
                    principal = await authenticator.authenticate(request.headers)
                    allowed = await authorizer.authorize(principal, bound_service.worker_id)
                    if not allowed:
                        raise AuthorizationError("调用方没有执行该 Worker 的权限")
                    session_id = tenant_session_id(principal, str(payload["session_id"]))
                    scoped_payload = {**payload, "session_id": session_id}
                    context = RequestContext(
                        principal=principal,
                        request_id=str(payload.get("task_id", "")),
                        trace_id=request.headers.get("x-request-id", ""),
                    )
                    return dict(await bound_service.handle(scoped_payload, context))
                except AuthenticationError as error:
                    raise HTTPException(status_code=401, detail=str(error)) from error
                except AuthorizationError as error:
                    raise HTTPException(status_code=403, detail=str(error)) from error
                except (KeyError, TypeError, ValueError) as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error

            return execute

        app.post(path)(endpoint_for(service))
    return app
