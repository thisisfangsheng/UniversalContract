"""A2A 适配层：把本地的单智能体运行器（Harness）变成可远程调用的"工作者"。

本文件包含协议的两端：
    A2AWorkerService   —— 服务端：收到协议请求，翻译后调用本地 Harness
    A2AWorkerEndpoint  —— 客户端：把任务单发出去，把响应翻译成回执
    InMemoryA2ATransport —— 传输层的内存实现（demo 用；生产换 HTTP 实现）

设计要点：网上传输的只有"可 JSON 序列化的字典"，契约对象（AgentRequest /
AgentResult）不出本地进程——协议与契约在此解耦。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .contracts import (
    A2ATransport, AgentRequest, Harness, RequestContext, WorkerDescriptor,
    WorkerEndpoint, WorkerResult, WorkerTask,
)


class A2AWorkerService:
    """Worker 服务端：一个服务实例只绑定一个 Harness，即"一个 Worker = 一个单智能体能力"。

    多智能体协作是编排层的事；Worker 内部永远是单智能体，这是硬边界。
    """

    def __init__(self, harness: Harness, worker_id: str) -> None:
        self._harness = harness          # 本服务背后真正干活的单智能体运行器
        self._worker_id = worker_id      # 本服务对外声明的身份（回执里自报家门，防冒名）

    @property
    def worker_id(self) -> str:
        """返回服务绑定的稳定 Worker 身份，供部署层授权使用。"""
        return self._worker_id

    async def handle(
        self,
        payload: Mapping[str, Any],
        request_context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """协议入口：可序列化字典进 -> 调用本地 Harness -> 可序列化字典出。"""

        # 必填字段用方括号取 + str() 强转：缺键直接报错（不该被半处理），
        # 网络来的值一律规整成字符串（协议边界防御）。
        task_id = str(payload["task_id"])
        session_id = str(payload["session_id"])

        # 协议载荷 -> 契约对象 AgentRequest：
        #   task_id     -> request_id   （协议叫 task，契约叫 request，此后贯通全链）
        #   session_id  -> session_id   （业务连续性主线，直通）
        #   instruction -> input        （任务指令文本）
        #   幂等键      -> metadata     （契约无独立字段，收进开放扩展槽）
        #   resume      -> resume       （人工审批后的恢复载荷；None = 全新请求）
        result = await self._harness.run(
            AgentRequest(
                request_id=task_id,
                session_id=session_id,
                input=str(payload["instruction"]),
                # input 是编排器构造的中立协作快照。不能在协议边界丢弃，
                # 否则任务事实、前序回执与跨 runtime 的共享状态无法抵达 Harness。
                metadata={
                    "a2a": True,
                    "idempotency_key": str(payload["idempotency_key"]),
                    "collaboration": dict(payload.get("input", {})),
                },
                resume=payload.get("resume"),
                request_context=request_context,
            )
        )

        # 契约对象 AgentResult -> 协议响应。三个细节：
        #   worker_id 用自身状态而非请求里的值——服务端自证身份；
        #   output 里 text 固定存在（None 归一为空串），结构化字段（data）并入同层；
        #   run_id 改名 remote_run_id——强调这是"远端"运行记录。
        return {
            "task_id": task_id,
            "worker_id": self._worker_id,
            "status": result.status,
            "output": {"text": result.output or "", **dict(result.data or {})},
            "error": result.error,
            "remote_run_id": result.run_id,
        }


class A2AWorkerEndpoint:
    """Worker 客户端：编排器侧的统一调用入口。

    编排器只知道 WorkerEndpoint 接口（descriptor + execute），
    不知道远端用什么框架、在本地还是远程。
    """

    def __init__(self, descriptor: WorkerDescriptor, transport: A2ATransport) -> None:
        self.descriptor = descriptor     # 公开属性（编排器要读它建能力目录）——注意无下划线
        self._transport = transport      # 传输层（纯内部件）

    async def execute(self, task: WorkerTask, session_id: str) -> WorkerResult:
        """执行一张任务单：校验归属 -> 组装载荷 -> 发送 -> 响应归一化为回执。"""

        # 归属校验（协议层最后防线）：正常流程中编排器按 worker_id 查目录
        # 才会拿到本 endpoint，不可能错；这里防御的是"调用方拿错对象"的编程错误。
        if task.worker_id != self.descriptor.worker_id:
            raise ValueError(f"任务指定 {task.worker_id}，但 endpoint 属于 {self.descriptor.worker_id}")

        # 任务单 -> 协议载荷（与 Service 端的反解严格镜像，两边共守同一份字段名）。
        # resume 仅在恢复场景携带（None 时省略，保持载荷最小）。
        response = await self._transport.send_task(
            self.descriptor.endpoint,
            {
                "task_id": task.task_id,
                "session_id": session_id,
                "instruction": task.instruction,
                "input": dict(task.input),        # 拷贝为普通 dict（task.input 可能是只读视图）
                "idempotency_key": task.idempotency_key,
                **({"resume": task.resume} if task.resume is not None else {}),
            },
        )

        # 响应 -> 回执 WorkerResult。纪律：必填键方括号取+强转（缺了就崩），
        # 可选键 .get() 带默认（容忍省略）。
        return WorkerResult(
            task_id=str(response["task_id"]),
            worker_id=str(response["worker_id"]),
            status=str(response["status"]),  # type: ignore[arg-type]  # 运行时信任服务端只返回合法值
            output=dict(response.get("output", {})),
            error=response.get("error"),
            remote_run_id=response.get("remote_run_id"),
        )


class InMemoryA2ATransport:
    """传输层的内存实现：用字典模拟"地址 -> 服务"的解析，保留 client/server 形状。

    生产环境换成 HTTP/A2A SDK 实现时，调用方（Endpoint）与被调方（Service）
    都不需要改——因为 send_task 的签名就是"地址 + 载荷 -> 载荷"。
    """

    def __init__(self, services: Mapping[str, A2AWorkerService]) -> None:
        self._services = services        # 字典就是"假 DNS"：地址字符串 -> 服务实例

    async def send_task(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return await self._services[endpoint].handle(payload)
        except KeyError as error:
            # 字典查不到键是实现细节错误，"未注册的地址"才是协议语义错误——
            # 升格为 ValueError，并保留原始异常链（from error）便于调试。
            raise ValueError(f"未注册的 A2A endpoint: {endpoint}") from error


class HttpA2ATransport:
    """通过 HTTP POST 调用远程 A2A Worker 服务。

    ``endpoint`` 是 Worker 的完整任务 URL。调用方可注入 ``httpx.AsyncClient``
    以复用连接或接入 ASGI 测试应用；未注入时本类自行创建和关闭客户端。
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_s: float = 30,
        headers_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._client = client
        self._timeout_s = timeout_s
        self._headers_provider = headers_provider

    async def send_task(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"HTTP A2A endpoint 必须是完整的 http(s) 地址: {endpoint!r}")
        headers = dict(self._headers_provider() if self._headers_provider else {})
        if self._client is not None:
            response = await self._client.post(
                endpoint, json=dict(payload), headers=headers, timeout=self._timeout_s
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(endpoint, json=dict(payload), headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"HTTP A2A endpoint 返回的载荷不是对象: {endpoint!r}")
        return data
