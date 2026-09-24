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
- 身份与租户：可选 JWT 登录 profile。账户与租户通过 membership 关联；动态 Worker、Skill、MCP Server、任务和会话按租户隔离。

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

### 用户认证与多租户模式

平台有两个认证 profile：

| Profile | 行为 | 使用场景 |
|---|---|---|
| `local` | 显式无认证；所有请求使用开发身份和 `default` tenant | 离线演示、调试既有功能 |
| `token` | 用户名/密码登录；JWT + membership 校验当前租户 | 体验多用户和租户隔离 |

默认 `AUTH_PROFILE=local`，控制台不会要求登录。要启用 `token`，将 `.env.example` 复制为
本地 `.env`，并设置：

```bash
AUTH_PROFILE=token
JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

`JWT_SECRET` 是后端签发和验证 JWT 的私有签名密钥，不代表用户或租户，也绝不能发送给前端。
本地临时生成即可；每次更换它，既有 token 都会失效。生产环境必须由 Secret Manager、Vault、KMS
或部署平台注入稳定的随机密钥，至少 32 字节，且所有后端实例使用同一值。

#### 从用户视角使用

1. 以 `token` profile 启动后端，在控制台打开“身份与租户”。
2. 首次使用选择“注册账户并创建租户”，填写用户名、至少 12 位的密码、显示名称、租户 ID 和名称。
	注册会创建用户、租户以及该用户的 `owner` membership，并自动登录。
3. 创建动态员工、登记 MCP Server、维护 Skill、发起任务；它们全部保存到当前租户。
4. 在“身份与租户”下拉框选择已加入的其他租户即可切换工作空间。浏览器会携带 Bearer token 和
	当前 `X-Tenant`；后端每次都重新验证 membership。
5. 退出登录会吊销当前 access token。再次登录时平台返回默认 membership tenant，之前在该租户创建
	的动态员工、MCP、Skill 和任务仍然可见。

owner 可将已经注册的用户加入当前租户：

```bash
curl -X POST http://127.0.0.1:8000/api/tenants/<tenant_id>/memberships \
  -H "Authorization: Bearer <owner_access_token>" \
  -H "Content-Type: application/json" \
  -d '{"username":"bob","role":"member"}'
```

角色可为 `owner`、`member`、`viewer`。当前参考实现已强制跨租户隔离：无凭据或无效/过期/吊销
token 返回 `401`，请求未加入的 tenant 返回 `403`。同一租户内按资源创建者控制读写的 ACL 尚未实现，
因此不应将角色误解为完整的资源级授权策略。

## 启动

终端一启动后端：

```bash
cd demo/digital-workforce-platform
set -a; source .env 2>/dev/null || true; set +a
PYTHONPATH=backend:../.. /home/sean/.miniforge3/envs/uc/bin/python -m uvicorn digital_workforce.main:app --host 0.0.0.0 --reload --port 8000
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
PYTHONPATH=backend:../.. /home/sean/.miniforge3/envs/uc/bin/python backend/tests/test_offline.py
cd frontend && npm run build
```

离线集成测试覆盖员工和 MCP 管理、顺序/并行/REACT 编排、审批恢复、SQLite 会话持久化、历史任务
续办，以及 token profile 下的注册、跨租户拒绝、退出吊销与重新登录后资源可见性。