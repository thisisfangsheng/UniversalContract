# 受保护 REST A2A Worker 示例

这个新增示例复用 [deployment/fastapi_a2a.py](../../deployment/fastapi_a2a.py) 提供的当前 REST 面：
`POST /workers/echo/tasks`。它不修改现有编排 demo，也不假设尚未实现的
`/api/workflows` 门面已经存在。

示例包含：

- `app.py`：以 `StaticBearerAuthenticator` 和 `ScopeAuthorizer` 保护一个离线 Echo Worker；
- `demo.py`：用 `httpx.ASGITransport` 发起真实 HTTP 语义调用，无需启动监听端口；
- 认证失败返回 `401`，有效 token 返回 `200`；
- `session_id=customer-42` 在存储层会被重写为 `demo-tenant:customer-42`，展示 tenant 隔离。

离线运行：

```bash
python demo/rest-a2a-worker/demo.py
```

如需作为独立服务运行，安装 Uvicorn 后从仓库根目录执行：

```bash
uvicorn app:app --app-dir demo/rest-a2a-worker --port 8000
```

然后向 `http://127.0.0.1:8000/workers/echo/tasks` 发起 `POST`，并携带
`Authorization: Bearer demo-token`。`demo-token` 仅限本地示例，生产环境应替换为企业认证器。