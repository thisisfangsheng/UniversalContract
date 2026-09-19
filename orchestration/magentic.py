"""反思循环（REACT 模式）的 Magentic 实现引擎。

先读这一段（给初次接触者的三句话）：
1. REACT 模式 = "执行一轮 -> 观察结果 -> 决定继续还是结束"的循环；
2. 本文件用 MAF 框架自带的 Magentic 编排器来实现这个循环——Magentic 里
   的"manager（管理者）"每轮决定下一个该谁发言、该做什么；"participant
   （参与者）"就是被点到名的工人；
3. 对调用方完全透明：你只看到 create_react_workflow(engine="magentic")，
   工人还是原来的 WorkerEndpoint（经 A2A 调用）。

内部分工：
- MagenticWorkerAdapter：纯契约翻译（对话轮次 <-> 任务单/回执），
  不 import 任何 MAF 类型，可独立单测；
- MagenticWorkerClient：MAF 的聊天客户端绑定，把参与者收到的指令转发给
  适配器，把回执文本包装成回复——参与者侧没有 LLM，"大脑"是远程 Worker；
- build_magentic_workflow：装配入口（参与者 + 管理者 + 护栏），
  强制写操作治理：声明了高危能力的 Worker 不得注册为参与者；
- collect_magentic_result：把循环终局装配回公开契约 OrchestrationResult。

MAF 1.18.0 的三条实测行为（详见 docs/04_框架适配指南.md）：
- 参与者走流式调用路径，自定义聊天客户端在 stream=True 时必须返回
  ResponseStream（带 finalizer 折叠成完整响应），不能返回协程或裸生成器；
- MagenticManagerBase 是纯逻辑接口（plan/replan/create_progress_ledger/
  prepare_final_answer 四个方法），子类化即可实现不需要 LLM 的确定性
  管理者——离线示例与生产的管理者共用同一扩展点；
- 每轮管理者产出一个"进度账本"（progress ledger）：是否已完成、是否
  打转、是否有进展、下一个发言者、给它的指令。判定完成的规则见
  DeterministicMagenticManager 的注释。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from common.collaboration import build_collaboration_input
from common.timing import timing_span
from common.contracts import (
    LlmServingSpec, OrchestrationMode, OrchestrationRequest, OrchestrationResult, RoundRecord,
    WorkerDescriptor, WorkerEndpoint, WorkerResult, WorkerTask,
)

try:  # MAF 为可选依赖：适配器的翻译逻辑可在无 agent-framework 环境下单测
    from agent_framework import (
        Agent, BaseChatClient, ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream,
    )
    from agent_framework.orchestrations import (
        MagenticBuilder, MagenticManagerBase, MagenticProgressLedger, MagenticProgressLedgerItem,
    )

    _MAF_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MAF_AVAILABLE = False


_MANAGER_AUTHOR = "magentic_manager"


# ---------------------------------------------------------------------------
# 契约翻译层（无 MAF 依赖）
# ---------------------------------------------------------------------------


class MagenticWorkerAdapter:
    """把一个 WorkerEndpoint 包装为 Magentic participant 的语义等价物。

    与 A2AWorkerService 互为镜像：那边是 payload -> AgentRequest，这边是
    chat 轮次 -> WorkerTask、WorkerResult -> participant 发言文本。
    """

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        session_id: str,
        run_id: str,
        shared_state: dict[str, Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.session_id = session_id
        self.run_id = run_id
        self._round = 0
        self.worker_results: list[WorkerResult] = []
        # 所有 participant 持有同一个中立字典；只存事实和 WorkerResult，不存 MAF 对象。
        self._shared_state = shared_state if shared_state is not None else {"workflow_facts": {}, "results": []}

    # ---- participant 元数据（manager 依据它选择 next_speaker） ----

    @property
    def name(self) -> str:
        return self.endpoint.descriptor.worker_id

    @property
    def description(self) -> str:
        d = self.endpoint.descriptor
        caps = ", ".join(sorted(d.capabilities))
        base = d.description or d.worker_id
        return f"{base} [capabilities: {caps}]" if caps else base

    # ---- chat 轮次 -> WorkerTask -> WorkerResult -> 文本 ----

    async def handle_instruction(self, instruction: str, context: Mapping[str, Any] | None = None) -> str:
        self._round += 1
        task = WorkerTask(
            task_id=f"{self.name}-{self.run_id}-m{self._round}",
            worker_id=self.name,
            instruction=instruction,
            input=dict(context or {}),
            # 新一轮 = 新业务意图，幂等键全新；run_id 保证同一次 workflow 内可审计
            idempotency_key=f"{self.run_id}:{self.name}:m{self._round}",
        )
        shared_task = WorkerTask(
            task_id=task.task_id,
            worker_id=task.worker_id,
            instruction=task.instruction,
            input=build_collaboration_input(
                task,
                self._shared_state.get("workflow_facts", {}),
                self._shared_state.get("results", ()),
            ),
            idempotency_key=task.idempotency_key,
        )
        with timing_span(f"worker:{self.name}:round:{self._round}"):
            result = await self.endpoint.execute(shared_task, self.session_id)
        self.worker_results.append(result)
        self._shared_state.setdefault("results", []).append(result)
        return self._render(result)

    @staticmethod
    def _render(result: WorkerResult) -> str:
        """把协议终态翻译成对话文本：completed 带结果，失败类带显式状态前缀。"""
        if result.status == "completed":
            return str(result.output.get("text", ""))
        return f"[{result.status}] {result.error or 'no output'}"

    # ---- 结果收集 ----

    @property
    def rounds(self) -> tuple[RoundRecord, ...]:
        """每次 handle_instruction 视为该 Worker 在一轮中的参与记录（round=None）。"""
        return ()

    def reset(self) -> None:
        self._round = 0
        self.worker_results = []


# ---------------------------------------------------------------------------
# MAF 绑定层（需要 agent-framework）
# ---------------------------------------------------------------------------

if _MAF_AVAILABLE:

    class OpenAICompatibleMagenticClient(BaseChatClient):
        """将同一份 LlmServingSpec 接到 MAF manager_agent 的聊天客户端。"""

        OTEL_PROVIDER_NAME = "universal-contract-magentic-manager"

        def __init__(self, serving: LlmServingSpec) -> None:
            self._serving = serving

        @property
        def service_url(self) -> str:
            return self._serving.base_url

        def _inner_get_response(self, *, messages, stream=False, options=None, **kwargs):
            if stream:
                return ResponseStream(self._stream(messages), finalizer=self._finalize)
            return self._respond(messages)

        async def _stream(self, messages):
            text = await self._invoke(messages)
            yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(text)])

        def _finalize(self, updates) -> ChatResponse:
            text = "".join(update.text or "" for update in updates if getattr(update, "text", None))
            return ChatResponse(messages=[Message(role="assistant", contents=[text])])

        async def _respond(self, messages) -> ChatResponse:
            text = await self._invoke(messages)
            return ChatResponse(messages=[Message(role="assistant", contents=[text])])

        async def _invoke(self, messages) -> str:
            from harness.openai_compatible import openai_chat_completion

            translated = []
            for message in messages or ():
                if isinstance(message, str):
                    translated.append({"role": "user", "content": message})
                    continue
                text = "".join(content.text or "" for content in message.contents if hasattr(content, "text"))
                if text:
                    role = str(getattr(message, "role", "user"))
                    translated.append({"role": role if role in {"system", "user", "assistant"} else "user", "content": text})
            with timing_span("manager:llm"):
                return await openai_chat_completion(self._serving, translated)


    def create_openai_compatible_manager_agent(
        serving: LlmServingSpec,
        instructions: str | None = None,
    ) -> Agent:
        """创建由 OpenAI 兼容 Serving 驱动的 Magentic manager agent。

        ``instructions`` 允许具体 demo 或业务工作流添加任务依赖规则；未传入时
        保持通用的按 agent card 分派语义，避免把某个业务流程耦合到公共工厂。
        """
        return Agent(
            client=OpenAICompatibleMagenticClient(serving),
            instructions=instructions or (
                "You are the orchestration manager. Use each participant's agent card to select the "
                "best next worker based on its role, capabilities, and stated boundaries. Never assign work "
                "outside a participant's card and never invent an unregistered participant."
            ),
            name="orchestration-manager",
            description=(
                "根据 participant 的 agent card（角色、能力与边界）选择下一位 Worker；"
                "只分派已注册 participant，综合其回执后给出最终结论。"
            ),
        )

    class MagenticWorkerClient(BaseChatClient):
        """participant 的 chat client：prompt 进 -> A2A WorkerResult 文本出。

        LLM 不在这里。Agent 对象对 Magentic 只是"会说话的 participant"，
        说话内容由远程 Harness（经 A2AWorkerEndpoint）产生。
        """

        OTEL_PROVIDER_NAME = "universal-contract-worker"

        def __init__(self, adapter: MagenticWorkerAdapter, context: Mapping[str, Any] | None = None) -> None:
            self._adapter = adapter
            self._context = dict(context or {})

        @property
        def service_url(self) -> str:
            return self._adapter.endpoint.descriptor.endpoint

        def _inner_get_response(self, *, messages, stream=False, options=None, **kwargs):
            if stream:
                return ResponseStream(self._stream(messages), finalizer=self._finalize)
            return self._respond(messages)

        async def _stream(self, messages):
            text = await self._invoke(messages)
            yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(text)])

        def _finalize(self, updates) -> ChatResponse:
            text = "".join(u.text or "" for u in updates if getattr(u, "text", None))
            return ChatResponse(
                messages=[Message(role="assistant", contents=[text], author_name=self._adapter.name)],
                response_id=f"a2a-{self._adapter.name}-{self._adapter._round}",
            )

        async def _respond(self, messages) -> ChatResponse:
            text = await self._invoke(messages)
            return ChatResponse(
                messages=[Message(role="assistant", contents=[text], author_name=self._adapter.name)],
                response_id=f"a2a-{self._adapter.name}-{self._adapter._round}",
            )

        async def _invoke(self, messages) -> str:
            """从 orchestrator 消息中提取本轮 instruction。

            规则：倒序找第一条不是本 participant 自己发言的消息——即 manager
            的 instruction（author=magentic_manager）或初始 task 文本。
            """
            instruction = self._context.get("instruction")
            if not instruction:
                for msg in reversed(list(messages or ())):
                    if getattr(msg, "author_name", None) != self._adapter.name:
                        instruction = "".join(
                            c.text or "" for c in msg.contents if hasattr(c, "text")
                        )
                        break
            return await self._adapter.handle_instruction(
                instruction or "执行你被声明的能力", self._context.get("input")
            )

else:  # pragma: no cover

    OpenAICompatibleMagenticClient = None  # type: ignore[assignment]
    create_openai_compatible_manager_agent = None  # type: ignore[assignment]
    MagenticWorkerClient = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 确定性 manager 扩展点（生产中可替换为受约束 LLM manager）
# ---------------------------------------------------------------------------

if _MAF_AVAILABLE:

    class DeterministicMagenticManager(MagenticManagerBase):
        """最小确定性 manager：按声明顺序点名未发言的 Worker，全部发言即收敛。

        生产中替换为 StandardMagenticManager（LLM 驱动、Magentic-One 论文
        prompt）或自定义 MagenticManagerBase 子类；ledger 的确定性校验原则
        （Participant 白名单、轮数上限）由框架护栏与本类的注册检查共同承担。
        """

        def __init__(self, speaker_order: Sequence[str], task_text: str = "") -> None:
            super().__init__(max_stall_count=3, max_reset_count=1, max_round_count=16)
            self._speaker_order = list(speaker_order)
            self._task_text = task_text

        def _spoken(self, ctx) -> set[str]:
            return {
                msg.author_name
                for msg in ctx.chat_history
                if msg.author_name and msg.author_name not in (_MANAGER_AUTHOR,)
            }

        async def plan(self, ctx) -> Message:
            return Message(role="assistant", contents=[f"PLAN: 分派 {len(self._speaker_order)} 个已注册 Worker。"])

        async def replan(self, ctx) -> Message:
            return Message(role="assistant", contents=[f"REPLAN(round={ctx.round_count}): 重派未完成 Worker。"])

        async def create_progress_ledger(self, ctx) -> Any:
            pending = [s for s in self._speaker_order if s not in self._spoken(ctx)]
            if not pending:
                return MagenticProgressLedger(
                    is_request_satisfied=MagenticProgressLedgerItem(reason="all registered workers reported", answer=True),
                    is_in_loop=MagenticProgressLedgerItem(reason="no loop detected", answer=False),
                    is_progress_being_made=MagenticProgressLedgerItem(reason="workers finished", answer=True),
                    next_speaker=MagenticProgressLedgerItem(reason="n/a", answer=self._speaker_order[0]),
                    instruction_or_question=MagenticProgressLedgerItem(reason="n/a", answer=""),
                )
            nxt = pending[0]
            return MagenticProgressLedger(
                is_request_satisfied=MagenticProgressLedgerItem(reason="workers pending", answer=False),
                is_in_loop=MagenticProgressLedgerItem(reason="no loop detected", answer=False),
                is_progress_being_made=MagenticProgressLedgerItem(reason="worker will report", answer=True),
                next_speaker=MagenticProgressLedgerItem(reason="first pending worker", answer=nxt),
                instruction_or_question=MagenticProgressLedgerItem(reason="dispatch", answer=f"{nxt}：请依据你的能力处理当前任务并给出结论。"),
            )

        async def prepare_final_answer(self, ctx) -> Message:
            parts = []
            for msg in ctx.chat_history:
                if msg.author_name and msg.author_name != _MANAGER_AUTHOR:
                    text = "".join(c.text or "" for c in msg.contents if hasattr(c, "text"))
                    parts.append(text)
            return Message(role="assistant", contents=[" | ".join(parts)])

else:  # pragma: no cover

    DeterministicMagenticManager = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

_DEFAULT_FORBIDDEN = frozenset({"write", "payment", "refund-submit", "account-change"})


def build_magentic_workflow(
    workers: Sequence[WorkerEndpoint],
    session_id: str,
    run_id: str,
    *,
    manager=None,  # MagenticManagerBase 子类实例（如 DeterministicMagenticManager）
    manager_agent=None,  # 或 LLM Agent（agent_framework.Agent），二选一
    max_round_count: int = 8,
    max_stall_count: int = 3,
    max_reset_count: int = 1,
    enable_plan_review: bool = False,
    forbidden_capabilities: frozenset[str] = _DEFAULT_FORBIDDEN,
    task_context: Mapping[str, Any] | None = None,
):
    """装配 Magentic 工作流；返回 (workflow, adapters)。

    护栏（确定性代码，先于任何 LLM 决策）：
    1. Worker 白名单：participant 只能来自注册的 WorkerEndpoint 列表；
    2. 写操作治理：descriptor.capabilities 与 forbidden_capabilities 有交集的
       Worker 拒绝注册——主业务副作用必须回到 Workflow 显式节点；
    3. 轮数/停滞/重置上限直接交给 MagenticBuilder 内置护栏。
    """

    if not _MAF_AVAILABLE:  # pragma: no cover
        raise ImportError("需要安装 agent-framework（pip install agent-framework）才能构建 Magentic 工作流")
    if (manager is None) == (manager_agent is None):
        raise ValueError("manager 与 manager_agent 必须二选一")

    adapters: list[MagenticWorkerAdapter] = []
    participants = []
    # MAF participant 各自是 Agent 对象，故共享状态必须在其外部持有并显式传入。
    shared_state: dict[str, Any] = {"workflow_facts": dict(task_context or {}), "results": []}
    for worker in workers:
        blocked = worker.descriptor.capabilities & forbidden_capabilities
        if blocked:
            raise ValueError(
                f"Worker {worker.descriptor.worker_id} 含高危能力 {sorted(blocked)}，"
                "不允许注册为 Magentic participant（写操作必须回 Workflow 显式节点）"
            )
        adapter = MagenticWorkerAdapter(worker, session_id, run_id, shared_state)
        adapters.append(adapter)
        participants.append(
            Agent(
                client=MagenticWorkerClient(adapter, dict(task_context or {})),
                name=adapter.name,
                description=adapter.description,
            )
        )

    workflow = MagenticBuilder(
        participants=participants,
        manager=manager,
        manager_agent=manager_agent,
        max_round_count=max_round_count,
        max_stall_count=max_stall_count,
        max_reset_count=max_reset_count,
        enable_plan_review=enable_plan_review,
    ).build()
    return workflow, adapters


def collect_magentic_result(
    request: OrchestrationRequest,
    final_text: str,
    adapters: Sequence[MagenticWorkerAdapter],
) -> OrchestrationResult:
    """把 Magentic 的终局发言 + adapter 收集的 WorkerResult 装配回公开契约。

    OrchestrationResult 契约保持不变：summary 给人看，worker_results 供机器
    审计回溯；rounds 在 Magentic 模式下由 participant 轮次近似（plan 侧无
    静态计划，故每轮 RoundRecord.plan 取占位计划）。
    """

    all_results = [r for adapter in adapters for r in adapter.worker_results]
    has_approval = any(r.status == "needs_approval" for r in all_results)
    has_failure = any(r.status in ("failed", "cancelled") for r in all_results)
    status = "needs_approval" if has_approval else "failed" if has_failure else "completed"

    placeholder_plan = None
    rounds: list[RoundRecord] = []
    for adapter in adapters:
        for i, _result in enumerate(adapter.worker_results, start=1):
            if placeholder_plan is None or placeholder_plan.round != i:
                from common.contracts import OrchestrationPlan

                placeholder_plan = OrchestrationPlan(
                    plan_id=f"react-{request.workflow_id}-m{i}",
                    mode=OrchestrationMode.REACT,
                    tasks=(),
                    round=i,
                )
            rounds.append(RoundRecord(round=i, plan=placeholder_plan, results=(_result,)))

    return OrchestrationResult(
        workflow_id=request.workflow_id,
        status=status,  # type: ignore[arg-type]
        summary=final_text,
        worker_results=all_results,
        rounds=tuple(rounds),
    )
