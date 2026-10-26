"""Run a bounded read-only inventory of project-related host jobs."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from opendata.host_inventory import main  # noqa: E402  # direct script entrypoint

if __name__ == "__main__":
    raise SystemExit(main())
