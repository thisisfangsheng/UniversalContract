"""Platform orchestration services."""

import sys
from pathlib import Path

UC_ROOT = Path(__file__).resolve().parents[5]
if str(UC_ROOT) not in sys.path:
	sys.path.insert(0, str(UC_ROOT))
