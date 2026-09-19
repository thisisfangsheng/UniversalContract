"""Opt-in wall-clock timing for external workflow dependencies."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from collections.abc import Iterator


@contextmanager
def timing_span(name: str) -> Iterator[None]:
    """Print a span duration when UNIVERSAL_CONTRACT_TIMING is enabled."""
    if os.getenv("UNIVERSAL_CONTRACT_TIMING") != "1":
        yield
        return
    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1_000
        print(f"[timing] {name} {elapsed_ms:.0f}ms", flush=True)