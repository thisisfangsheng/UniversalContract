"""编排层：基于 Microsoft Agent Framework Workflow 的多智能体任务编排。

本文件把"主管（Leader）"和"反思器（Reflector）"两个角色绑到工作流图的
节点上，组成两种图：

图 1（单轮编排）：  plan -> dispatch -> aggregate
    主管生成一次性的任务计划，调度器执行，聚合器出结论。

图 2（反思循环）：  plan -> dispatch -> reflect -(继续)-> plan（回到开头）
                                       └-(结束)-> aggregate
    每轮执行完由反思器观察结果：需要更多任务就回到 plan 开新一轮，
    可以收敛了就把全部轮次交给聚合器。

统一入口：create_workflow(request, ...) 根据 request.mode 选择编排语义；REACT
分支内部由 react_engine 参数选择循环的引擎：
  - engine="magentic"（默认）：用 MAF 的 Magentic 编排器跑循环，
    WorkerEndpoint 经 magentic_orchestration 包装成 Magentic 的参与者；
  - engine="builtin"：本文件内置的确定性循环图（反思规则写死在代码里，
    无需大模型），适合受控流程和离线测试。

两种引擎共用同一份 WorkerEndpoint 契约与 A2A/运行器数据面，互相切换零改动。

MAF 1.18.0 的三条实测行为（写循环图时最容易踩的坑，详见 docs/03）：
1. 消息只沿着"发出消息的节点所连接的边"流动，条件边在投递瞬间求值；
2. 消息按类型路由到 handler；类型不匹配的消息会被静默丢弃（图随后闲置
   终止，不报错）——所以每条边两端的类型必须与 handler 签名严格对齐；
3. 节点实例跨轮保活（有状态）：同一 workflow 重复 run 前必须调用
   ReactOrchestration.reset() 清理内部状态。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler
from typing_extensions import Never

from common.collaboration import build_collaboration_input
from common.contracts import (
    LeaderPolicy, OrchestrationPlan, OrchestrationRequest, OrchestrationResult,
    LlmServingSpec, OrchestrationMode, ReflectionPolicy, RoundRecord, WorkerEndpoint, WorkerResult, WorkerTask,
)


# ---------------------------------------------------------------------------
# 工作流内部消息：在图的节点之间传递，只在本文件内使用，因此不放进 common/contracts.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanSignal:
    """反思节点 -> 计划节点：观察完毕，需要以增量任务开启下一轮。"""

    request: OrchestrationRequest  # 已合并反思产出的新事实（next_facts）
    new_tasks: Sequence[WorkerTask]  # 反思器建议的增量任务（下一轮要执行的）
    next_round: int                 # 下一轮的轮次号（当前轮 + 1）
    parent_plan_id: str             # 当前计划 ID（形成多轮追溯链）
    reason: str                     # 为什么要再跑一轮（写日志用）


@dataclass(frozen=True)
class FinalSignal:
    """反思节点 -> 聚合节点：循环结束，把全部轮次交给聚合器出终态。"""

    request: OrchestrationRequest
    history: tuple[RoundRecord, ...]  # 到目前为止的全部轮次记录
    reason: str                       # 结束原因：reflector_done（反思判定完成）/
                                      # reflector_failed（反思判定失败）/
                                      # max_rounds_exceeded（超过轮数上限被强制终止）


# ---------------------------------------------------------------------------
# 节点 1：计划（PlanExecutor）
# ---------------------------------------------------------------------------


class PlanExecutor(Executor):
    """工作流的起始节点：让主管（Leader）选择 Worker 和任务依赖，自己不执行业务。

    拥有两个 handler（MAF 按消息类型自动路由）：
    - plan()    处理初始请求（第一轮计划）
    - replan()  处理反思信号（后续轮次的增量计划）
    """

    def __init__(
        self,
        leader: LeaderPolicy,
        workers: Sequence[WorkerEndpoint],
        expected_mode: OrchestrationMode | None = None,
    ) -> None:
        super().__init__(id="leader-plan")  # 节点稳定 ID：checkpoint/恢复/日志都靠它定位
        self._leader = leader
        self._workers = workers
        self._expected_mode = expected_mode  # 通用工厂传入请求 mode，阻断计划与请求漂移

    @handler
    async def plan(
        self,
        request: OrchestrationRequest,
        ctx: WorkflowContext[tuple[OrchestrationRequest, OrchestrationPlan]],
    ) -> None:
        # 关键一行：传给主管的是 WorkerDescriptor（能力目录）列表，
        # 而不是 WorkerEndpoint 对象——主管拿到的是"电话号码"不是"电话机"，
        # 物理上无法绕过调度器直接调用 Worker。
        plan = await self._leader.create_plan(request, [worker.descriptor for worker in self._workers])
        if self._expected_mode is not None and plan.mode != self._expected_mode:
            raise ValueError(
                f"请求声明 mode={self._expected_mode}，但 Leader 生成了 mode={plan.mode} 的计划"
            )
        await ctx.send_message((request, plan))  # 原样带上 request：下游还要用 session_id / workflow_id

    @handler
    async def replan(self, signal: ReplanSignal, ctx: WorkflowContext[tuple[OrchestrationRequest, OrchestrationPlan]]) -> None:
        # 把反思器给出的增量任务组装成下一轮计划：轮次号 +1，parent_plan_id
        # 指向上一轮，形成可追溯的计划链。
        new_plan = OrchestrationPlan(
            plan_id=f"plan-r{signal.next_round}-{signal.parent_plan_id.split('plan-r', 1)[-1] if 'plan-r' in signal.parent_plan_id else signal.parent_plan_id}",
            mode=OrchestrationMode.REACT,
            tasks=signal.new_tasks,
            completion_rule="all",
            round=signal.next_round,
            parent_plan_id=signal.parent_plan_id,
        )
        await ctx.send_message((signal.request, new_plan))


# ---------------------------------------------------------------------------
# 节点 2：调度（DispatchExecutor）—— 控制流的核心
# ---------------------------------------------------------------------------


class DispatchExecutor(Executor):
    """按计划调度任务：并行模式用 gather 扇出；其余模式按序执行并检查依赖。"""

    def __init__(self, workers: Sequence[WorkerEndpoint]) -> None:
        super().__init__(id="worker-dispatch")
        # 把 Worker 列表转成 worker_id -> endpoint 的字典：既 O(1) 查找，
        # 又天然形成"注册目录"——查不到的 worker_id 会被 _execute 捕获为失败。
        self._workers = {worker.descriptor.worker_id: worker for worker in workers}

    @handler
    async def dispatch(
        self,
        message: tuple[OrchestrationRequest, OrchestrationPlan],
        ctx: WorkflowContext[tuple[OrchestrationRequest, OrchestrationPlan, Sequence[WorkerResult]]],
    ) -> None:
        request, plan = message

        if plan.mode == OrchestrationMode.CONCURRENT:
            # 并行模式：全部任务同时执行（gather 保序返回，结果顺序与计划一致）。
            # 并行的前提是计划中任务互不依赖——依赖合法性由主管的计划校验兜底。
            # 并行任务在启动前共享同一份事实快照；没有先完成者，因此 prior_results 为空。
            results = await asyncio.gather(*(
                self._execute(task, request.session_id, request.facts, ()) for task in plan.tasks
            ))
        else:
            # 顺序模式：Leader-Worker / 串行 / 接力 / 反思循环的轮内任务都走这里。
            # 串行与接力的差别不在调度器，而在主管写计划的方式（是否串 depends_on 链）。
            results = []
            completed: set[str] = set()  # 已成功完成的 task_id 集合（依赖检查用）
            for task in plan.tasks:
                # 依赖检查：声明的所有前置任务必须已成功完成（失败/待审批不算）
                if not set(task.depends_on).issubset(completed):
                    raise ValueError(f"任务 {task.task_id} 的依赖尚未完成")
                # 每个后续任务收到当前已完成任务的可审计回执快照；这让
                # LEADER_WORKER、SEQUENTIAL、HANDOFF 与 REACT 轮内都能协作共享信息。
                result = await self._execute(task, request.session_id, request.facts, results)
                results.append(result)
                if result.status == "completed":
                    completed.add(task.task_id)          # 只有成功才解锁下游
                elif plan.completion_rule == "all":
                    break                                 # 要求全成时一个失败就短路（省预算）

        # 失败没有抛异常：失败被"数据化"为回执的 status，控制流照常推进到
        # 下一节点，由聚合器统一判定终态——保证部分失败也能得出明确结论。
        await ctx.send_message((request, plan, results))

    async def _execute(
        self,
        task: WorkerTask,
        session_id: str,
        workflow_facts: Mapping[str, Any],
        completed_results: Sequence[WorkerResult],
    ) -> WorkerResult:
        """单任务执行：查目录调 endpoint；选了未注册 Worker 则返回规范化失败。"""
        try:
            shared_task = replace(
                task,
                input=build_collaboration_input(task, workflow_facts, completed_results),
            )
            return await self._workers[task.worker_id].execute(shared_task, session_id)
        except KeyError:
            # KeyError = 主管选了目录外的 Worker。返回失败的回执（而非抛异常），
            # 保证聚合器总能拿到结构化的结果。
            return WorkerResult(task.task_id, task.worker_id, "failed", error="Leader 选择了未注册 Worker")


# ---------------------------------------------------------------------------
# 节点 3：反思（ReflectExecutor）—— 反思循环新增的节点
# ---------------------------------------------------------------------------


class ReflectExecutor(Executor):
    """观察一轮执行结果，决定继续（replan）或收敛（结束）。

    确定性护栏（反思的输出一律按"提案"对待，不信任其自我约束）：
    - 要求继续但已达轮数上限       -> 强制结束（max_rounds_exceeded）；
    - 要求继续但没给任何新任务     -> 视为失败（空转防御）；
    - 单轮任务数超过上限           -> 视为失败（预算防御）。
    所有偏离契约的提案都数据化为失败终态，不抛异常。
    """

    def __init__(
        self,
        reflector: ReflectionPolicy,
        workers: Sequence[WorkerEndpoint],
        max_rounds: int = 4,
        max_tasks_per_round: int = 8,
    ) -> None:
        super().__init__(id="reflect")
        self._reflector = reflector
        self._descriptors = [worker.descriptor for worker in workers]  # 反思器也只看目录
        self._max_rounds = max_rounds
        self._max_tasks_per_round = max_tasks_per_round
        self._history: list[RoundRecord] = []  # 跨轮累积的执行记录（节点实例跨轮保活）

    @handler
    async def reflect(
        self,
        message: tuple[OrchestrationRequest, OrchestrationPlan, Sequence[WorkerResult]],
        ctx: WorkflowContext[ReplanSignal | FinalSignal],
    ) -> None:
        request, plan, results = message
        # 先把本轮打包存档（观察记录按轮累积）
        self._history.append(RoundRecord(round=plan.round, plan=plan, results=tuple(results)))

        # 让反思器阅读全部历史，产出决策
        reflection = await self._reflector.reflect(request, tuple(self._history), self._descriptors)

        # 按决策分派（护栏在 replan 分支内）
        if reflection.decision == "done":
            await ctx.send_message(FinalSignal(request, tuple(self._history), "reflector_done"))
            return
        if reflection.decision == "failed":
            await ctx.send_message(FinalSignal(request, tuple(self._history), "reflector_failed"))
            return

        # decision == "replan"：逐条过确定性护栏
        next_round = plan.round + 1
        if next_round > self._max_rounds:                        # 护栏 1：轮数上限（硬终止）
            await ctx.send_message(FinalSignal(request, tuple(self._history), "max_rounds_exceeded"))
            return
        if not reflection.new_tasks:                             # 护栏 2：说要继续却没给任务
            await ctx.send_message(FinalSignal(request, tuple(self._history), "reflector_failed"))
            return
        if len(reflection.new_tasks) > self._max_tasks_per_round:  # 护栏 3：单轮任务数上限
            await ctx.send_message(FinalSignal(request, tuple(self._history), "reflector_failed"))
            return

        # 合并反思产生的新事实（洞察）进请求，形成下一轮的输入
        merged_facts = {**request.facts, **dict(reflection.next_facts)}
        next_request = replace(request, facts=merged_facts)
        await ctx.send_message(
            ReplanSignal(
                request=next_request,
                new_tasks=reflection.new_tasks,
                next_round=next_round,
                parent_plan_id=plan.plan_id,
                reason=reflection.reason,
            )
        )

    def reset(self) -> None:
        """同一 workflow 实例重复 run 前必须复位轮次历史（节点跨轮保活）。"""
        self._history = []


# ---------------------------------------------------------------------------
# 节点 4：聚合（AggregateExecutor）
# ---------------------------------------------------------------------------


class AggregateExecutor(Executor):
    """终止节点：把任务回执收敛为业务终态。

    两个 handler 分别服务两种图：
    - aggregate       单轮图（plan -> dispatch -> aggregate）
    - aggregate_rounds 反思循环图（收 FinalSignal，汇总全部轮次）
    """

    def __init__(self) -> None:
        super().__init__(id="leader-aggregate")

    @handler
    async def aggregate(
        self,
        message: tuple[OrchestrationRequest, OrchestrationPlan, Sequence[WorkerResult]],
        ctx: WorkflowContext[Never, OrchestrationResult],
    ) -> None:
        """单轮聚合。WorkflowContext 的第一个泛型是 Never：不再向下游发消息。"""
        request, plan, results = message
        status = self._judge_single_round(plan.completion_rule, results)
        summary = self._summarize(results)
        await ctx.yield_output(OrchestrationResult(request.workflow_id, status, summary, results))

    @handler
    async def aggregate_rounds(self, signal: FinalSignal, ctx: WorkflowContext[Never, OrchestrationResult]) -> None:
        """多轮聚合：汇总所有轮次的回执，按结束原因与最后一轮结果判终态。"""
        all_results = [result for record in signal.history for result in record.results]

        if signal.reason == "max_rounds_exceeded":
            status: str = "failed"                       # 被护栏强制终止 -> 失败
        elif signal.reason == "reflector_failed":
            status = "failed"                            # 反思明确放弃 -> 失败
        elif any(result.status == "needs_approval" for result in all_results):
            status = "needs_approval"                    # 任一任务要审批 -> 整体挂起（优先级最高）
        else:
            # 正常收敛：看最后一轮——非空且全部成功才算完成
            last = signal.history[-1].results if signal.history else ()
            status = "completed" if last and all(result.status == "completed" for result in last) else "failed"

        summary = self._summarize(all_results)
        await ctx.yield_output(
            OrchestrationResult(signal.request.workflow_id, status, summary, all_results, signal.history)  # type: ignore[arg-type]
        )

    @staticmethod
    def _judge_single_round(completion_rule: str, results: Sequence[WorkerResult]) -> str:
        """单轮终态判定。优先级：审批 > 失败 > 完成（协议成功不等于业务成功）。"""
        if any(result.status == "needs_approval" for result in results):
            return "needs_approval"                      # 优先级最高：哪怕其他任务都成功也要挂起
        if completion_rule == "all" and any(result.status != "completed" for result in results):
            return "failed"                              # 要求全成但存在失败/取消
        if not results:
            return "failed"                              # 空结果防御：宁失败不给误导性的"完成"
        return "completed"

    @staticmethod
    def _summarize(results: Sequence[WorkerResult]) -> str:
        """生成给人看的摘要：优先取文本输出，失败取错误信息，用 | 拼接。"""
        return " | ".join(str(result.output.get("text", result.error or "")) for result in results)


# ---------------------------------------------------------------------------
# 装配函数
# ---------------------------------------------------------------------------


def create_maf_workflow(
    leader: LeaderPolicy,
    workers: Sequence[WorkerEndpoint],
    *,
    expected_mode: OrchestrationMode | None = None,
) -> Workflow:
    """装配单轮编排图：plan -> dispatch -> aggregate（v1 语义，保持兼容）。"""

    planner = PlanExecutor(leader, workers, expected_mode)
    dispatcher = DispatchExecutor(workers)
    aggregator = AggregateExecutor()
    return (
        WorkflowBuilder(start_executor=planner, name="universal-leader-worker")
        .add_edge(planner, dispatcher)          # 计划完成后交给调度
        .add_edge(dispatcher, aggregator)       # 调度完成后直接聚合（无反思）
        .build()
    )


def create_react_workflow(
    workers: Sequence[WorkerEndpoint],
    *,
    engine: str = "magentic",  # "magentic"（生产默认，MAF 承担全部迭代循环）| "builtin"（确定性参考实现，可离线/可单测）
    # -- engine="magentic" 参数（实现：magentic_orchestration.build_magentic_workflow）--
    manager=None,  # MagenticManagerBase 子类实例（如 DeterministicMagenticManager）
    manager_agent=None,  # 或 LLM Agent；与 manager 二选一
    session_id: str = "",
    run_id: str = "",
    max_round_count: int = 8,
    max_stall_count: int = 3,
    max_reset_count: int = 1,
    # -- engine="builtin" 参数（实现：本文件的回边图）--
    leader: LeaderPolicy | None = None,
    reflector: ReflectionPolicy | None = None,
    # -- 共用护栏 --
    max_rounds: int = 4,
    max_tasks_per_round: int = 8,
    task_context: Mapping[str, Any] | None = None,
) -> ReactOrchestration:
    """反思循环（REACT）模式的统一入口：模式在契约里只有一个，引擎在这里选。

    - engine="magentic"（默认）：MAF Magentic 承担 plan/ledger/replan/final
      全部循环逻辑；WorkerEndpoint 经 MagenticWorkerAdapter 适配为参与者。
      manager 与 manager_agent 必须二选一。
    - engine="builtin"：确定性回边图。leader + reflector 必须提供。
      无 LLM 依赖，适合受控流程、离线测试与"反思即规则"的场景。

    返回 ReactOrchestration 句柄：统一两引擎的 reset() 与 finalize()。
    """

    if engine == "magentic":
        from .magentic import build_magentic_workflow

        if (manager is None) == (manager_agent is None):
            raise ValueError('engine="magentic" 需要 manager 或 manager_agent 二选一')
        workflow, adapters = build_magentic_workflow(
            workers,
            session_id=session_id,
            run_id=run_id,
            manager=manager,
            manager_agent=manager_agent,
            max_round_count=max_round_count,
            max_stall_count=max_stall_count,
            max_reset_count=max_reset_count,
            task_context=task_context,
        )
        return ReactOrchestration(workflow=workflow, engine="magentic", _adapters=adapters)

    if engine == "builtin":
        if leader is None or reflector is None:
            raise ValueError('engine="builtin" 需要 leader 与 reflector')
        planner = PlanExecutor(leader, workers, OrchestrationMode.REACT)
        dispatcher = DispatchExecutor(workers)
        reflector_executor = ReflectExecutor(reflector, workers, max_rounds, max_tasks_per_round)
        aggregator = AggregateExecutor()
        workflow = (
            WorkflowBuilder(start_executor=planner, name="universal-react-builtin")
            .add_edge(planner, dispatcher)
            .add_edge(dispatcher, reflector_executor)
            # 回边：反思器发出"再规划"信号时回到计划节点（循环的核心）
            .add_edge(reflector_executor, planner, condition=lambda m: isinstance(m, ReplanSignal))
            # 终边：反思器发出"结束"信号时进入聚合
            .add_edge(reflector_executor, aggregator, condition=lambda m: isinstance(m, FinalSignal))
            .build()
        )
        return ReactOrchestration(workflow=workflow, engine="builtin", _reflector_executor=reflector_executor)

    raise ValueError(f"未知 engine: {engine!r}（可选 'magentic' 或 'builtin'）")


def create_workflow(
    request: OrchestrationRequest,
    workers: Sequence[WorkerEndpoint],
    *,
    leader: LeaderPolicy | None = None,
    react_engine: str = "magentic",
    manager=None,
    manager_agent=None,
    max_round_count: int = 8,
    max_stall_count: int = 3,
    max_reset_count: int = 1,
    reflector: ReflectionPolicy | None = None,
    max_rounds: int = 4,
    max_tasks_per_round: int = 8,
    llm_serving: LlmServingSpec | None = None,
) -> Workflow | ReactOrchestration:
    """按 ``OrchestrationRequest.mode`` 构造唯一匹配的 workflow。

    mode 是调用方声明的编排语义，因而只允许本函数决定实现分支：REACT
    使用指定的反思引擎；其他 mode 使用 Leader 生成一次性计划的工作流，并在
    PlanExecutor 中校验计划 mode 与请求 mode 必须一致。旧工厂保留给兼容调用，
    新代码应优先使用本函数。
    """

    if llm_serving is not None:
        from harness.openai_compatible import OpenAICompatibleLeader, OpenAICompatibleReflector
        if leader is None:
            leader = OpenAICompatibleLeader(llm_serving)
        if request.mode == OrchestrationMode.REACT and reflector is None and react_engine == "builtin":
            reflector = OpenAICompatibleReflector(llm_serving)

    if request.mode == OrchestrationMode.REACT:
        return create_react_workflow(
            workers,
            engine=react_engine,
            manager=manager,
            manager_agent=manager_agent,
            session_id=request.session_id,
            run_id=request.workflow_id,
            max_round_count=max_round_count,
            max_stall_count=max_stall_count,
            max_reset_count=max_reset_count,
            leader=leader,
            reflector=reflector,
            max_rounds=max_rounds,
            max_tasks_per_round=max_tasks_per_round,
            task_context=request.facts,
        )

    if leader is None:
        raise ValueError(f"mode={request.mode} 的单轮编排需要 leader")
    if react_engine != "magentic" or manager is not None or manager_agent is not None or reflector is not None:
        raise ValueError("非 REACT mode 不接受 react_engine、manager、manager_agent 或 reflector")
    return create_maf_workflow(leader, workers, expected_mode=request.mode)


@dataclass
class ReactOrchestration:
    """反思循环的运行句柄：屏蔽两种引擎在生命周期与结果形态上的差异。

    - builtin 引擎：workflow.run() 产出的 outputs[-1] 就是 OrchestrationResult；
    - magentic 引擎：outputs[-1] 是编排器（manager）的终局发言（文本），
      需要结合适配器收集的任务回执装配回公开契约。
    finalize() 统一这两条路径，调用方始终拿到 OrchestrationResult。
    """

    workflow: Workflow
    engine: str  # "magentic" | "builtin"
    _adapters: Any = None             # magentic 引擎：各 Worker 的适配器（收集回执用）
    _reflector_executor: Any = None   # builtin 引擎：反思节点（复位历史用）

    @property
    def adapters(self) -> Sequence[Any]:
        """返回 Magentic participant adapter 的只读序列；builtin 引擎返回空元组。"""
        return tuple(self._adapters or ())

    def reset(self) -> None:
        """同一 workflow 实例重复 run 前必须复位引擎内状态。"""
        if self.engine == "builtin" and self._reflector_executor is not None:
            self._reflector_executor.reset()
        if self.engine == "magentic" and self._adapters is not None:
            for adapter in self._adapters:
                adapter.reset()

    async def finalize(
        self,
        request: OrchestrationRequest,
        run_result: Any = None,
        *,
        final_text: str | None = None,
    ) -> OrchestrationResult:
        """把引擎的运行产物装配回公开契约 OrchestrationResult。"""
        if self.engine == "builtin":
            if run_result is None:
                raise ValueError("builtin 引擎的 finalize 需要 run_result")
            return list(run_result.get_outputs())[-1]

        # magentic 引擎：从输出里提取编排器的终局发言文本
        if final_text is None and run_result is not None:
            for output in reversed(list(run_result.get_outputs())):
                text = getattr(output, "text", None)
                if text:
                    final_text = str(text)
                    break
        if final_text is None:
            raise ValueError('magentic 引擎的 finalize 需要 run_result 或 final_text')
        from .magentic import collect_magentic_result

        return collect_magentic_result(request, final_text, self._adapters)  # type: ignore[arg-type]
