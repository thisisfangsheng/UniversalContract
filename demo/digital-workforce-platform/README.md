# Digital Workforce Platform

这是一个可运行的企业数字员工协作平台示例。它把 UniversalContract 的
`HarnessSpec`、`ProviderHarness`、A2A Worker、`SharedSession` 与编排工厂
组合为一个 FastAPI 后端和 React 管理界面。

## 目录

```text
digital-workforce-platform/
├── backend/
│   ├── digital_workforce/  # FastAPI API、UC 适配、编排与 SQLite 持久化
│   ├── tests/              # 离线端到端验证
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               # Vite + React 控制台
├── .env.example            # 仅变量名与安全默认值
├── docker-compose.yml
└── README.md
```

## 示例覆盖的能力

- 员工目录：内置或动态注册 Worker，选择 OpenAI、AgentScope、LangGraph 等 runtime。
- MCP 治理：为每个 Worker 显式绑定 MCP Server，并在保存时验证 runtime、传输与工具白名单兼容性。
- 协作模式：顺序、并行、接力审批及 REACT 编排。
- REACT：没有模型时执行“首轮协作 -> 修订整合”的确定性回环；配置模型后使用 UC Magentic manager，根据 Worker 能力和回执动态选人、重规划并收敛。
- 会话连续性：`SQLiteSessionProvider` 实现 UC `SessionProvider` 协议；`SharedSession` 按租户和 session ID 保存。历史任务“继续处理”会新建任务记录但复用 session。
- 运行审计：SQLite 保存任务、结果快照、事件、审批与会话；前端经 SSE 展示执行轨迹。

前后端分层、UC 抽象映射、持久化边界及常见场景的带编号时序图见
[架构设计.md](架构设计.md)。可单独渲染的 Mermaid 图源位于 [architecture/](architecture/)。

## 安装

### 额外依赖

除 UniversalContract 根目录 `requirements.txt` 之外，本示例还需要：

| 范围 | 依赖 | 用途 |
|---|---|---|
| Python | `uvicorn[standard]` | FastAPI ASGI 服务进程 |
| Python | `SQLAlchemy` | 平台任务、员工、审计记录的 ORM |
| Python | `aiosqlite` | SQLite 的异步数据库驱动 |
| 前端 | Node.js 20+、npm | 安装并运行 React/Vite 控制台 |

Python 额外依赖定义在 `backend/requirements.txt`；前端精确依赖版本锁定在
`frontend/package-lock.json`，其中包含 React、Ant Design、React Router、Vite 与 TypeScript。

从 UniversalContract 仓库根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r demo/digital-workforce-platform/backend/requirements.txt
cd demo/digital-workforce-platform/frontend
npm ci
```

若要让 Worker 调用真实模型，将 `.env.example` 复制为 `.env`，并填写
`UNIVERSAL_CONTRACT_LLM_API_KEY`。不配置模型时，平台仍可运行离线流程，但 Worker 会明确说明不能生成真实回答。

## 启动

终端一启动后端：

```bash
cd demo/digital-workforce-platform
set -a; source .env 2>/dev/null || true; set +a
PYTHONPATH=backend uvicorn digital_workforce.main:app --host 0.0.0.0 --reload --port 8000
```

终端二启动前端：

```bash
cd demo/digital-workforce-platform/frontend
npm run dev
```

Vite 监听全部网卡，因此可使用 `http://127.0.0.1:5173`、`http://localhost:5173` 或
`http://<服务器IP>:5173` 访问。前端始终请求相对路径 `/api`；Vite 在开发时将其代理至本机
`http://127.0.0.1:8000`，因此无需在前端写死服务器 IP，也不会产生浏览器跨域请求。

也可容器化启动：

```bash
cd demo/digital-workforce-platform
cp .env.example .env
docker compose up --build
```

容器前端同样使用相对 `/api`，由 Nginx 转发到 Compose 内的 `backend:8000`；可使用
`http://127.0.0.1:8080`、`http://localhost:8080` 或 `http://<服务器IP>:8080` 访问。
请在服务器防火墙或安全组放行实际使用的前端端口（开发模式 `5173`，容器模式 `8080`）。

## 验证

```bash
cd demo/digital-workforce-platform
PYTHONPATH=backend python backend/tests/test_offline.py
cd frontend && npm run build
```

离线集成测试覆盖员工和 MCP 管理、顺序/并行/REACT 编排、审批恢复、SQLite 会话持久化及历史任务续办。