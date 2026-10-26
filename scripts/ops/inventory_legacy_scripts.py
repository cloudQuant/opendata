"""Inventory legacy ai-for-investor fetch scripts without importing them."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from opendata.migration_inventory import main  # noqa: E402  # required for direct script execution

if __name__ == "__main__":
    raise SystemExit(main())
