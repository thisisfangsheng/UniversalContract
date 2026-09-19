# AgentScope Harness 高德 MCP 示例

本示例用 UniversalContract 的 `ProviderHarness` 执行 AgentScope `Agent`，并由
`AgentScopeMcpRuntimeAdapter` 将 `HarnessSpec.tools` 中的中立高德 MCP 声明转换
为 AgentScope 的 `MCPClient`、`Toolkit` 和原生工具调用循环。demo 本身不直接
连接或调用 MCP；MCP 的加载与执行都封装在具体框架 adapter 中。

在仓库根目录的 `.env` 或 shell 配置：

```bash
UNIVERSAL_CONTRACT_LLM_BASE_URL="https://your-llm-gateway/v1"
UNIVERSAL_CONTRACT_LLM_API_KEY="..."
UNIVERSAL_CONTRACT_LLM_MODEL="gpt-4o-mini"
AMAP_MCP_ENDPOINT="https://mcp.amap.com/..."
```

高德 endpoint 可能含有访问凭据，应仅保存在本地环境变量或 `.env`，不要提交到仓库。

在仓库根目录、且已按[用户安装手册](../../docs/06_用户安装手册.md)激活 Python 环境后运行：

```bash
python demo/agentscope-harness-mcp/demo.py
```

脚本会在同一个 `session_id` 中依次查询长春桥位置、到佳程广场的公共交通路线，
以及佳程广场附近的猪蹄餐馆。它只向模型暴露 `maps_geo`、
`maps_direction_transit_integrated`、`maps_around_search` 三项高德能力。