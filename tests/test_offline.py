"""v2 离线验证：无需模型 API Key，覆盖手册五类验收点中可在离线环境执行的部分。

运行：python tests/test_offline.py
（MAF 集成用例在 agent-framework 未安装时自动跳过。）
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.a2a import A2AWorkerEndpoint, A2AWorkerService, HttpA2ATransport, InMemoryA2ATransport  # noqa: E402
from common.contracts import (  # noqa: E402
    AgentIdentity, AgentRequest, AgentResult, AuthMode, Capability, ContextItem, HarnessExecutionContext, HarnessProviders, HarnessSpec,
    LlmServingSpec, McpServerSpec, McpTransport, OrchestrationMode, OrchestrationPlan, OrchestrationRequest,
    PrincipalContext, Reflection, RoundRecord, SharedSession, SkillSpec, WorkerDescriptor, WorkerResult, WorkerTask,
)
from common.security import ScopeAuthorizer, StaticBearerAuthenticator, TrustedGatewayAuthenticator  # noqa: E402
from common.skills import (  # noqa: E402
    FilesystemSkillProvider, InlineSkillProvider, SKILL_BODY_CONTEXT_PREFIX,
    SKILL_CATALOG_CONTEXT_LABEL, SkillRegistry, parse_skill_markdown,
)
from deployment.fastapi_a2a import create_a2a_app  # noqa: E402
from harness import ProviderHarness  # noqa: E402

PASS = 0


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS {name}")


class InMemorySessions:
    def __init__(self) -> None:
        self._items: dict[str, SharedSession] = {}

    async def load(self, session_id: str) -> SharedSession:
        return self._items.setdefault(session_id, SharedSession(session_id))

    async def save(self, session: SharedSession) -> None:
        self._items[session.session_id] = session


class TenantContext:
    async def provide(self, request: AgentRequest, session: SharedSession) -> list[ContextItem]:
        return [ContextItem("tenant-policy", "仅返回可审计结论。", 10)]


class RefundLeader:
    async def create_plan(self, request, workers) -> OrchestrationPlan:
        return OrchestrationPlan(
            f"plan-{request.workflow_id}",
            OrchestrationMode.CONCURRENT,
            (
                WorkerTask("policy-check", "policy-worker", "核查退款政策", request.facts, "refund-policy-001"),
                WorkerTask("risk-check", "risk-worker", "核查支付风险", request.facts, "refund-risk-001"),
            ),
        )


class PolicyFirstLeader:
    async def create_plan(self, request, workers) -> OrchestrationPlan:
        return OrchestrationPlan(
            f"plan-r1-{request.workflow_id}",
            OrchestrationMode.REACT,
            (WorkerTask("policy-check-r1", "policy-worker", "核查退款政策", request.facts,
                        f"{request.workflow_id}:policy:r1"),),
        )


class PolicyEscalationReflector:
    async def reflect(self, request, history, workers) -> Reflection:
        results = [result for record in history for result in record.results]
        policy = next((result for result in results if result.task_id.startswith("policy-check")), None)
        risk = next((result for result in results if result.task_id.startswith("risk-check")), None)
        if risk is not None and "通过" in str(risk.output.get("text", "")):
            return Reflection("done", "风险复核通过，退款结论齐备")
        if policy is not None and "需高级风控复核" in str(policy.output.get("text", "")) and "escalated" not in request.facts:
            return Reflection(
                "replan",
                "政策结论要求高级风控复核，追加 risk-worker",
                (WorkerTask("risk-check-r2", "risk-worker", "高级风控复核", dict(request.facts),
                            f"{request.workflow_id}:risk:r2"),),
                {"escalated": "risk-review"},
            )
        return Reflection("failed", "政策结论未给出可执行的升级路径")


def build_worker_stack(sessions, worker_id, runtime_name, response, capability):
    harness = ProviderHarness(
        HarnessSpec(AgentIdentity(worker_id, worker_id), "按能力执行任务。"),
        HarnessProviders(session=sessions, contexts=[TenantContext()]),
        _StubRuntime(response),
    )
    service = A2AWorkerService(harness, worker_id)
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor(worker_id, f"memory://{worker_id}", frozenset({capability})),
        InMemoryA2ATransport({f"memory://{worker_id}": service}),
    )
    return harness, endpoint


# ---------------------------------------------------------------------------
# 契约层不变式
# ---------------------------------------------------------------------------


def test_contract_invariants() -> None:
    print("[contracts]")
    # 幂等键必填：缺少即构造失败
    try:
        WorkerTask("t", "w", "i", {},)  # type: ignore[call-arg]
        raise AssertionError("idempotency_key 应为必填")
    except TypeError:
        ok("WorkerTask.idempotency_key 必填")

    plan = OrchestrationPlan(plan_id="p", mode=OrchestrationMode.REACT, tasks=())
    assert plan.round == 1 and plan.parent_plan_id is None
    ok("OrchestrationPlan 多轮字段默认值兼容 v1")

    assert dataclasses.is_dataclass(Reflection) and dataclasses.is_dataclass(RoundRecord)
    r = Reflection("done", "reason")
    assert r.new_tasks == () and r.next_facts == {}
    ok("Reflection 默认增量字段为空")

    modes = {m.name for m in OrchestrationMode}
    assert "REACT" in modes and "MAGENTIC" not in modes
    ok("OrchestrationMode 新增 REACT；实现引擎（Magentic）不进入契约")


def test_http_a2a_transport() -> None:
    print("[http a2a]")

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://worker.local/tasks"
        assert json.loads(request.content) == {"task_id": "task-1"}
        assert request.headers["authorization"] == "Bearer worker-token"
        return httpx.Response(200, json={"task_id": "task-1", "status": "completed"})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            result = await HttpA2ATransport(client, headers_provider=lambda: {
                "Authorization": "Bearer worker-token",
            }).send_task(
                "http://worker.local/tasks", {"task_id": "task-1"}
            )
            assert result == {"task_id": "task-1", "status": "completed"}
        finally:
            await client.aclose()

    asyncio.run(scenario())
    ok("HttpA2ATransport 发送 JSON POST、认证 header 并归一化 JSON 响应")


# ---------------------------------------------------------------------------
# A2A 往返（v1 验收点回归）
# ---------------------------------------------------------------------------


class _StubHarness:
    """记录收到的 AgentRequest，返回固定 AgentResult。"""

    def __init__(self) -> None:
        self.last_request: AgentRequest | None = None

    async def validate(self):
        return []

    async def run(self, request: AgentRequest) -> AgentResult:
        self.last_request = request
        return AgentResult("completed", "stub-output", "run-1")

    async def close(self) -> None:
        return None


def test_protected_fastapi_a2a() -> None:
    print("[protected fastapi a2a]")
    harness = _StubHarness()
    worker_id = "secure-worker"
    app = create_a2a_app(
        {"/workers/secure/tasks": A2AWorkerService(harness, worker_id)},
        authenticator=StaticBearerAuthenticator({
            "tenant-a-token": PrincipalContext(
                "alice", "tenant-a", scopes=frozenset({"worker:secure-worker:execute"}), auth_mode=AuthMode.OIDC,
            ),
            "tenant-b-token": PrincipalContext(
                "bob", "tenant-b", scopes=frozenset({"a2a:execute"}), auth_mode=AuthMode.OIDC,
            ),
            "denied-token": PrincipalContext("eve", "tenant-a", auth_mode=AuthMode.OIDC),
        }),
        authorizer=ScopeAuthorizer(),
    )
    payload = {
        "task_id": "task-1", "session_id": "same-session", "instruction": "核查",
        "input": {}, "idempotency_key": "idempotency-1",
    }

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/workers/secure/tasks", json=payload)).status_code == 401
            assert (await client.post(
                "/workers/secure/tasks", json=payload, headers={"Authorization": "Bearer denied-token"},
            )).status_code == 403
            allowed = await client.post(
                "/workers/secure/tasks", json=payload, headers={"Authorization": "Bearer tenant-a-token"},
            )
            assert allowed.status_code == 200
            assert harness.last_request is not None
            assert harness.last_request.session_id == "tenant-a:same-session"
            assert harness.last_request.request_context is not None
            assert harness.last_request.request_context.principal.subject == "alice"
            assert "authorization" not in harness.last_request.metadata
            allowed = await client.post(
                "/workers/secure/tasks", json=payload, headers={"Authorization": "Bearer tenant-b-token"},
            )
            assert allowed.status_code == 200
            assert harness.last_request is not None
            assert harness.last_request.session_id == "tenant-b:same-session"
            gateway_app = create_a2a_app(
                {"/workers/secure/tasks": A2AWorkerService(harness, worker_id)},
                authenticator=TrustedGatewayAuthenticator("gateway-secret"),
                authorizer=ScopeAuthorizer(),
            )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://test") as client:
            rejected = await client.post("/workers/secure/tasks", json=payload, headers={
                "X-A2A-Subject": "mallory", "X-A2A-Tenant": "tenant-a", "X-A2A-Scopes": "a2a:execute",
            })
            assert rejected.status_code == 401
            accepted = await client.post("/workers/secure/tasks", json=payload, headers={
                "X-A2A-Gateway-Secret": "gateway-secret", "X-A2A-Subject": "gateway-user",
                "X-A2A-Tenant": "tenant-gateway", "X-A2A-Scopes": "a2a:execute",
            })
            assert accepted.status_code == 200
            assert harness.last_request is not None
            assert harness.last_request.request_context is not None
            assert harness.last_request.request_context.principal.auth_mode == AuthMode.GATEWAY

    asyncio.run(scenario())
    ok("FastAPI A2A 返回 401/403，隔离 tenant session，并只接受可信网关身份")


def test_a2a_roundtrip() -> None:
    print("[a2a]")
    harness = _StubHarness()
    service = A2AWorkerService(harness, "policy-worker")  # type: ignore[arg-type]
    transport = InMemoryA2ATransport({"memory://policy": service})
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor("policy-worker", "memory://policy", frozenset({"refund-policy"})), transport
    )
    task = WorkerTask("t-1", "policy-worker", "check", {"order": "ORD-1"}, "idem-1")
    result = asyncio.run(endpoint.execute(task, "sess-1"))

    assert harness.last_request is not None
    req = harness.last_request
    assert req.request_id == "t-1" and req.session_id == "sess-1" and req.input == "check"
    assert req.metadata["idempotency_key"] == "idem-1" and req.metadata["a2a"] is True
    assert req.metadata["collaboration"] == {"order": "ORD-1"}
    ok("payload -> AgentRequest 保留 task_id/session_id/幂等键及任务输入")

    assert result.task_id == "t-1" and result.status == "completed" and result.remote_run_id == "run-1"
    assert result.output == {"text": "stub-output"}
    ok("AgentResult -> WorkerResult 归一化")

    try:
        bad = WorkerTask("t-2", "risk-worker", "x", {}, "k")
        asyncio.run(endpoint.execute(bad, "sess-1"))
        raise AssertionError("归属校验应失败")
    except ValueError:
        ok("task.worker_id 与 endpoint 归属校验")

    try:
        asyncio.run(transport.send_task("memory://unknown", {}))
        raise AssertionError("未知 endpoint 应失败")
    except ValueError:
        ok("未注册 endpoint 规范化为 ValueError")


# ---------------------------------------------------------------------------
# Harness 标准上下文：所有 runtime 的协作与长期记忆输入
# ---------------------------------------------------------------------------


def test_harness_standard_context() -> None:
    print("[harness standard context]")

    class MemorySessions:
        def __init__(self) -> None:
            self.session = SharedSession(
                "session-1",
                facts={"tenant": "cn"},
                messages=["assistant: earlier decision"],
            )

        async def load(self, session_id: str) -> SharedSession:
            assert session_id == self.session.session_id
            return self.session

        async def save(self, session: SharedSession) -> None:
            self.session = session

    class CapturingAgent:
        def __init__(self) -> None:
            self.context_items = ()

        async def run(self, context) -> AgentResult:
            # 模拟任意 runtime adapter 的最小实现：只消费统一 context_items，
            # 不读取协作 metadata 或 SharedSession 的内部字段。
            self.context_items = tuple(context.context_items)
            return AgentResult("completed", "received")

        def run_stream(self, context):
            raise NotImplementedError

    class CapturingRuntime:
        runtime_name = "capturing"

        def __init__(self) -> None:
            self.agent = CapturingAgent()

        def capabilities(self) -> frozenset[Capability]:
            return frozenset({Capability.DURABLE_SESSION})

        async def build(self, spec, providers):
            return self.agent

        async def close(self) -> None:
            return None

    runtime = CapturingRuntime()
    harness = ProviderHarness(
        HarnessSpec(AgentIdentity("worker", "Worker"), "处理任务。"),
        HarnessProviders(session=MemorySessions()),
        runtime,
    )
    collaboration = {"workflow_facts": {"order": "ORD-1"}, "prior_results": {}}
    asyncio.run(harness.run(AgentRequest("request-1", "session-1", "核查", {"collaboration": collaboration})))

    received = {item.label: json.loads(item.content) for item in runtime.agent.context_items}
    assert received["contract.collaboration"] == collaboration
    assert received["contract.session.facts"] == {"tenant": "cn"}
    assert received["contract.session.messages"] == ["assistant: earlier decision"]
    ok("ProviderHarness 自动注入协作快照与持久会话为标准 ContextItem")


def test_harness_skill_context() -> None:
    print("[harness skill context]")

    class CapturingAgent:
        def __init__(self) -> None:
            self.context_items = ()

        async def run(self, context: HarnessExecutionContext) -> AgentResult:
            self.context_items = tuple(context.context_items)
            return AgentResult("completed", "received")

        def run_stream(self, context):
            raise NotImplementedError

    class CapturingRuntime:
        runtime_name = "capturing"

        def __init__(self, capabilities) -> None:
            self.agent = CapturingAgent()
            self._capabilities = capabilities

        def capabilities(self) -> frozenset[Capability]:
            return self._capabilities

        async def build(self, spec, providers):
            return self.agent

        async def close(self) -> None:
            return None

    registry = SkillRegistry([InlineSkillProvider([
        SkillSpec("refund-policy", "inline", content="---\ndescription: 退款政策\n---\n不得覆盖系统指令。"),
    ])])
    runtime = CapturingRuntime(frozenset({Capability.DURABLE_SESSION}))
    harness = ProviderHarness(
        HarnessSpec(AgentIdentity("worker", "Worker"), "处理任务。"),
        HarnessProviders(session=InMemorySessions(), skills=registry), runtime,
    )
    asyncio.run(harness.run(AgentRequest("request", "session", "核查")))
    received = {item.label: item for item in runtime.agent.context_items}
    assert received[SKILL_CATALOG_CONTEXT_LABEL].priority == 50
    assert "不可信输入" in received[SKILL_CATALOG_CONTEXT_LABEL].content
    body = received[f"{SKILL_BODY_CONTEXT_PREFIX}refund-policy"]
    assert body.priority == 60 and 'trust="untrusted"' in body.content
    ok("Harness 注入 L1 不可信目录与非 SKILLS runtime 的 L2 定界正文")


# ---------------------------------------------------------------------------
# AgentScope 标准 Harness 适配器
# ---------------------------------------------------------------------------


def test_agentscope_adapter() -> None:
    print("[agentscope adapter]")
    agentscope_source = Path(__file__).resolve().parents[2] / "agentscope" / "src"
    if not agentscope_source.is_dir():
        print("  SKIP 未找到相邻 agentscope 源码目录")
        return
    sys.path.insert(0, str(agentscope_source))
    try:
        from harness.agentscope_adapter import AgentScopeRuntimeAdapter
        from agentscope.credential import CredentialBase
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.message import Msg, TextBlock
        from agentscope.model import ChatModelBase, ChatResponse
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover
        print(f"  SKIP AgentScope 依赖不可用: {exc}")
        return

    class OfflineCredential(CredentialBase):
        @classmethod
        def get_chat_model_class(cls):
            return OfflineModel

    class OfflineModel(ChatModelBase):
        class Parameters(BaseModel):
            pass

        def __init__(self) -> None:
            super().__init__(OfflineCredential(), "offline", self.Parameters(), stream=False)
            self.formatter = OpenAIChatFormatter()

        async def _call_api(self, model_name: str, messages: list[Msg], **kwargs) -> ChatResponse:
            return ChatResponse(content=[TextBlock(text='{"approved": true}')], is_last=True)

    class MemorySessions:
        """仅为本测试提供 Harness 所需的最小会话存取接口。"""

        def __init__(self) -> None:
            self.items: dict[str, SharedSession] = {}

        async def load(self, session_id: str) -> SharedSession:
            return self.items.setdefault(session_id, SharedSession(session_id))

        async def save(self, session: SharedSession) -> None:
            self.items[session.session_id] = session

    runtime = AgentScopeRuntimeAdapter(OfflineModel())
    providers = HarnessProviders(session=MemorySessions())
    harness = ProviderHarness(
        HarnessSpec(
            AgentIdentity("agent", "AgentScope worker"),
            "返回 JSON。",
            frozenset({Capability.DURABLE_SESSION, Capability.STRUCTURED_OUTPUT}),
        ),
        providers,
        runtime,
    )
    assert asyncio.run(harness.validate()) == []
    result = asyncio.run(harness.run(AgentRequest("request-1", "session-1", "执行", {"facts": {}})))
    assert result.status == "completed" and result.data == {"approved": True} and result.run_id
    ok("AgentScope 标准适配器执行 Agent.reply 并归一化 JSON 结构化输出")


def test_openai_compatible_serving_configuration() -> None:
    """Serving 配置必须按 HarnessSpec 隔离，编排 Serving 也不与 Worker 混用。"""
    print("[openai-compatible serving]")
    from harness.openai_compatible import OpenAICompatibleLeader, OpenAICompatibleRuntimeAdapter

    class MemorySessions:
        async def load(self, session_id: str) -> SharedSession:
            return SharedSession(session_id)

        async def save(self, session: SharedSession) -> None:
            return None

    calls: list[tuple[str, str]] = []

    async def complete(serving, messages) -> str:
        calls.append((serving.base_url, serving.model))
        if "planner" in serving.model:
            return '{"tasks": [{"task_id": "plan-1", "worker_id": "worker-a", "instruction": "check", "idempotency_key": "wf:1"}]}'
        return '{"served_by": "worker"}'

    providers = HarnessProviders(session=MemorySessions())
    serving_a = LlmServingSpec("worker-a-model", "https://a.example/v1", "key-a")
    serving_b = LlmServingSpec("worker-b-model", "https://b.example/v1", "key-b")
    for agent_id, serving in (("worker-a", serving_a), ("worker-b", serving_b)):
        harness = ProviderHarness(
            HarnessSpec(AgentIdentity(agent_id, agent_id), "reply", llm_serving=serving), providers,
            OpenAICompatibleRuntimeAdapter(complete=complete),
        )
        result = asyncio.run(harness.run(AgentRequest(f"r-{agent_id}", "s", "go")))
        assert result.data == {"served_by": "worker"}
    assert calls == [("https://a.example/v1", "worker-a-model"), ("https://b.example/v1", "worker-b-model")]
    ok("每个 HarnessSpec 独立选择自己的 OpenAI 兼容 base_url 与 model")

    planner_serving = LlmServingSpec("planner-model", "https://planner.example/v1", "planner-key")
    leader = OpenAICompatibleLeader(planner_serving, complete=complete)
    plan = asyncio.run(leader.create_plan(
        OrchestrationRequest("wf", "s", "plan", {}, OrchestrationMode.CONCURRENT),
        [WorkerDescriptor("worker-a", "memory://a", frozenset())],
    ))
    assert plan.tasks[0].worker_id == "worker-a"
    assert calls[-1] == ("https://planner.example/v1", "planner-model")
    ok("编排 Leader 使用独立的 OpenAI 兼容 Serving")

    try:
        from orchestration import create_workflow
    except ImportError:
        print("  SKIP create_workflow 需要 agent-framework")
    else:
        workflow = create_workflow(
            OrchestrationRequest("wf-auto", "s", "plan", {}, OrchestrationMode.CONCURRENT), [],
            llm_serving=planner_serving,
        )
        assert workflow is not None
        ok("create_workflow 接受 llm_serving 并自动装配编排 Leader")


# ---------------------------------------------------------------------------
# MagenticWorkerAdapter 翻译与护栏
# ---------------------------------------------------------------------------


class _EchoEndpoint:
    def __init__(self, descriptor: WorkerDescriptor) -> None:
        self.descriptor = descriptor
        self.seen_tasks: list[WorkerTask] = []

    async def execute(self, task: WorkerTask, session_id: str) -> WorkerResult:
        self.seen_tasks.append(task)
        return WorkerResult(task.task_id, task.worker_id, "completed", {"text": f"done {task.task_id}"})


def test_magentic_adapter() -> None:
    print("[magentic adapter]")
    from orchestration.magentic import MagenticWorkerAdapter

    endpoint = _EchoEndpoint(WorkerDescriptor("policy-worker", "memory://policy", frozenset({"refund-policy"}), "政策核查"))
    shared_state = {"workflow_facts": {"order": "ORD-1"}, "results": []}
    adapter = MagenticWorkerAdapter(  # type: ignore[arg-type]
        endpoint, session_id="sess-1", run_id="wf-9", shared_state=shared_state
    )

    text1 = asyncio.run(adapter.handle_instruction("核查退款", {"order": "ORD-1"}))
    text2 = asyncio.run(adapter.handle_instruction("再核查", {}))
    assert text1 == "done policy-worker-wf-9-m1" and text2.endswith("m2")
    assert len(endpoint.seen_tasks) == 2
    t1, t2 = endpoint.seen_tasks
    assert t1.task_id != t2.task_id and t1.idempotency_key != t2.idempotency_key
    ok("每轮 chat 生成新 task_id 与新幂等键")

    assert t1.input["workflow_facts"] == {"order": "ORD-1"}
    assert t2.input["prior_results"][t1.task_id]["output"]["text"] == text1
    ok("Magentic participant 接收共享事实与前序 Worker 回执")

    assert adapter.description == "政策核查 [capabilities: refund-policy]"
    ok("participant 元数据携带能力目录")

    adapter.reset()
    assert adapter.worker_results == []
    ok("reset 清空轮次与结果收集")


def test_magentic_write_guard() -> None:
    print("[magentic guard]")
    from orchestration.magentic import build_magentic_workflow

    evil = _EchoEndpoint(WorkerDescriptor("pay-worker", "memory://pay", frozenset({"refund-submit"})))
    normal = _EchoEndpoint(WorkerDescriptor("policy-worker", "memory://policy", frozenset({"refund-policy"})))
    try:
        build_magentic_workflow(
            [evil, normal],  # type: ignore[arg-type]
            session_id="s", run_id="r",
            manager=object(),
        )
        raise AssertionError("高危能力应被拒绝注册")
    except ValueError:
        ok("forbidden_capabilities 写操作护栏")


def test_reflector_rules() -> None:
    print("[react reflector]")

    reflector = PolicyEscalationReflector()
    request = OrchestrationRequest("wf", "s", "退款", {"amount": 199}, OrchestrationMode.REACT)
    workers = [WorkerDescriptor("policy-worker", "u", frozenset({"refund-policy"})),
               WorkerDescriptor("risk-worker", "u", frozenset({"payment-risk"}))]
    policy_plan = OrchestrationPlan("p1", OrchestrationMode.REACT, (), round=1)
    escalate_result = WorkerResult("policy-check-r1", "policy-worker", "completed",
                                   {"text": "...需高级风控复核"})

    r1 = asyncio.run(reflector.reflect(request, (RoundRecord(1, policy_plan, (escalate_result,)),), workers))
    assert r1.decision == "replan" and r1.new_tasks[0].worker_id == "risk-worker"
    assert r1.new_tasks[0].idempotency_key.endswith(":risk:r2") and r1.next_facts == {"escalated": "risk-review"}
    ok("观察 -> replan 增量任务 + 事实更新")

    merged = {**request.facts, **dict(r1.next_facts)}
    risk_pass = WorkerResult("risk-check-r2", "risk-worker", "completed", {"text": "高级风控复核通过"})
    r2 = asyncio.run(reflector.reflect(
        dataclasses.replace(request, facts=merged),
        (RoundRecord(1, policy_plan, (escalate_result,)), RoundRecord(2, policy_plan, (risk_pass,))),
        workers,
    ))
    assert r2.decision == "done"
    ok("复核通过 -> 收敛 done")

    fail_result = WorkerResult("risk-check-r2", "risk-worker", "failed", {}, error="模型超时")
    r3 = asyncio.run(reflector.reflect(
        dataclasses.replace(request, facts=merged),
        (RoundRecord(1, policy_plan, (escalate_result,)), RoundRecord(2, policy_plan, (fail_result,))),
        workers,
    ))
    assert r3.decision == "failed"
    ok("无法继续 -> 显式 failed（不无限重试）")


# ---------------------------------------------------------------------------
# MAF 集成：ReAct 图真实回边 + harness 回归
# ---------------------------------------------------------------------------


def test_react_workflow_integration() -> None:
    print("[maf integration]")
    try:
        from orchestration import ReactOrchestration, create_maf_workflow, create_react_workflow, create_workflow
    except ImportError as exc:  # pragma: no cover
        print(f"  SKIP 需要安装 agent-framework: {exc}")
        return

    sessions = InMemorySessions()
    providers = HarnessProviders(session=sessions)
    harness = ProviderHarness(
        HarnessSpec(AgentIdentity("policy-worker", "政策"), "只解释政策。"),
        providers,
        _StubRuntime("escalate"),
    )
    service = A2AWorkerService(harness, "policy-worker")  # type: ignore[arg-type]
    transport = InMemoryA2ATransport({"memory://policy": service})
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor("policy-worker", "memory://policy", frozenset({"refund-policy"})), transport
    )
    harness_risk = ProviderHarness(
        HarnessSpec(AgentIdentity("risk-worker", "风险"), "只评估风险。"),
        providers,
        _StubRuntime("risk-pass"),
    )
    service_risk = A2AWorkerService(harness_risk, "risk-worker")  # type: ignore[arg-type]
    endpoint_risk = A2AWorkerEndpoint(
        WorkerDescriptor("risk-worker", "memory://risk", frozenset({"payment-risk"})),
        InMemoryA2ATransport({"memory://risk": service_risk}),
    )

    async def scenario() -> tuple[int, str]:
        orch = create_react_workflow(
            [endpoint, endpoint_risk], engine="builtin",
            leader=PolicyFirstLeader(), reflector=PolicyEscalationReflector(), max_rounds=4,
        )
        events = await orch.workflow.run(OrchestrationRequest("wf-1", "s-1", "退款", {}, OrchestrationMode.REACT))
        result = await orch.finalize(OrchestrationRequest("wf-1", "s-1", "退款", {}, OrchestrationMode.REACT), events)
        rounds = len(result.rounds)
        status = result.status
        return rounds, status

    rounds, status = asyncio.run(scenario())
    assert rounds == 2 and status == "completed", (rounds, status)
    ok(f"ReAct builtin 回边真实执行：r1 升级 -> r2 复核通过，2 轮 completed")

    # 通用工厂只由请求 mode 选择编排分支，避免调用方手工组合出不一致的 workflow。
    react_request = OrchestrationRequest("wf-factory", "s-1", "退款", {}, OrchestrationMode.REACT)
    factory_react = create_workflow(
        react_request,
        [endpoint, endpoint_risk],
        react_engine="builtin",
        leader=PolicyFirstLeader(),
        reflector=PolicyEscalationReflector(),
    )
    assert isinstance(factory_react, ReactOrchestration) and factory_react.engine == "builtin"
    ok("create_workflow 根据 request.mode=REACT 选择反思引擎")

    try:
        create_workflow(
            OrchestrationRequest("wf-factory-invalid", "s-1", "退款", {}, OrchestrationMode.CONCURRENT),
            [endpoint, endpoint_risk],
            react_engine="builtin",
            leader=RefundLeader(),
        )
        raise AssertionError("非 REACT mode 不应接受 REACT 引擎参数")
    except ValueError:
        ok("create_workflow 拒绝非 REACT 请求携带 REACT 引擎参数")

    class WrongModeLeader:
        """故意违反请求 mode，用于验证通用工厂传递的计划一致性校验。"""

        async def create_plan(self, request, workers):
            return OrchestrationPlan("wrong-mode", OrchestrationMode.SEQUENTIAL, ())

    async def scenario_mode_mismatch() -> None:
        workflow = create_workflow(
            OrchestrationRequest("wf-mode-mismatch", "s-1", "退款", {}, OrchestrationMode.CONCURRENT),
            [endpoint, endpoint_risk],
            leader=WrongModeLeader(),
        )
        await workflow.run(OrchestrationRequest("wf-mode-mismatch", "s-1", "退款", {}, OrchestrationMode.CONCURRENT))

    try:
        asyncio.run(scenario_mode_mismatch())
        raise AssertionError("Leader 计划 mode 与请求 mode 不一致时应失败")
    except ValueError as error:
        assert "mode" in str(error)
        ok("create_workflow 拒绝 Leader 计划与 request.mode 不一致")

    # 所有内置编排 mode 使用同一份协作数据面：每个任务都有 workflow_facts；
    # 有先后关系的任务还会获得已经完成的 WorkerResult 快照。
    class SharingLeader:
        def __init__(self, mode: OrchestrationMode) -> None:
            self._mode = mode

        async def create_plan(self, request, workers):
            first = WorkerTask("share-1", "policy-worker", "first", {"own": "first"}, "share:1")
            second = WorkerTask(
                "share-2", "risk-worker", "second", {"own": "second"}, "share:2",
                depends_on=("share-1",) if self._mode == OrchestrationMode.HANDOFF else (),
            )
            return OrchestrationPlan("share-plan", self._mode, (first, second))

    shared_policy = _EchoEndpoint(
        WorkerDescriptor("policy-worker", "memory://shared-policy", frozenset({"refund-policy"}))
    )
    shared_risk = _EchoEndpoint(
        WorkerDescriptor("risk-worker", "memory://shared-risk", frozenset({"payment-risk"}))
    )

    async def run_sharing_mode(mode: OrchestrationMode) -> None:
        shared_policy.seen_tasks.clear()
        shared_risk.seen_tasks.clear()
        request = OrchestrationRequest("wf-share", "s-share", "退款", {"order": "ORD-1"}, mode)
        workflow = create_workflow(request, [shared_policy, shared_risk], leader=SharingLeader(mode))
        await workflow.run(request)

    for mode in (
        OrchestrationMode.LEADER_WORKER,
        OrchestrationMode.SEQUENTIAL,
        OrchestrationMode.CONCURRENT,
        OrchestrationMode.HANDOFF,
    ):
        asyncio.run(run_sharing_mode(mode))
        first_task = shared_policy.seen_tasks[0]
        second_task = shared_risk.seen_tasks[0]
        assert first_task.input["workflow_facts"] == {"order": "ORD-1"}
        assert second_task.input["workflow_facts"] == {"order": "ORD-1"}
        if mode == OrchestrationMode.CONCURRENT:
            assert second_task.input["prior_results"] == {}
        else:
            assert second_task.input["prior_results"]["share-1"]["output"]["text"] == "done share-1"
    ok("所有单轮 mode 共享工作流事实；有序 mode 传递前序 Worker 回执")

    # max_rounds 确定性护栏：reflector 永远要求 replan -> 强制终止
    class AlwaysReplanReflector:
        async def reflect(self, request, history, workers):
            return Reflection("replan", "不收敛", new_tasks=(
                WorkerTask(f"t-r{len(history) + 1}", "policy-worker", "again", {},
                           f"wf:policy:r{len(history) + 1}"),
            ))

    async def scenario_guard() -> tuple[int, str]:
        harness3 = ProviderHarness(
            HarnessSpec(AgentIdentity("policy-worker", "政策"), "i"), providers, _StubRuntime("plain")
        )
        svc3 = A2AWorkerService(harness3, "policy-worker")  # type: ignore[arg-type]
        ep3 = A2AWorkerEndpoint(
            WorkerDescriptor("policy-worker", "memory://p3", frozenset({"refund-policy"})),
            InMemoryA2ATransport({"memory://p3": svc3}),
        )
        orch = create_react_workflow(
            [ep3], engine="builtin",
            leader=PolicyFirstLeader(), reflector=AlwaysReplanReflector(), max_rounds=3,
        )
        events = await orch.workflow.run(OrchestrationRequest("wf-3", "s-1", "退款", {}, OrchestrationMode.REACT))
        result = await orch.finalize(OrchestrationRequest("wf-3", "s-1", "退款", {}, OrchestrationMode.REACT), events)
        return len(result.rounds), str(result.status)

    rounds_g, status_g = asyncio.run(scenario_guard())
    assert rounds_g == 3 and status_g == "failed", (rounds_g, status_g)
    ok(f"max_rounds 护栏强制终止（rounds={rounds_g}, failed），不信任反思的无限 replan")

    # magentic 引擎（默认引擎）真跑：WorkerEndpoint 适配为 participant，经 A2A 全链路
    from orchestration.magentic import DeterministicMagenticManager

    async def scenario_magentic() -> tuple[int, str, int]:
        sessions = InMemorySessions()
        h1, ep1 = build_worker_stack(sessions, "policy-worker", "agentscope-worker", "政策结论", "refund-policy")
        h2, ep2 = build_worker_stack(sessions, "risk-worker", "langgraph-worker", "风险结论", "payment-risk")
        request = OrchestrationRequest("wf-m", "s-1", "退款", {}, OrchestrationMode.REACT)
        orch = create_react_workflow(
            [ep1, ep2], engine="magentic",
            session_id=request.session_id, run_id=request.workflow_id,
            manager=DeterministicMagenticManager(["policy-worker", "risk-worker"]),
        )
        stream = orch.workflow.run(request.goal, stream=True)
        async for _ev in stream:
            pass
        run_result = await stream.get_final_response()
        result = await orch.finalize(request, run_result)
        return len(result.rounds), str(result.status), len(result.worker_results)

    rounds_m, status_m, workers_m = asyncio.run(scenario_magentic())
    assert (rounds_m, status_m, workers_m) == (2, "completed", 2), (rounds_m, status_m, workers_m)
    ok('REACT/magentic 引擎经 A2A 全链路执行：rounds=2, worker_results=2, completed')

    # 引擎选择护栏
    try:
        create_react_workflow([], engine="magentic", session_id="s", run_id="r")
        raise AssertionError("manager 缺失应报错")
    except ValueError:
        ok('engine="magentic" 强制 manager/manager_agent 二选一')

    # v1 单轮图回归
    harness2 = ProviderHarness(
        HarnessSpec(AgentIdentity("policy-worker", "政策"), "i"),
        providers,
        _StubRuntime("plain"),
    )
    service2 = A2AWorkerService(harness2, "policy-worker")  # type: ignore[arg-type]
    endpoint2 = A2AWorkerEndpoint(
        WorkerDescriptor("policy-worker", "memory://p2", frozenset({"refund-policy"})),
        InMemoryA2ATransport({"memory://p2": service2}),
    )
    async def scenario_v1() -> str:
        wf = create_maf_workflow(RefundLeader(), [endpoint2, endpoint_risk])
        events = await wf.run(OrchestrationRequest("wf-2", "s-1", "退款", {}, OrchestrationMode.CONCURRENT))
        result = list(events.get_outputs())[-1]
        return str(result.status)

    assert asyncio.run(scenario_v1()) == "completed"
    ok("v1 Leader-Worker 图回归通过")


class _StubRuntime:
    """按 request 轮次返回不同文本，驱动多轮场景。"""

    def __init__(self, mode: str) -> None:
        self.runtime_name = "stub"
        self._mode = mode
        self._calls = 0

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DURABLE_SESSION})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders):
        return _StubAgent(spec.identity.name, self)


class _StubAgent:
    def __init__(self, name: str, runtime: "_StubRuntime") -> None:
        self._name = name
        self._runtime = runtime

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        self._runtime._calls += 1
        n = self._runtime._calls
        if self._runtime._mode == "escalate":
            text = "需高级风控复核" if n == 1 else "高级风控复核通过"
        elif self._runtime._mode == "risk-pass":
            text = "高级风控复核通过"
        else:
            text = "plain"
        return AgentResult("completed", f"{self._name}: {text}", f"run-{n}")

    def run_stream(self, context: HarnessExecutionContext):
        raise NotImplementedError


def test_tools_skills_spec_and_validate() -> None:
    print("[tools/skills spec & validate]")
    import dataclasses as _dc

    # 不变式
    server = McpServerSpec(name="s1", transport=McpTransport.STREAMABLE_HTTP, url="http://x")
    assert server.load_tools and not server.load_skills and server.allowed_tools == frozenset()
    skill = SkillSpec(name="sk1", source="inline", content="# x")
    assert skill.source == "inline" and not skill.enable_scripts
    ok("McpServerSpec / SkillSpec 默认值（load_skills 关、脚本默认禁）")

    providers = HarnessProviders(session=InMemorySessions(), contexts=[TenantContext()])
    runtime_ok = _ToolLoopRuntime("rt-ok")
    runtime_noloop = _StubRuntime("plain")  # 无 TOOL_LOOP 能力

    def make_problems(spec: HarnessSpec, runtime) -> list[str]:
        harness = ProviderHarness(spec, providers, runtime)
        return list(asyncio.run(harness.validate()))

    base = HarnessSpec(AgentIdentity("w", "w"), "i",
                       frozenset({Capability.DURABLE_SESSION, Capability.TOOL_LOOP}),
                       tools=[server], skills=[SkillSpec(name="a", source="inline", content="x")])
    assert make_problems(base, runtime_ok) == []
    ok("合法 spec（server+inline skill）validate 通过")

    problems = make_problems(base, runtime_noloop)
    assert any("TOOL_LOOP" in p for p in problems), problems
    ok("声明 tools/skills 但 runtime 无 TOOL_LOOP -> 校验失败")

    bad_ref = _dc.replace(base, skills=[SkillSpec(name="b", source="mcp", server_ref="ghost")])
    assert any("server_ref" in p or "未声明" in p for p in make_problems(bad_ref, runtime_ok))
    ok("skill.server_ref 指向不存在的 server -> 校验失败")

    server_no_skills = _dc.replace(server, load_skills=False)
    bad_load = _dc.replace(base, tools=[server_no_skills],
                           skills=[SkillSpec(name="c", source="mcp", server_ref="s1")])
    assert any("load_skills" in p for p in make_problems(bad_load, runtime_ok))
    ok("server 未开启 load_skills -> 校验失败")

    server_narrow = _dc.replace(server, allowed_tools=frozenset({"query_policy"}), load_skills=True)
    bad_width = _dc.replace(base, tools=[server_narrow],
                            skills=[SkillSpec(name="d", source="mcp", server_ref="s1",
                                              allowed_tools=frozenset({"submit_refund"}))])
    assert any("超出" in p for p in make_problems(bad_width, runtime_ok))
    ok("skill.allowed_tools 超出 server 暴露范围 -> 校验失败")

    scripted = _dc.replace(base, skills=[SkillSpec(name="e", source="inline", content="x", enable_scripts=True)])
    assert any("enable_scripts" in p for p in make_problems(scripted, runtime_ok))
    strict_harness = ProviderHarness(
        scripted, _dc.replace(providers, strict_skills=True), runtime_ok,
    )
    assert any("enable_scripts" in problem for problem in asyncio.run(strict_harness.validate()))
    strict_result = asyncio.run(strict_harness.run(AgentRequest("strict", "session", "run")))
    assert strict_result.status == "failed" and strict_result.error is not None
    ok("enable_scripts=True 在宽松模式提醒、strict_skills 模式阻断")


def test_skill_markdown_parser() -> None:
    print("[skill markdown parser]")
    parsed = parse_skill_markdown(
        "---\nname: refund-policy\ndescription: 查询退款\nallowed-tools: [query, submit]\nenable-scripts: true\n---\n先检查订单。\n",
        source="inline", origin="inline", default_name="fallback",
    )
    assert parsed.name == "refund-policy" and parsed.description == "查询退款"
    assert parsed.body == "先检查订单。\n" and parsed.allowed_tools == frozenset({"query", "submit"})
    assert parsed.enable_scripts and len(parsed.content_digest) == 64
    ok("SKILL.md frontmatter、正文与 digest 解析")

    fallback = parse_skill_markdown("第一行说明\n第二行", source="inline", default_name="fallback")
    assert fallback.name == "fallback" and fallback.description == "第一行说明"
    ok("缺失 frontmatter 时回退 SkillSpec 名称与正文首行")


def test_skill_providers_and_registry() -> None:
    print("[skill providers]")

    async def scenario() -> None:
        inline = InlineSkillProvider([SkillSpec("inline-skill", "inline", content="内联技能")])
        registry = SkillRegistry([inline])
        assert [entry.name for entry in await registry.catalog()] == ["inline-skill"]
        assert (await registry.resolve(SkillSpec("inline-skill", "inline"))).body == "内联技能"

        missing = FilesystemSkillProvider([SkillSpec("missing", "path", path="/not/a/skill")])
        assert await missing.catalog() == []
        assert missing.problems and "SKILL.md" in missing.problems[0]

        duplicate = SkillRegistry([
            InlineSkillProvider([SkillSpec("same", "inline", content="one")]),
            InlineSkillProvider([SkillSpec("same", "inline", content="two")]),
        ])
        try:
            await duplicate.catalog()
            raise AssertionError("同名 skill 应拒绝加载")
        except ValueError as exc:
            assert "inline" in str(exc)

    asyncio.run(scenario())
    ok("Inline / Filesystem Provider 缓存加载，Registry 拒绝同名冲突")


class _ToolLoopRuntime:
    """具备 TOOL_LOOP 的测试 runtime。"""

    def __init__(self, name: str) -> None:
        self.runtime_name = name

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DURABLE_SESSION, Capability.TOOL_LOOP})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders):
        return _StubAgent(spec.identity.name, self)

    async def close(self) -> None:
        return None


def test_framework_fit_fields() -> None:
    """v2.2 适配性调整：结构化输出（data）与 HITL 恢复（resume）穿透 A2A 协议层。"""
    print("[framework fit: data & resume]")

    class _GateHarness:
        """模拟 HITL 门控：resume=None -> needs_approval；resume.approved -> completed+data。"""

        def __init__(self) -> None:
            self.last_request: AgentRequest | None = None

        async def validate(self):
            return []

        async def run(self, request: AgentRequest) -> AgentResult:
            self.last_request = request
            if request.resume is None:
                return AgentResult("needs_approval", "需审批", "run-1",
                                   None, {"approval_request": "refund-199", "decision": "pending"})
            if request.resume.get("approved"):
                return AgentResult("completed", "已提交", "run-2", None,
                                   {"decision": "approved", "refund_id": "RF-1"})
            return AgentResult("cancelled", "拒绝", "run-2", None, {"decision": "rejected"})

        async def close(self) -> None:
            return None

    harness = _GateHarness()
    service = A2AWorkerService(harness, "gate-worker")  # type: ignore[arg-type]
    endpoint = A2AWorkerEndpoint(
        WorkerDescriptor("gate-worker", "memory://gate", frozenset({"refund-submit"})),
        InMemoryA2ATransport({"memory://gate": service}),
    )

    # 步 1：全新执行 -> needs_approval，data 中的键并入协议 output
    task = WorkerTask("g-1", "gate-worker", "提交退款", {"amount": 199}, "wf:gate:1")
    first = asyncio.run(endpoint.execute(task, "s-1"))
    assert first.status == "needs_approval"
    assert first.output.get("approval_request") == "refund-199" and first.output.get("decision") == "pending"
    ok("AgentResult.data 穿透 A2A 协议层（output 里可见结构化键）")

    # 步 2：resume 恢复 -> 幂等键复用 + 载荷到达 AgentRequest.resume
    resume_task = WorkerTask("g-1", "gate-worker", "提交退款", {"amount": 199},
                             "wf:gate:1", resume={"approved": True})
    second = asyncio.run(endpoint.execute(resume_task, "s-1"))
    assert second.status == "completed" and second.output.get("decision") == "approved"
    assert second.output.get("refund_id") == "RF-1"
    req = harness.last_request
    assert req is not None and req.resume == {"approved": True}
    ok("WorkerTask.resume -> A2A payload -> AgentRequest.resume 全链路透传")

    # 结构化输出能力声明
    assert Capability.STRUCTURED_OUTPUT in {m for m in Capability}
    ok("Capability.STRUCTURED_OUTPUT 声明结构化输出能力")


def test_skill_adapter_build_guards() -> None:
    print("[skill adapter guards]")
    adapter_files = (
        "harness/agentscope_mcp_adapter.py",
        "harness/langgraph_mcp_adapter.py",
        "harness/mcp_openai_runtime.py",
    )
    root = Path(__file__).resolve().parents[1]
    for adapter_file in adapter_files:
        source = (root / adapter_file).read_text(encoding="utf-8")
        assert "尚未翻译 Skill 声明" not in source and "尚不支持 Skill 声明" not in source
    base_source = (root / "harness/agentscope_adapter.py").read_text(encoding="utf-8")
    assert "if spec.tools:" in base_source and "if spec.tools or spec.skills:" not in base_source
    ok("四个 adapter 不再拒绝由 Harness 统一注入的 Skill 上下文")


if __name__ == "__main__":
    test_contract_invariants()
    test_http_a2a_transport()
    test_protected_fastapi_a2a()
    test_a2a_roundtrip()
    test_harness_standard_context()
    test_harness_skill_context()
    test_agentscope_adapter()
    test_openai_compatible_serving_configuration()
    test_magentic_adapter()
    test_magentic_write_guard()
    test_reflector_rules()
    test_tools_skills_spec_and_validate()
    test_skill_markdown_parser()
    test_skill_providers_and_registry()
    test_framework_fit_fields()
    test_skill_adapter_build_guards()
    test_react_workflow_integration()
    print(f"\nALL {PASS} CHECKS PASSED")
