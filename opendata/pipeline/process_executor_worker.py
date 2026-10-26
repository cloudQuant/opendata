"""Fixed subprocess entry point for the reviewed process executor."""

from __future__ import annotations

import socket
import sys
from pathlib import Path


def main() -> int:
    """Load the local package and hand one inherited socket to its worker."""
    if len(sys.argv) != 2:
        return 2
    try:
        descriptor = int(sys.argv[1])
        if descriptor < 0:
            return 2
        channel = socket.socket(fileno=descriptor)
    except (OSError, ValueError):
        return 2

    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    try:
        from opendata.pipeline.process_executor import _worker_bootstrap

        _worker_bootstrap(channel)
    except BaseException:
        channel.close()
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
