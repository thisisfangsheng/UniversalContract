"""离线演示 Harness 的 Skill L1 目录和 L2 降级注入。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from common.contracts import (
    AgentIdentity, AgentRequest, AgentResult, Capability, HarnessExecutionContext,
    HarnessProviders, HarnessSpec, SkillSpec,
)
from common.providers import InMemorySessionProvider
from common.skills import FilesystemSkillProvider, InlineSkillProvider, SkillRegistry
from harness import ProviderHarness


class ContextPrintingAgent:
    """最小离线 runtime：显示 Harness 传入的中立上下文，不调用模型。"""

    async def run(self, context: HarnessExecutionContext) -> AgentResult:
        for item in context.context_items:
            if item.label.startswith("contract.skills"):
                print(f"\n[{item.label}] priority={item.priority}\n{item.content}")
        return AgentResult("completed", "Skill 上下文已由 Harness 注入。", "offline-skill-run")

    def run_stream(self, context: HarnessExecutionContext):
        raise NotImplementedError


class OfflineRuntime:
    runtime_name = "offline-context-printer"

    def capabilities(self) -> frozenset[Capability]:
        # 不声明 SKILLS，因而由 ProviderHarness 采用 L2 降级正文注入。
        return frozenset({Capability.DURABLE_SESSION})

    async def build(self, spec: HarnessSpec, providers: HarnessProviders) -> ContextPrintingAgent:
        return ContextPrintingAgent()

    async def close(self) -> None:
        return None


async def main() -> None:
    skill_root = Path(__file__).with_name("skills") / "refund-policy"
    specs = (
        SkillSpec("refund-policy", "path", path=str(skill_root)),
        SkillSpec(
            "invoice-check",
            "inline",
            content="---\ndescription: 发票字段校验\nallowed-tools: [invoice_lookup]\n---\n核验发票号码、抬头和税额；缺失字段时返回待补充项。\n",
        ),
    )
    registry = SkillRegistry((FilesystemSkillProvider(specs), InlineSkillProvider(specs)))
    harness = ProviderHarness(
        HarnessSpec(AgentIdentity("skill-demo", "Skill Demo"), "按技能处理任务。", skills=specs),
        HarnessProviders(session=InMemorySessionProvider(), skills=registry),
        OfflineRuntime(),
    )
    problems = await harness.validate()
    if problems:
        raise RuntimeError(f"Skill 声明无效: {problems}")
    result = await harness.run(AgentRequest("skill-demo-1", "skill-demo-session", "查询退款条件"))
    print(f"\nstatus={result.status}; output={result.output}")


if __name__ == "__main__":
    asyncio.run(main())