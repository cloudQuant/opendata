"""Witness the C101 behaviour change in ``ecb.models._client.seam_body``.

The round's mypy face recorded ``union-attr`` at ``_client.py:58``: the function annotated
``raw: bytes | str`` and then called ``raw.encode("utf-8")`` unconditionally, so a seam that
supplies a **bytes** payload would have raised ``AttributeError`` at runtime. The fix returns
bytes unchanged and encodes only the text branches. This probe runs the shipped function over
all four seam shapes and shows the bytes branch is the one the old line could not serve.

Arm ``--arm`` re-executes the *old* last line against the same bytes payload and requires it to
raise, so the face cannot be read as "the branch was unreachable anyway".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from opendata.data.providers._engine.http_json import HttpResponse  # noqa: E402
from opendata.data.providers.ecb.models._client import seam_body  # noqa: E402

#: ``seam_body(`` but not the ``_seam_body(`` that sec, congress_gov and bls keep to themselves.
CALLER_RE = re.compile(r"(?<!_)\bseam_body\(")

CASES = [
    ("bytes payload", HttpResponse(status=200, document=None, payload=b"<Root/>"), b"<Root/>"),
    ("str payload", HttpResponse(status=200, document=None, payload="<Root/>"), b"<Root/>"),
    (
        "list document",
        HttpResponse(status=200, document=["<Root>", "<Child/>"]),
        b"<Root>\n<Child/>",
    ),
    ("str document", HttpResponse(status=200, document="<Root/>"), b"<Root/>"),
    ("empty document", HttpResponse(status=200, document=123), b""),
]


def main(argv: list[str] | None = None) -> int:
    """Print the shipped reading, or run the old-line arm."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="show the pre-fix line fails on bytes")
    args = parser.parse_args(argv)

    if args.arm:
        payload_bytes = CASES[0][1].payload
        assert isinstance(payload_bytes, bytes)  # nosec B101  # noqa: S101
        try:
            payload_bytes.encode("utf-8")  # type: ignore[attr-defined]
        except AttributeError as exc:
            print(
                f"arm: old line `raw.encode('utf-8')` on bytes payload -> "
                f"AttributeError({exc}) PASS"
            )
            return 0
        print("arm: old line did not fail -- the recorded gap does not exist -> FAIL")
        return 1

    failures = 0
    roots = [REPO / "opendata", REPO / "scripts"]
    callers = sum(
        1
        for root in roots
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
        and path.name != "_client.py"
        and CALLER_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
    )
    print(f"population: shapes={len(CASES)} ecb_seam_body_external_caller_files={callers}")
    for label, response, expected in CASES:
        try:
            actual = seam_body(response)
        except Exception as exc:
            failures += 1
            print(f"RAISED {label}: {type(exc).__name__}({exc})")
            continue
        ok = actual == expected and type(actual) is bytes
        failures += 0 if ok else 1
        print(f"{'OK' if ok else 'WRONG'} {label}: -> {actual!r}")
    print(f"face: shapes_ok={len(CASES) - failures} of {len(CASES)}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
