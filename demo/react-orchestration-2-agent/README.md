# Magentic REACT 双 MCP Worker 示例

这个示例通过 Magentic 的 REACT 编排器协调两个遵守 Universal Contract 的 Harness agent：

- **AgentScope 高德 worker**：由 `AgentScopeMcpRuntimeAdapter` 把中立的高德
  `McpServerSpec` 翻译为 AgentScope `MCPClient` 与 `Toolkit`。它只能查询上海 POI
  和未来一周天气，并必须选出一个具体出行日期。
- **LangGraph Maqami worker**：由 `LangGraphMcpRuntimeAdapter` 把匿名 Maqami
  `McpServerSpec` 翻译为 `MultiServerMCPClient`、LangChain tools 与 LangGraph ReAct graph。
  它只能从前序协作上下文取得日期，并用 Maqami `post_flights_rates` 查询当天单程北京到上海航班报价。

本示例的两个任务具有固定数据依赖，因此使用确定性 Magentic manager 严格按“高德 -> 航班报价”
顺序调度，避免为已知流程增加 manager LLM 重规划延迟。manager 只读取两张 worker 的 agent card
（身份、能力、详细职责边界），不能直接调用 MCP。worker 间的结果通过 A2A 协议回执与中立协作快照传递；两个
Harness 还共享同一个 `SharedSession` 存储，且不会交换 AgentScope 或 LangGraph 的私有对象。
本版本使用 `InMemoryA2ATransport`，不创建 FastAPI 路由或 HTTP 接口。

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
python demo/react-orchestration-2-agent/demo.py
```

也可以传入不同的复杂意图：

```bash
python demo/react-orchestration-2-agent/demo.py "找上海适合周末游玩的地点和天气较好的日期，再查当天北京到上海单程航班"
```

输出会包含 Magentic workflow 状态、两个 worker 的汇总文本以及已执行的 worker 回执数量和轮次。

需要定位外部服务耗时时，可开启阶段计时；输出仅包含耗时，不会记录提示词、密钥或工具返回内容：

```bash
UNIVERSAL_CONTRACT_TIMING=1 python demo/react-orchestration-2-agent/demo.py
```