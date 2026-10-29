"""Tiny import-time probe used by the isolated process executor tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PROBE_ENV = "OD_TEST_PROCESS_EXECUTOR_BOOTSTRAP_IMPORT"
_MARKER_ENV = "OD_TEST_PROCESS_EXECUTOR_BOOTSTRAP_MARKER"
_SECRET_TEXT = "bootstrap-secret-do-not-leak"

if os.environ.get(_PROBE_ENV) == "emit-and-raise":
    marker = os.environ.get(_MARKER_ENV)
    if marker is not None:
        marker_path = Path(marker).resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root not in marker_path.parents:
            raise RuntimeError("test marker must stay inside the temporary directory")
        marker_path.write_text("imported", encoding="utf-8")
    print(_SECRET_TEXT, file=sys.stderr)
    raise RuntimeError(_SECRET_TEXT)


def handle(parameters: dict[str, object]) -> dict[str, object]:
    """Provide a harmless top-level function for the worker import test."""
    return {"value": parameters.get("value")}
