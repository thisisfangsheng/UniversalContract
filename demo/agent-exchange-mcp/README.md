# AgentScope 与 LangGraph Runtime 交换 MCP 示例

本示例构造两个遵守同一份 Universal Contract `HarnessSpec` 的地图 agent：

- AgentScope runtime 使用 `AgentScopeMcpRuntimeAdapter` 将 MCP 声明转为
  `MCPClient` 与 `Toolkit`。
- LangGraph runtime 使用 `LangGraphMcpRuntimeAdapter` 将 MCP 声明转为
  `MultiServerMCPClient`、LangChain tools 与 LangGraph ReAct graph。

两个 `ProviderHarness` 共享完全相同的 `HarnessSpec`、`HarnessProviders` 和
`common.providers.InMemorySessionProvider`。每轮结束后，`ProviderHarness` 会把用户输入和 agent 输出写入
框架无关的 `SharedSession.messages`；下一次调用无论切换到哪个 runtime，都会通过
`contract.session.messages` 上下文读取完整历史。

对话按以下顺序运行：

1. AgentScope 查询出发地长春桥。
2. AgentScope 查询目的地佳程广场。
3. LangGraph 根据前两轮记录查询公共交通。
4. LangGraph 根据目的地查询附近猪蹄餐馆。
5. AgentScope 回顾包含 LangGraph 输出在内的完整会话。

在仓库根目录的 `.env` 或 shell 配置：

```bash
UNIVERSAL_CONTRACT_LLM_BASE_URL="https://your-llm-gateway/v1"
UNIVERSAL_CONTRACT_LLM_API_KEY="..."
UNIVERSAL_CONTRACT_LLM_MODEL="gpt-4o-mini"
AMAP_MCP_ENDPOINT="https://mcp.amap.com/..."
```

`AMAP_MCP_ENDPOINT` 可能包含访问凭据，只应保存在本地环境变量或 `.env`，不要提交。

在已按[用户安装手册](../../docs/06_用户安装手册.md)创建并激活的 Python 环境中运行：

```bash
python demo/agent-exchange-mcp/demo.py
```

终端中每轮的 `[contract] shared_session_messages` 会依次增长为 `2`、`4`、`6`、`8`、`10`。
这项计数与 runtime 无关，是 Harness contract 管理的共享上下文连续性的直接证据。