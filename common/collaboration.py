"""多智能体协作的数据面：构造可跨编排器、协议和 runtime 传递的共享输入包。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import WorkerResult, WorkerTask


# A2A payload、AgentRequest.metadata 和各 runtime adapter 共用的稳定键名。
COLLABORATION_METADATA_KEY = "collaboration"


def build_collaboration_input(
    task: WorkerTask,
    workflow_facts: Mapping[str, Any],
    completed_results: Sequence[WorkerResult] = (),
) -> dict[str, Any]:
    """生成一个 Worker 可消费的共享状态快照。

    不将 runtime 私有对象写入快照；所有字段必须能被 A2A JSON 协议传输。
    ``prior_results`` 让顺序协作能读取已经完成的工作，
    ``dependency_results`` 则标明其中哪些是当前任务显式依赖的上游产物。
    并行任务共享同一份 workflow_facts，但开始后不会观察彼此的中间结果。
    """

    prior_results = {
        result.task_id: {
            "worker_id": result.worker_id,
            "status": result.status,
            "output": dict(result.output),
            "error": result.error,
            "remote_run_id": result.remote_run_id,
        }
        for result in completed_results
    }
    return {
        "workflow_facts": dict(workflow_facts),
        "task_input": dict(task.input),
        "prior_results": prior_results,
        "dependency_results": {
            task_id: prior_results[task_id]
            for task_id in task.depends_on
            if task_id in prior_results
        },
    }