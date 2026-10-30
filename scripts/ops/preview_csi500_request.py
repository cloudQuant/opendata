#!/usr/bin/env python3
"""Print an offline request-shape preview for one CSI 500 business date."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opendata.pipeline.csi500_request_preview import (  # noqa: E402
    Csi500PreviewInputError,
    parse_business_date,
    preview_csi500_request,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build a CLI with only the caller-selected date as input."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--business-date",
        required=True,
        help="caller-selected ISO date (not checked against an exchange calendar)",
    )
    return parser


def _emit(payload: object) -> None:
    """Write only the bounded, non-secret preview fields as compact JSON."""
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline preview and return a stable status code."""
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = preview_csi500_request(business_date=parse_business_date(args.business_date))
    except Csi500PreviewInputError as error:
        _emit({"status": "blocked", "code": str(error)})
        return 2

    _emit(
        {
            "status": report.status,
            "authorization_status": report.authorization_status,
            "calendar_evidence_status": report.calendar_evidence_status,
            "target_status": report.target_status,
            "gate_status": report.gate_status,
            "execution_enabled": report.execution_enabled,
            "dataset_id": report.dataset_id,
            "symbol": report.symbol,
            "business_date": report.business_date.isoformat(),
            "request": {
                "method": report.method,
                "host": report.host,
                "path": report.path,
                "query_summary_sha256": report.query_summary_sha256,
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
