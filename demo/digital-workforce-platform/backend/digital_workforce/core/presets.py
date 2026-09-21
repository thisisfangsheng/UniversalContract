from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerPreset:
    worker_id: str
    name: str
    description: str
    runtime_name: str
    capabilities: tuple[str, ...]
    skills: tuple[tuple[str, str, str], ...] = ()
    mcp_server_names: tuple[str, ...] = ()
    enabled: bool = True


WORKER_PRESETS = (
    WorkerPreset("timekeeper", "时序管理员", "提供当前时间与时区事实。", "offline-contract", ("time",)),
    WorkerPreset("scout", "情报调研员", "汇总调研事实并标注证据边界。", "offline-contract", ("research",), (("web-research-spec", "调研输出格式与证据要求", "inline"),)),
    WorkerPreset("analyst", "数据分析员", "汇总前序回执并输出分析结论。", "openai-compatible", ("analysis",), (("data-summary-spec", "数据分析与对比规范", "inline"),)),
    WorkerPreset("writer", "报告撰写员", "将事实写成结构化中文报告。", "openai-compatible", ("writing",), (("report-template", "中文报告章节模板", "inline"),)),
    WorkerPreset("reviewer", "质量审校员", "按审校清单输出通过或改进意见。", "openai-compatible", ("review",), (("review-checklist", "报告质量审校清单", "inline"),)),
    WorkerPreset("it-desk", "IT 服务台", "企业工单查询预留员工。", "mcp-openai-compatible", ("ticket",), enabled=False),
)


TEMPLATES: dict[str, dict[str, Any]] = {
    "sc-a-fact-brief": {"id": "sc-a-fact-brief", "name": "快速事实速览", "mode": "sequential", "worker_ids": ["timekeeper", "scout"], "facts": {}},
    "sc-b-competitor-scan": {"id": "sc-b-competitor-scan", "name": "并行竞品扫描", "mode": "concurrent", "worker_ids": ["scout", "analyst"], "facts": {}},
    "sc-c-iterative-report": {"id": "sc-c-iterative-report", "name": "迭代式调研报告", "mode": "react", "worker_ids": ["scout", "analyst", "writer", "reviewer"], "facts": {}, "react": {"engine": "builtin", "max_rounds": 3}},
    "sc-d-approval-release": {"id": "sc-d-approval-release", "name": "需审批的对外发布", "mode": "handoff", "worker_ids": ["writer", "reviewer"], "facts": {}},
}
