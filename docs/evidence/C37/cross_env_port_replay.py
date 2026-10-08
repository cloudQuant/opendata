#!/usr/bin/env python
"""Replay the ported financial_statement fetcher in whichever stack runs it.

C37 uses it to falsify (or confirm) "CI installed a different pandas, so its
string->float kernel printed ...41". Run it twice with the same interpreter
choice as the transcript header says: the local env and a throwaway venv that
pins the versions CI resolved to. Offline only - HTTP is replayed from the
recorded transcript, so no request leaves the machine.
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "upstream" / "financial_statement"
MODULE_PATH = REPO / "opendata_http" / "stock_fundamental" / "stock_finance_sina.py"

# (column, row) pairs CI reported red, plus the text CI printed for the port cell.
CI_CELLS: list[tuple[tuple[str, int], str]] = [
    (("流动资产合计", 11), "218472857114.41"),
    (("资产总计", 10), "272699660092.25"),
    (("资产总计", 19), "224702253941.13"),
    (("未分配利润", 7), "192903581645.13"),
    (("归属于母公司股东权益合计", 5), "258357842676.41"),
    (("归属于母公司股东权益合计", 17), "206782990983.25"),
]


class RecordedResponse:
    """One replayed HTTP response shaped like the piece of requests we use."""

    def __init__(self, entry: dict[str, Any]) -> None:
        """Bind one transcript entry."""
        self._entry = entry

    @property
    def content(self) -> bytes:
        """Return the recorded body bytes."""
        raw: bytes = base64.b64decode(self._entry["content_b64"])
        return raw

    @property
    def encoding(self) -> str:
        """Return the recorded encoding."""
        encoding: str = self._entry.get("encoding") or "utf-8"
        return encoding

    @property
    def text(self) -> str:
        """Return the recorded body as text."""
        return self.content.decode(self.encoding)

    def json(self) -> Any:  # noqa: ANN401  # untyped json boundary
        """Parse the recorded body the way requests would."""
        parsed: Any = json.loads(self.text)
        return parsed

    def raise_for_status(self) -> None:
        """No-op: the transcript recorded a 2xx response."""


def load_fetcher() -> Any:  # noqa: ANN401  # untyped pandas callable
    """Import the ported fetcher without running the package ``__init__``.

    ``opendata_http/__init__.py`` pulls the whole provider tree (and with it
    optional dependencies a probe venv does not carry), so the package modules
    are stubbed with their real ``__path__`` and the single module is loaded by
    file. The code under test is byte-identical to what the gate imports.
    """
    for name, path in (
        ("opendata_http", REPO / "opendata_http"),
        ("opendata_http.stock_fundamental", REPO / "opendata_http" / "stock_fundamental"),
        ("opendata_http.utils", REPO / "opendata_http" / "utils"),
    ):
        stub = types.ModuleType(name)
        stub.__path__ = [str(path)]
        sys.modules[name] = stub
    spec = importlib.util.spec_from_file_location(
        "opendata_http.stock_fundamental.stock_finance_sina", MODULE_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - evidence only
        message = f"cannot load {MODULE_PATH}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fetcher: Any = module.stock_financial_report_sina
    return fetcher


def main() -> int:
    """Replay the fetcher and print how each CI-reported cell renders."""
    print(f"python : {sys.version.splitlines()[0]}")
    print(f"pandas : {pd.__version__}")
    print(f"numpy  : {np.__version__}")
    print(f"platform: {sys.platform}")
    import requests.compat as compat

    json_module = cast("ModuleType", compat.__dict__["json"])
    print(f"requests .json() 用的 json 模块: {json_module.__name__}")

    with gzip.open(FIXTURE / "responses.json.gz") as handle:
        entries: list[dict[str, Any]] = json.loads(handle.read())
    cursor = iter(entries)
    requests.get = cast("Any", lambda *a, **k: RecordedResponse(next(cursor)))

    fetcher = load_fetcher()
    frame = fetcher(stock="sh600519", symbol="资产负债表")
    print(f"\n搬运层返回 {frame.shape[0]} 行 x {frame.shape[1]} 列")
    reproduced = 0
    for (column, index), printed in CI_CELLS:
        value = frame[column].tolist()[index]
        rendered = str(value)
        hit = rendered == printed
        reproduced += int(hit)
        print(
            f"  {column}[{index}] dtype={frame[column].dtype} 值类型={type(value).__name__}"
            f"\n      str() -> {rendered!r}   CI 当年印的 -> {printed!r}"
            f"   {'复现 CI 右值' if hit else '未复现（仍是录制值）'}"
        )
    print(f"\n=> 本栈复现 CI 右值 {reproduced} / {len(CI_CELLS)} 例")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
