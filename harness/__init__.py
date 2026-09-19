"""框架无关的 Harness 生命周期，以及具体框架的运行时适配器。"""

from .provider import ProviderHarness, as_harness

__all__ = ["ProviderHarness", "as_harness"]