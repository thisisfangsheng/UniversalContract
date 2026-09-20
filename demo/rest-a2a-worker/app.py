"""一个可由 Uvicorn 承载的受保护 A2A Worker REST 应用。"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.a2a import A2AWorkerService
from common.contracts import (
    AgentIdentity, AgentResult, Capability, HarnessExecutionContext, HarnessProviders, HarnessSpec,
    PrincipalContext,
)
from common.providers import InMemorySessionProvider
from common.security import ScopeAuthorizer, StaticBearerAuthenticator
from deployment.fastapi_a2a import create_a2a_app
from harness import ProviderHarness


class EchoAgent:
    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        tenant_id = context.request.request_context.principal.tenant_id
        return AgentResult("completed", f"tenant={tenant_id}; input={context.request.input}", "rest-demo-run")

    def run_stream(self, context: HarnessExecutionContext):
        raise NotImplementedError


class EchoRuntime:
    runtime_name = "rest-demo-echo"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DURABLE_SESSION})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> EchoAgent:
        return EchoAgent()

    async def close(self) -> None:
        return None


sessions = InMemorySessionProvider()
harness = ProviderHarness(
    HarnessSpec(AgentIdentity("echo-worker", "REST Echo Worker"), "回显经过认证的请求。"),
    HarnessProviders(session=sessions),
    EchoRuntime(),
)
app = create_a2a_app(
    {"/workers/echo/tasks": A2AWorkerService(harness, "echo-worker")},
    authenticator=StaticBearerAuthenticator({
        "demo-token": PrincipalContext("demo-user", "demo-tenant", scopes=frozenset({"a2a:execute"})),
    }),
    authorizer=ScopeAuthorizer(),
)