# 配置驱动的 FastAPI HTTP A2A Magentic REACT 双 MCP Worker 示例

这个示例演示如何通过 `usecase.yaml` 选择两个已注册的 Harness agent 模板，并由
Magentic REACT 编排器把它们组织为一个完整 use case。使用者修改 YAML 中的目标、事实、
Worker、MCP 工具白名单与编排限制，无需编写 `ProviderHarness`、A2A endpoint 或 workflow
装配代码。

`usecase_runtime.py` 是这个 demo 的小型控制面：它从 `AGENT_CATALOG` 选择已知模板、根据
配置实例化 Harness，并用 FastAPI 路由将本地 Worker 暴露为 HTTP A2A 服务。编排器实际使用
`HttpA2ATransport` 调用这些 HTTP endpoint，因此将本地 `http://local-a2a/...` 地址替换为
已部署的远程 A2A Worker 地址时，不需要修改 orchestration。

当前 catalog 提供两个已知模板：

- **AgentScope 高德 worker**：由 `AgentScopeMcpRuntimeAdapter` 把中立的高德
  `McpServerSpec` 翻译为 AgentScope `MCPClient` 与 `Toolkit`。它只能查询上海 POI
  和未来一周天气，并必须选出一个具体出行日期。
- **LangGraph Maqami worker**：由 `LangGraphMcpRuntimeAdapter` 把匿名 Maqami
  `McpServerSpec` 翻译为 `MultiServerMCPClient`、LangChain tools 与 LangGraph ReAct graph。
  它只能从前序协作上下文取得日期，并用 Maqami `post_flights_rates` 查询当天单程北京到上海航班报价。

本示例的两个任务具有固定数据依赖，因此配置选择确定性 Magentic manager 并严格按“高德 -> 航班报价”
顺序调度，避免为已知流程增加 manager LLM 重规划延迟。manager 只读取两张 worker 的 agent card
（身份、能力、详细职责边界），不能直接调用 MCP。worker 间的结果通过 HTTP A2A 协议回执与中立协作快照传递；两个
Harness 共享同一个 `SharedSession` 存储，且不会交换 AgentScope 或 LangGraph 的私有对象。

## HTTP A2A 接口

每个配置的 worker 都会先按 catalog 实例化为 `ProviderHarness`，再由
`A2AWorkerService(harness, worker_id)` 统一为 A2A 服务。`_a2a_app()` 为每个
`endpoint_path` 注册一个 FastAPI `POST` 路由；路由把 JSON payload 交给
`A2AWorkerService.handle()`，后者负责 `WorkerTask`、`AgentRequest`、`AgentResult`
和 `WorkerResult` 的协议转换。

编排侧为同一路径建立 `WorkerDescriptor` 与 `A2AWorkerEndpoint`，并使用
`HttpA2ATransport` 发出 JSON HTTP 请求。调用链如下：

```text
Magentic workflow -> A2AWorkerEndpoint -> HttpA2ATransport
  -> POST /workers/{worker_id}/tasks -> FastAPI route
  -> A2AWorkerService -> ProviderHarness -> framework runtime
  -> WorkerResult JSON
```

默认运行方式使用 `httpx.ASGITransport` 将请求直接送进进程内的 FastAPI app：它完整行使
HTTP 方法、JSON 负载、状态码和协议转换，但不启动 Uvicorn 或监听 TCP 端口。部署成独立
worker 服务时，使用 Uvicorn 承载同一个 FastAPI app，并把 `WorkerDescriptor.endpoint`
改为实际的 HTTP(S) 地址；编排器和 `HttpA2ATransport` 均无需改动。

本 demo 显式使用本地 `auth_mode=none` profile。生产服务应使用 `deployment.create_a2a_app()`
配合企业 `Authenticator`、`Authorizer` 与共享 `SessionProvider`；认证、API Gateway、mTLS 和
多租户隔离要求见 [部署与认证指南](../../docs/07_部署与认证指南.md)。

## Use case 配置

默认配置在 [usecase.yaml](usecase.yaml)。其中每个 `workers` 项只引用模板名，例如：

```yaml
workers:
  - worker_id: agentscope-amap-trip-worker
    template: amap-trip
    endpoint_path: /workers/agentscope-amap-trip-worker/tasks
    mcp:
      endpoint_env: AMAP_MCP_ENDPOINT
      allowed_tools: [maps_text_search, maps_weather]
```

`template` 必须是运行时已注册的名称。该模板决定 runtime、指令和能力边界；配置只允许调整
实例标识、A2A 路径、MCP endpoint、工具白名单与超时。这样能在保持已审核 agent 边界的同时，
让使用者按配置组合它们。

## 配置

在仓库根目录 `.env` 或 shell 中设置：

```bash
UNIVERSAL_CONTRACT_LLM_BASE_URL="https://your-llm-gateway/v1"
UNIVERSAL_CONTRACT_LLM_API_KEY="..."
UNIVERSAL_CONTRACT_LLM_MODEL="gpt-4o-mini"
AMAP_MCP_ENDPOINT="https://mcp.amap.com/..."
```

Maqami 使用公开的匿名 endpoint `https://mcp.maqami.co/`，本示例不读取任何 Maqami 密钥。
为避免执行有副作用的动作，MCP 白名单仅允许读取航班报价的 `post_flights_rates`，不暴露预订、取消
或支付工具。

## 运行

在仓库根目录、且已按[用户安装手册](../../docs/06_用户安装手册.md)创建并激活 Python 环境后运行：

```bash
python demo/react-orchestration-2-agent-fastapi/demo.py
```

可使用另一份 YAML 配置：

```bash
python demo/react-orchestration-2-agent-fastapi/demo.py \
  --config path/to/usecase.yaml
```

也可以传入不同的复杂意图：

```bash
python demo/react-orchestration-2-agent-fastapi/demo.py "找上海适合周末游玩的地点和天气较好的日期，再查当天北京到上海单程航班"
```

输出会包含 Magentic workflow 状态、两个 worker 的汇总文本以及已执行的 worker 回执数量和轮次。

需要定位外部服务耗时时，可开启阶段计时；输出仅包含耗时，不会记录提示词、密钥或工具返回内容：

```bash
UNIVERSAL_CONTRACT_TIMING=1 python demo/react-orchestration-2-agent-fastapi/demo.py
```