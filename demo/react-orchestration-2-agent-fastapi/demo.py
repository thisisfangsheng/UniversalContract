"""通过 usecase.yaml 选择已注册 agent 模板，组装并运行 REACT 用例。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from usecase_runtime import build_use_case, load_environment, load_use_case, serving_from_environment


async def main(config_path: Path, goal: str | None) -> None:
    load_environment(REPOSITORY_ROOT)
    config = load_use_case(config_path)
    use_case = build_use_case(config, serving_from_environment())
    try:
        result = await use_case.run(goal)
        print(f"workflow={result.workflow_id} status={result.status}")
        print(result.summary)
        print(f"worker_results={len(result.worker_results)} rounds={len(result.rounds)}")
    finally:
        await use_case.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过 YAML 配置运行 Magentic REACT 用例")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("usecase.yaml"),
        help="use case YAML 配置路径。",
    )
    parser.add_argument("goal", nargs="?", help="可选：覆盖 YAML 中的默认复杂意图。")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.config, arguments.goal))
