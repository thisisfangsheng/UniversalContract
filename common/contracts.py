"""类型契约层：整个项目唯一被所有模块共同依赖的文件。

本文件只定义"数据长什么样"（dataclass）和"接口长什么样"（Protocol），
不含任何业务逻辑，也不导入任何 AI 框架（MAF / AgentScope / LangGraph /
OpenAI SDK 都不出现在这里）。

这样做的目的：无论底层换用哪个 AI 框架，只要照着这里的类型来实现，
上层（编排）和下层（协议、运行器）的代码就一行都不用改。

阅读顺序建议：
1. 先看"单智能体运行器"相关类型（AgentRequest ~ Harness Protocol）
2. 再看"任务编排"相关类型（WorkerDescriptor ~ A2ATransport Protocol）
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


# ===========================================================================
# 能力声明：runtime（如 MAF/AgentScope）实际支持什么，用枚举说清楚
# ===========================================================================


class Capability(StrEnum):
    """单智能体运行环境（runtime）的能力清单。

    用途：声明方（HarnessSpec.requested_capabilities）说"我需要这些能力"，
    实现方（runtime adapter）说"我支持这些能力"，
    ProviderHarness.validate() 在启动前做差集检查，防止运行时才发现不支持。
    """

    TOOL_LOOP = "tool_loop"                          # 会执行"模型调用工具再反馈结果"的循环
    DURABLE_SESSION = "durable_session"              # 会话状态可持久化、可恢复
    STREAMING = "streaming"                          # 支持流式输出（边生成边返回）
    HUMAN_APPROVAL = "human_approval"                # 支持人工审批暂停/恢复
    BACKGROUND_AGENTS = "background_agents"          # 支持后台异步运行子智能体
    STRUCTURED_OUTPUT = "structured_output"          # 能产出结构化结果（AgentResult.data）


class McpTransport(StrEnum):
    """MCP（Model Context Protocol）服务器的传输方式。

    六个主流框架（MAF / AgentScope / LangChain / OpenAI SDK / Google ADK /
    DeepAgents）全部支持 STREAMABLE_HTTP 与 STDIO，因此这两种是安全选项。
    """

    STREAMABLE_HTTP = "streamable_http"              # 通过 HTTP 远程连接（最常用）
    WEBSOCKET = "websocket"                          # 通过 WebSocket 长连接（仅 MAF 支持）
    STDIO = "stdio"                                  # 以子进程方式在本地启动（命令行交互）


@dataclass(frozen=True)
class McpServerSpec:
    """一个 MCP 服务器的"中立声明"。

    这里的字段只是描述"要连接什么、暴露哪些工具"，不含任何框架对象。
    各框架的适配器（adapter）在构建 agent 时把它翻译成原生写法，例如：
      - MAF:        MCPStreamableHTTPTool(name=..., url=..., allowed_tools=...)
      - AgentScope: MCPClient(mcp_config=HttpMCPConfig(url=...)) -> Toolkit(mcps=[...])
      - LangChain:  MultiServerMCPClient({"name": {"transport": "http", "url": ...}})
      - OpenAI SDK: MCPServerStreamableHttp(params=..., tool_filter=...)
      - Google ADK: McpToolset(connection_params=..., tool_filter=[...])
    翻译对照详见 docs/04_框架适配指南.md。
    """

    name: str                                        # 服务器逻辑名（同时是工具命名空间，如 mcp__{name}__{tool}）
    transport: McpTransport                          # 传输方式：决定适配器选用哪个原生类
    url: str = ""                                    # 远程地址（STREAMABLE_HTTP / WEBSOCKET 时必填）
    command: str = ""                                # 本地启动命令（STDIO 时必填，如 npx）
    args: tuple[str, ...] = ()                       # 启动命令的参数列表
    env: Mapping[str, str] = field(default_factory=dict)      # 子进程环境变量
    description: str = ""                            # 用途描述（会出现在提示词/能力目录里）
    headers: Mapping[str, str] = field(default_factory=dict)  # HTTP 认证头（建议只存环境变量引用，不存明文）
    allowed_tools: frozenset[str] = frozenset()      # 工具白名单：空 = 全部暴露（各家都有对应参数）
    blocked_tools: frozenset[str] = frozenset()      # 工具黑名单：与白名单同时存在时黑名单优先
    requires_approval: bool = False                  # 是否需要人工审批（对应 MAF approval_mode）
    request_timeout_s: int | None = None             # 请求超时秒数；None = 用适配器默认值
    load_tools: bool = True                          # 是否把服务器的 tools 暴露为可调用工具
    load_skills: bool = False                        # 是否发现服务器发布的 skills；
                                                     # 默认关闭：skill 内容会进入提示词，属于外部输入，需谨慎


@dataclass(frozen=True)
class SkillSpec:
    """一个 agent skill（技能）的"中立声明"。

    skill 是一种"指令集"而不是可调用的工具：它是一份带元数据的
    SKILL.md 文档，智能体先看目录（名称+描述），需要时读全文，
    再用自己已有的工具按文档执行。
    这个形态来自 Anthropic 的 Agent Skills 规范（agentskills.io），
    MAF / AgentScope / DeepAgents 已原生支持；OpenAI SDK / Google ADK /
    LangGraph 没有原生支持，适配器会退化为"把内容作为上下文注入"。
    """

    name: str                                        # 技能名（对应 SKILL.md frontmatter 的 name）
    source: Literal["path", "inline", "mcp"]         # 来源：磁盘目录 / 直接内联文本 / 从 MCP 服务器发现
    path: str = ""                                   # source="path"：技能目录（内含 SKILL.md）
    content: str = ""                                # source="inline"：SKILL.md 全文
    server_ref: str = ""                             # source="mcp"：引用 tools 里同名 McpServerSpec
    allowed_tools: frozenset[str] = frozenset()      # 执行该技能预批准的工具（对应 frontmatter.allowed-tools）
    enable_scripts: bool = False                     # 是否允许执行技能自带脚本（有安全风险，默认禁止）


class OrchestrationMode(StrEnum):
    """编排模式：多个工作者（Worker）之间如何协作。

    前四种是"一次成型"的计划：任务和依赖在执行前就定好。
    REACT 是"边执行边调整"：每轮执行后反思结果，决定继续加任务还是结束。
    """

    LEADER_WORKER = "leader_worker"                  # 主管分派任务给工人，收齐结果后汇总
    SEQUENTIAL = "sequential"                        # 串行：上一个成功才执行下一个
    CONCURRENT = "concurrent"                        # 并行：所有任务同时执行（asyncio.gather）
    HANDOFF = "handoff"                              # 接力：下一个任务显式依赖上一个的产出
    REACT = "react"                                  # 反思循环：执行 -> 观察 -> 再规划，直到收敛
    # 注意：REACT 只声明"要这种协作方式"，它背后用哪个引擎跑（MAF 的
    # Magentic 编排器 / 本项目内置的确定性循环图）是实现细节，
    # 由编排层的 engine 参数选择——调用方不需要知道。


class AuthMode(StrEnum):
    """A2A HTTP 服务的认证终止方式。"""

    NONE = "none"
    GATEWAY = "gateway"
    OIDC = "oidc"
    MTLS = "mtls"


@dataclass(frozen=True)
class PrincipalContext:
    """认证后的调用方身份；不保存原始 token、证书或厂商 claims。"""

    subject: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    auth_mode: AuthMode = AuthMode.NONE


@dataclass(frozen=True)
class RequestContext:
    """跨协议边界传播的已验证请求范围，用于审计和租户数据隔离。"""

    principal: PrincipalContext
    request_id: str = ""
    trace_id: str = ""


# ===========================================================================
# 单智能体运行器（Harness）相关类型
# "Harness" 指包裹住单个 agent 的执行外壳：它负责加载会话、注入上下文、
# 调用 agent、把结果记回会话。agent 本身由各框架实现。
# ===========================================================================


@dataclass(frozen=True)
class AgentIdentity:
    """智能体的身份三要素：唯一 ID + 展示名 + 说明。"""

    agent_id: str                                    # 稳定唯一标识（如 "policy-worker"）
    name: str                                        # 展示名（如 "政策 Worker"，给人看）
    description: str = ""                            # 一句话说明（给人看，也可进提示词）


@dataclass(frozen=True)
class LlmServingSpec:
    """OpenAI 兼容 LLM Serving 的连接声明。

    每个 HarnessSpec 可声明自己的 Serving；编排层也可单独持有一份，因此
    Worker、Leader 与 Reflector 可以使用不同模型和不同网关。密钥只在本地
    适配器创建客户端时使用，绝不可写入 AgentRequest、SharedSession 或日志。
    """

    model: str                                      # 服务端暴露的模型名
    base_url: str = "https://api.openai.com/v1"    # OpenAI 兼容 Chat Completions 根地址
    api_key: str = field(default="", repr=False)   # 建议从环境变量或 secret manager 注入
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class HarnessSpec:
    """单智能体的"出生证明"：静态声明它是谁、要做什么、配了什么。

    所有字段都是纯数据，不含任何框架对象——各框架适配器拿到的就是这份声明，
    在 build() 时翻译成原生构造（模型客户端、工具箱、技能加载器等）。

    tools/skills 是"静态配置"：agent 装了什么工具、什么技能；
    它们与运行期的"会话状态"（SharedSession / ContextItem）是两回事：
    前者决定 agent 能做什么，后者记录这一轮对话进行到哪里。
    切换框架时：前者按 spec 重建，后者随 session 迁移——会话不断。
    """

    identity: AgentIdentity                          # 身份
    instructions: str                                # 系统指令（告诉 agent 它的角色和行为规范）
    requested_capabilities: frozenset[Capability] = frozenset()  # 申请的能力清单（启动前校验）
    tools: Sequence[McpServerSpec] = ()              # 要连接的 MCP 服务器列表
    skills: Sequence[SkillSpec] = ()                 # 要装载的技能列表
    llm_serving: LlmServingSpec | None = None         # 本 agent 专属模型服务；None = 由 runtime 默认值提供


@dataclass(frozen=True)
class AgentRequest:
    """一次 agent 调用的完整输入。"""

    request_id: str                                  # 本次请求的唯一 ID（跨层审计的锚点）
    session_id: str                                  # 会话 ID：决定加载哪个 SharedSession（跨请求的连续性）
    input: str                                       # 用户/编排器给 agent 的指令文本
    metadata: Mapping[str, Any] = field(default_factory=dict)  # 开放扩展槽（如幂等键、租户、trace ID）
    resume: Any = None                               # 人工审批/中断的恢复载荷；
                                                     # None = 全新请求；非 None = 从中断处继续
                                                     # （恢复时幂等键必须复用原任务的键，防止重复执行）
    request_context: RequestContext | None = None    # HTTP 边界已验证的调用方范围；不含原始凭据


@dataclass(frozen=True)
class AgentResult:
    """一次 agent 调用的结果。

    output 给人看（自然语言）；data 给机器用（结构化键值，可 JSON 序列化）。
    四个主流框架都有结构化输出的原生能力（OpenAI 的 output_type、
    MAF 的结构化响应、AgentScope 的 structured_model、LangGraph 的 state），
    data 字段就是为它们准备的统一出口。
    """

    status: Literal["completed", "needs_approval", "needs_input", "failed", "cancelled"]
    output: str | None = None                        # 文本结果
    run_id: str | None = None                        # 本次运行在 runtime 侧的运行 ID（用于审计/恢复）
    error: str | None = None                         # 失败原因（status 非 completed 时）
    data: Mapping[str, Any] | None = None            # 结构化结果（键值可序列化）


@dataclass
class SharedSession:
    """跨框架共享的会话状态——多智能体之间"业务连续性"的载体。

    这是全文件唯一允许被修改（非 frozen）的数据类：每次 agent 调用后
    ProviderHarness 会原地追加内容再保存。
    三条铁律：
    1. 只存中立数据（字符串/字典），绝不存任何框架的原生对象——
       这样任何框架都能读懂它，切换框架时会话不丢；
    2. 各框架私有的内部状态（如 LangGraph checkpoint）不写进来；
    3. runtime_references 只存可序列化的 ID（如 run_id），便于追溯。
    """

    session_id: str                                  # 会话唯一 ID（一次客户会话可发起多次工作流）
    facts: dict[str, str] = field(default_factory=dict)            # 结构化事实（键值对，如 order_id=ORD-1001）
    messages: list[str] = field(default_factory=list)              # 对话历史（按时间追加）
    runtime_references: dict[str, dict[str, str]] = field(default_factory=dict)  # 各 runtime 的运行记录（如 run_id）


@dataclass(frozen=True)
class ContextItem:
    """注入给 agent 的一条上下文（如协作快照、会话记忆、租户规则或技能内容）。

    ProviderHarness 总会产生三个保留标签：``contract.collaboration``、
    ``contract.session.facts`` 和 ``contract.session.messages``。它们是多 Agent
    协作与跨 runtime 连续性的标准数据面；扩展 ContextProvider 不应覆盖它们。
    """

    label: str                                       # 条目名（agent 结果里可引用来源，如 tenant-policy=...）
    content: str                                     # 条目内容
    priority: int = 100                              # 排序权重：数值小的排前面（先注入）


# ===========================================================================
# Provider（依赖注入接口）：会话存取 + 上下文生产
# ===========================================================================


class SessionProvider(Protocol):
    """会话存取接口：任何能按 session_id 读写 SharedSession 的类都满足。

    Python 的 Protocol 是"结构化子类型"：实现类不需要显式继承本接口，
    只要方法签名匹配即可（例如 demo 里的 InMemorySessions）。
    """

    async def load(self, session_id: str) -> SharedSession: ...      # 取会话（不存在则通常创建空会话）
    async def save(self, session: SharedSession) -> None: ...        # 保存会话


class ContextProvider(Protocol):
    """上下文生产接口：每次 agent 调用前，产出若干条注入内容。

    典型实现：租户政策注入、历史事实摘要、（降级模式下）skill 内容注入。
    能同时拿到 request（本次请求）和 session（历史会话），所以内容可以
    同时引用"本次要做什么"和"之前发生过什么"。
    """

    async def provide(self, request: AgentRequest, session: SharedSession) -> Sequence[ContextItem]: ...


@dataclass(frozen=True)
class HarnessProviders:
    """Provider 的打包容器：随 HarnessSpec 一起注入运行器。"""

    session: SessionProvider                         # 会话存取（必填）
    contexts: Sequence[ContextProvider] = ()         # 上下文生产者（可多个，按优先级合并）


@dataclass(frozen=True)
class HarnessExecutionContext:
    """交给框架适配器的"全部世界"。

    适配器在 build()/run() 时能看到的只有这个对象——里面没有任何框架类型、
    没有网络协议类型、没有编排类型。这保证了：适配器不可能越界。
    """

    request: AgentRequest                            # 本次请求
    session: SharedSession                           # 已恢复的会话（可读，agent 也可往里主动记忆）
    context_items: Sequence[ContextItem]             # 已按 priority 排好序，含 Harness 标准协作/会话条目
    providers: HarnessProviders                      # Provider 打包（agent 运行中如需主动存取会话可用）


class RunningAgent(Protocol):
    """已经构建完成、可以运行的 agent（由各框架适配器实现）。

    合规实现必须将 ``context.context_items`` 完整地映射至其原生 runtime 的
    prompt、message 或 state。特别是 ``contract.`` 前缀的保留条目由
    ProviderHarness 自动提供，adapter 不应绕过或另行解析协作 metadata、会话对象。
    """

    async def run(self, context: HarnessExecutionContext) -> AgentResult: ...         # 一次性执行，返回终态
    def run_stream(self, context: HarnessExecutionContext) -> AsyncIterator[AgentResult]: ...  # 流式执行


class HarnessRuntimeAdapter(Protocol):
    """框架适配器：把某个具体框架（MAF/AgentScope/LangGraph/OpenAI...）接进契约的入口。

    这是整个项目最重要的扩展点：每接入一个新框架，就是实现这四个成员。
    所有框架相关的 import 都应该只出现在适配器实现文件里。
    """

    runtime_name: str                                # 框架标识（如 "agentscope-worker"；也是 runtime_references 的键）
    def capabilities(self) -> frozenset[Capability]: ...  # 本框架支持哪些能力（供 validate 校验）
    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> RunningAgent: ...
    # ↑ 工厂方法：把中立声明（spec）翻译成原生 agent；只在第一次真正有请求时才调用（懒加载）
    async def close(self) -> None: ...               # 释放资源（模型客户端连接、MCP 连接等）


class Harness(Protocol):
    """单智能体的本地调用面——上层（如 A2A 服务端）眼中的 Harness 就长这样。

    注意：ProviderHarness 并没有继承本 Protocol——Python 结构化子类型下，
    方法签名匹配即可当作 Harness 使用。
    """

    async def validate(self) -> Sequence[str]: ...   # 启动前校验，返回问题清单（空 = 通过）
    async def run(self, request: AgentRequest) -> AgentResult: ...
    async def close(self) -> None: ...


# ===========================================================================
# 任务编排相关类型：主管（Leader）如何描述计划、如何调用远程工作者（Worker）
# ===========================================================================


@dataclass(frozen=True)
class WorkerDescriptor:
    """工作者（Worker）的能力目录条目——主管"看得见"的全部信息。

    关键设计：主管拿到的是 endpoint 地址（字符串），不是对象引用。
    这意味着 Worker 可以在远程、可以换框架、可以独立部署，主管毫无感知。
    """

    worker_id: str                                   # 稳定唯一 ID（如 "policy-worker"）
    endpoint: str                                    # 调用地址（如 "memory://policy" 或 "https://..."）
    capabilities: frozenset[str]                     # 业务能力标签（如 {"refund-policy"}，供主管选人）
    description: str = ""                            # 人类可读说明


@dataclass(frozen=True)
class WorkerTask:
    """一张"任务单"：主管分配给某个 Worker 的一次工作单元。

    task_id / idempotency_key 是可靠性设计的核心：
    - task_id：本次工作单元的唯一 ID，贯穿全链路（请求 -> 运行 -> 审计）；
    - idempotency_key：幂等键——网络超时不代表对方没执行，
      恢复时先按此键查询，防止重复提交（如重复退款）。
      新的业务意图才用新键；恢复中断时必须复用旧键。
    """

    task_id: str
    worker_id: str                                   # 目标 Worker（编排器按此查目录）
    instruction: str                                 # 明确的任务指令
    input: Mapping[str, Any]                         # 结构化输入；编排时会封装为 collaboration 快照
    idempotency_key: str                             # 幂等键（必填，防止远程副作用重复执行）
    depends_on: tuple[str, ...] = ()                 # 前置任务 ID（串行/接力模式的依赖声明）
    resume: Any = None                               # 恢复载荷（人工审批后继续时携带；None = 全新任务）


@dataclass(frozen=True)
class WorkerResult:
    """任务回执：Worker 执行后的规范化结果。"""

    task_id: str
    worker_id: str
    status: Literal["completed", "failed", "needs_approval", "cancelled"]
    output: Mapping[str, Any] = field(default_factory=dict)   # 结构化输出（demo 里固定有 "text" 键）
    error: str | None = None                         # 失败原因
    remote_run_id: str | None = None                 # Worker 内部 runtime 的运行 ID（审计/恢复时反查用）


@dataclass(frozen=True)
class OrchestrationRequest:
    """编排入口：一次业务事务的全部信息。"""

    workflow_id: str                                 # 本次工作流的唯一 ID（如 "refund-001"）
    session_id: str                                  # 共享会话 ID（同一客户可发起多次工作流）
    goal: str                                        # 业务目标（自然语言）
    facts: Mapping[str, Any] = field(default_factory=dict)  # 业务事实（如订单号、金额）
    mode: OrchestrationMode = OrchestrationMode.LEADER_WORKER  # 期望的编排模式


@dataclass(frozen=True)
class OrchestrationPlan:
    """主管产出的执行计划：做什么（tasks）、怎么调度（mode）、怎么算完成（completion_rule）。"""

    plan_id: str
    mode: OrchestrationMode
    tasks: Sequence[WorkerTask]
    completion_rule: Literal["all", "any"] = "all"   # all=全部成功才算完成；any=任一成功即可
    round: int = 1                                   # 第几轮（反思循环模式下 >1）
    parent_plan_id: str | None = None                # 上一轮计划 ID（多轮时形成追溯链）


@dataclass(frozen=True)
class RoundRecord:
    """一轮执行的完整记录：反思循环的"观察"载体。

    每轮结束后，编排器把（轮次、计划、全部结果）打包存档，
    供反思器（ReflectionPolicy）阅读并决定下一步。
    """

    round: int
    plan: OrchestrationPlan
    results: Sequence[WorkerResult]


@dataclass(frozen=True)
class Reflection:
    """反思的产出：继续加任务（replan）、结束（done）还是放弃（failed）。"""

    decision: Literal["replan", "done", "failed"]
    reason: str                                      # 决策理由（写入日志/审计）
    new_tasks: Sequence[WorkerTask] = ()             # replan 时的增量任务（每轮的 task_id/幂等键必须全新）
    next_facts: Mapping[str, Any] = field(default_factory=dict)  # 新洞察：合并进下一轮 request.facts


@dataclass(frozen=True)
class OrchestrationResult:
    """编排终态：调用方能拿到的最终答案。"""

    workflow_id: str
    status: Literal["completed", "failed", "needs_approval"]
    summary: str                                     # 给人看的汇总文本
    worker_results: Sequence[WorkerResult]           # 全部任务回执（供机器审计回溯）
    rounds: Sequence[RoundRecord] = ()               # 多轮记录（单轮模式为空）


# ===========================================================================
# 编排层的三个可替换接口（Protocol）
# ===========================================================================


class LeaderPolicy(Protocol):
    """主管策略：只负责"做计划"，不负责执行。

    关键约束：create_plan 只能拿到 WorkerDescriptor（能力目录），
    拿不到 WorkerEndpoint 对象——主管在物理上无法绕过调度器直接调 Worker。
    计划可以由 LLM 生成，但生成后必须经确定性代码校验（白名单/预算/依赖无环）。
    """

    async def create_plan(
        self,
        request: OrchestrationRequest,
        workers: Sequence[WorkerDescriptor],
    ) -> OrchestrationPlan: ...


class ReflectionPolicy(Protocol):
    """反思策略：观察一轮执行结果，决定继续或收敛。

    与主管的关系：反思循环模式下，第一轮计划仍由主管生成，
    后续增量任务由反思器产出——反思是规划的延续，不是替代。
    """

    async def reflect(
        self,
        request: OrchestrationRequest,
        history: Sequence[RoundRecord],
        workers: Sequence[WorkerDescriptor],
    ) -> Reflection: ...


class WorkerEndpoint(Protocol):
    """编排器眼中的 Worker 调用边界（统一入口）。

    编排器只知道这个接口，不知道 Worker 用什么框架、在本地还是远程。
    descriptor 是公开属性（编排器要读它建目录）；execute 是唯一执行入口。
    """

    descriptor: WorkerDescriptor
    async def execute(self, task: WorkerTask, session_id: str) -> WorkerResult: ...


class A2ATransport(Protocol):
    """传输层接口：把协议载荷送到远端并拿回响应。

    只有一个方法、只处理"可序列化字典进、可序列化字典出"。
    认证、超时、重试、取消等网络语义全部封装在实现里（demo 用内存实现，
    生产换 HTTP/A2A SDK 实现），不泄漏给编排器和主管。
    """

    async def send_task(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
