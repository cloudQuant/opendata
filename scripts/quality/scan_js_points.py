#!/usr/bin/env python3
"""JS execution point registry for the ported tree (A2.6).

The ported ``opendata_http`` tree executes JavaScript in two ways
that must stay visible across upstream re-syncs:

* ``py_mini_racer.MiniRacer()`` engine sites: the executed payload is
  a hardcoded literal (``hk_js_decode`` in ``stock/cons.py``) and the
  remote response is only passed in as data - no remote code is
  evaluated.
* builtin ``eval(...)`` sites: sina HK/US/b-sina/kcb/summary pages
  embed factor dictionaries as ``var x = {...};`` assignments that
  upstream parses with ``eval`` on the *response text*; this is the
  bandit B307 class and the one place where remote text reaches an
  evaluator.

The registry is a frozen per-file inventory: ``--check`` fails on any
addition or removal so a re-sync cannot silently grow the JS surface.
Server-side execution is out of scope here (nothing in this tree
executes JS server-side beyond the in-process engine).

Usage:
    python scripts/quality/scan_js_points.py --check
    python scripts/quality/scan_js_points.py --update
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = REPO_ROOT / "opendata_http"
REGISTRY_PATH = REPO_ROOT / "docs" / "quality" / "js-execution-points.json"
REGISTRY_VERSION = 1

#: Engine instantiations; catches every ``MiniRacer()`` call.
_ENGINE_PATTERN = re.compile(r"MiniRacer\(\)")
#: Builtin ``eval`` calls only - method calls such as ``js_code.eval``
#: are the engine API and are counted as engine sites instead.
_BUILTIN_EVAL_PATTERN = re.compile(r"(?<![\w.])eval\(")
_BUILTIN_EXEC_PATTERN = re.compile(r"(?<![\w.])exec\(")

SCOPE_CHANGED = "scan scope changed silently"


def scan() -> dict[str, Any]:
    """Scan the ported tree for JS execution points.

    Returns:
        Registry payload: engine and eval inventories plus totals.
    """
    engine: Counter[str] = Counter()
    builtin_eval: Counter[str] = Counter()
    builtin_exec: Counter[str] = Counter()
    for path in sorted(TARGET.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        for pattern, counter in (
            (_ENGINE_PATTERN, engine),
            (_BUILTIN_EVAL_PATTERN, builtin_eval),
            (_BUILTIN_EXEC_PATTERN, builtin_exec),
        ):
            count = len(pattern.findall(text))
            if count:
                counter[relative] = count
    return {
        "version": REGISTRY_VERSION,
        "engine": "py_mini_racer",
        "js_payload_source": "opendata_http/stock/cons.py:hk_js_decode (hardcoded literal)",
        "engine_sites": dict(sorted(engine.items())),
        "builtin_eval_sites": dict(sorted(builtin_eval.items())),
        "builtin_exec_sites": dict(sorted(builtin_exec.items())),
        "totals": {
            "engine_sites": sum(engine.values()),
            "builtin_eval_sites": sum(builtin_eval.values()),
            "builtin_exec_sites": sum(builtin_exec.values()),
            "files": len(set(engine) | set(builtin_eval) | set(builtin_exec)),
        },
    }


def _load_registry() -> dict[str, Any] | None:
    try:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def check() -> int:
    """Compare the live scan against the frozen registry.

    Returns:
        Process exit code (0 when in sync).
    """
    registry = _load_registry()
    if registry is None:
        print(f"FAIL: registry missing or unreadable ({REGISTRY_PATH}); run --update")
        return 1
    current = scan()
    drift = False
    for key in ("engine_sites", "builtin_eval_sites", "builtin_exec_sites"):
        recorded, live = registry.get(key, {}), current[key]
        for name in sorted(set(recorded) | set(live)):
            if recorded.get(name) != live.get(name):
                print(f"{SCOPE_CHANGED}: {key} {name}: {recorded.get(name)} -> {live.get(name)}")
                drift = True
    if drift:
        print("review the new/changed JS execution points, then run --update")
        return 1
    totals = current["totals"]
    print(
        f"OK: JS execution points unchanged "
        f"(engine {totals['engine_sites']}, eval {totals['builtin_eval_sites']}, "
        f"exec {totals['builtin_exec_sites']})"
    )
    return 0


def update() -> int:
    """Rewrite the registry from the live scan.

    Returns:
        Process exit code (always 0).
    """
    current = scan()
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    totals = current["totals"]
    print(
        f"wrote {REGISTRY_PATH}: engine {totals['engine_sites']}, "
        f"eval {totals['builtin_eval_sites']}, exec {totals['builtin_exec_sites']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="compare against the registry")
    group.add_argument("--update", action="store_true", help="rewrite the registry")
    args = parser.parse_args(argv)
    return check() if args.check else update()


if __name__ == "__main__":
    sys.exit(main())
