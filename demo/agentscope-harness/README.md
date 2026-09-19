# AgentScope Harness 多轮对话示例

本示例通过 UniversalContract 的 `ProviderHarness` 生命周期构造一个 AgentScope
`Agent`，并使用相同的 `session_id` 连续发送三次请求。第二、三次请求会收到
此前对话的契约上下文；AgentScope 自己的框架原生状态始终封装在运行时适配器内。

在仓库根目录的 `.env` 或 shell 中配置：

```bash
UNIVERSAL_CONTRACT_LLM_BASE_URL="https://your-llm-gateway/v1"
UNIVERSAL_CONTRACT_LLM_API_KEY="..."
UNIVERSAL_CONTRACT_LLM_MODEL="gpt-4o-mini"
```

在仓库根目录、且已按[用户安装手册](../../docs/06_用户安装手册.md)激活 Python 环境后运行：

```bash
python demo/agentscope-harness/demo.py
```