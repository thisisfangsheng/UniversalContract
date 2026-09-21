from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UC_ROOT = Path(__file__).resolve().parents[4]
if str(UC_ROOT) not in sys.path:
    sys.path.insert(0, str(UC_ROOT))

from common.contracts import LlmServingSpec


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_token: str | None
    default_tenant_id: str = "default"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    def llm_serving(self) -> LlmServingSpec | None:
        if not self.llm_api_key:
            return None
        return LlmServingSpec(model=self.llm_model, base_url=self.llm_base_url, api_key=self.llm_api_key)


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'digital_workforce.db'}"),
        api_token=os.getenv("API_TOKEN"),
        llm_base_url=os.getenv("UNIVERSAL_CONTRACT_LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=os.getenv("UNIVERSAL_CONTRACT_LLM_API_KEY", ""),
        llm_model=os.getenv("UNIVERSAL_CONTRACT_LLM_MODEL", "gpt-4o-mini"),
    )
