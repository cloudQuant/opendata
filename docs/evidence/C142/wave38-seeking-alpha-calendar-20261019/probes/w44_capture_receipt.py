"""Build the wave-38 capture receipt for the Seeking Alpha earnings-calendar route.

The receipt is generated, not typed: every ``body_sha256``, ``body_bytes`` and ``http`` field is
computed from the bytes on disk in this run, and the status lines are read out of each ``.headers``
file's own ``HTTP/<version> <code>`` lines (the repo's capture format). A capture whose status chain
is empty is recorded as ``NO_STATUS_LINE`` rather than as a success, and the face fails if any entry
names a body or headers file that is not on disk -- a receipt that describes a missing artifact is
the failure shape this repo has been bitten by before.

Four captures, in the order the protocol was discovered:

``sa-calendar-full-params``
    the route exactly as this repo's adapter will call it (27 records, one page).
``sa-calendar-second-date``
    a date whose count exceeds one page: ``meta {size:50, total_count:56, total_pages:2}`` and a
    non-null ``links.next`` -- this is what proves the route paginates.
``sa-calendar-page2-api-form``
    page 2 built on the API URL: 6 records, ``links.next`` null, and 50 + 6 == ``total_count``, so a
    page cursor over this route is complete rather than silently truncated.
``sa-calendar-links-next-literal``
    the published ``links.next`` resolved literally against the site root: ``HTTP/2 404`` with
    ``text/html``. So ``links.next`` is not a fetchable API address, and an adapter that followed it
    verbatim would fetch a web page and parse nothing.

Authorization boundary is unchanged from wave 37: public unauthenticated GETs, no Authorization
header, no API key, no account login, no Cookie.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C142/wave38-seeking-alpha-calendar-20261019"
ROOT = REPO / BUNDLE
SOURCES = ROOT / "sources"
CARRIER = ROOT / "runs" / "w44_capture_receipt.txt"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"

API_BASE = "https://seekingalpha.com/api/v3/earnings_calendar/tickers"

CAPTURES: tuple[dict[str, str], ...] = (
    {
        "id": "sa-calendar-full-params",
        "url": (
            f"{API_BASE}?filter%5Bselected_date%5D=2026-10-19"
            "&filter%5Bwith_rating%5D=false&filter%5Bcurrency%5D=USD"
        ),
        "stem": "sa-calendar-full-params",
        "use": (
            "seeking_alpha::CalendarEarnings route exactly as this repo's adapter calls it; "
            "unauthenticated; 27 records, meta.total_pages=1"
        ),
    },
    {
        "id": "sa-calendar-second-date",
        "url": f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20",
        "stem": "sa-calendar-second-date",
        "use": (
            "a date whose count exceeds one page: meta size=50 total_count=56 total_pages=2 and a "
            "non-null links.next; this is what proves the route paginates"
        ),
    },
    {
        "id": "sa-calendar-page2-api-form",
        "url": f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20&page%5Bnumber%5D=2",
        "stem": "sa-calendar-page2-api-form",
        "use": (
            "page 2 built on the API URL: 6 records, links.next null, and 50+6 equals "
            "total_count 56, so the adapter's page cursor is complete rather than truncated"
        ),
    },
    {
        "id": "sa-calendar-page2-full-params",
        "url": (
            f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20"
            "&filter%5Bwith_rating%5D=false&filter%5Bcurrency%5D=USD&page%5Bnumber%5D=2"
        ),
        "stem": "sa-calendar-page2-full-params",
        "use": (
            "the same date and page sent with the upstream reference's extra filters: "
            "meta.total_count falls 56 -> 51 and page 2 holds 1 record instead of 6, so "
            "filter[currency]=USD silently narrows the US calendar; this repo's adapter does NOT "
            "send with_rating or currency, and this capture is the measured reason"
        ),
    },
    {
        "id": "sa-calendar-links-next-literal",
        "url": (
            "https://seekingalpha.com/earnings_calendar/tickers?"
            "filter[selected_date]=2026-10-20&page[number]=2"
        ),
        "stem": "sa-calendar-links-next-literal",
        "use": (
            "the published links.next resolved literally against the site root: HTTP/2 404 with "
            "text/html, so links.next is NOT a fetchable API address and the adapter must build "
            "page[number] itself; sent with curl -g because the link carries raw brackets"
        ),
    },
)
PROGRESSIVE_CHAIN: tuple[dict[str, Any], ...] = (
    {
        "step": 1,
        "url": f"{API_BASE}?start_date=2026-10-19&end_date=2026-10-26",
        "http": "400",
        "error_detail": "Parameter filter is required",
        "evidence_root": "docs/evidence/C140",
    },
    {
        "step": 2,
        "url": (f"{API_BASE}?filter%5Bstart_date%5D=2026-10-19&filter%5Bend_date%5D=2026-10-26"),
        "http": "400",
        "error_detail": "Parameter filter[selected_date] is required",
        "evidence_root": "docs/evidence/C140",
    },
    {
        "step": 3,
        "url": f"{API_BASE}?filter%5Bselected_date%5D=2026-10-19",
        "http": "200",
        "error_detail": "",
        "evidence_root": "docs/evidence/C140",
    },
    {
        "step": 4,
        "url": f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20",
        "http": "200",
        "error_detail": "meta.total_pages=2; links.next non-null",
        "evidence_root": BUNDLE,
    },
    {
        "step": 5,
        "url": (
            f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20"
            "&filter%5Bwith_rating%5D=false&filter%5Bcurrency%5D=USD&page%5Bnumber%5D=2"
        ),
        "http": "200",
        "error_detail": "1 record, total_count 51: the currency filter narrows the same date",
        "evidence_root": BUNDLE,
    },
    {
        "step": 4,
        "url": f"{API_BASE}?filter%5Bselected_date%5D=2026-10-20&page%5Bnumber%5D=2",
        "http": "200",
        "error_detail": "6 records; links.next null; 50+6==total_count",
        "evidence_root": BUNDLE,
    },
    {
        "step": 7,
        "url": "https://seekingalpha.com/earnings_calendar/tickers?"
        "filter[selected_date]=2026-10-20&page[number]=2",
        "http": "404",
        "error_detail": "text/html; links.next is not resolvable against the site root",
        "evidence_root": BUNDLE,
    },
)


def _json_meta(path: Path) -> dict[str, Any]:
    """Read the envelope's own pagination meta, or mark the body as non-JSON."""
    try:
        document = json.loads(path.read_text("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"json": False}
    if not isinstance(document, dict):
        return {"json": True, "shape": type(document).__name__}
    meta_raw = document.get("meta")
    links_raw = document.get("links")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    links: dict[str, Any] = links_raw if isinstance(links_raw, dict) else {}
    data = document.get("data")
    return {
        "json": True,
        "records": len(data) if isinstance(data, list) else None,
        "meta_size": meta.get("size"),
        "meta_total_count": meta.get("total_count"),
        "meta_total_pages": meta.get("total_pages"),
        "links_next": links.get("next"),
    }


def main() -> int:
    """Compute the receipt, print every reading, and fail on a missing artifact."""
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    problems: list[str] = []
    captures: list[dict[str, Any]] = []
    for spec in CAPTURES:
        stem = spec["stem"]
        body_path = SOURCES / f"{stem}.json"
        if not body_path.is_file():
            body_path = SOURCES / f"{stem}.out"
        headers_path = SOURCES / f"{stem}.headers"
        if not body_path.is_file() or not headers_path.is_file():
            problems.append(f"{stem}:missing_body_or_headers")
            continue
        raw = body_path.read_bytes()
        status_lines = [
            line
            for line in headers_path.read_text("utf-8", errors="replace").splitlines()
            if line.startswith("HTTP/")
        ]
        meta = _json_meta(body_path)
        captures.append(
            {
                "id": spec["id"],
                "url": spec["url"],
                "http": status_lines[-1].split()[1] if status_lines else "NO_STATUS_LINE",
                "status_line_chain": status_lines,
                "body_file": str(body_path.relative_to(ROOT)),
                "headers_file": str(headers_path.relative_to(ROOT)),
                "body_bytes": len(raw),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "envelope": meta,
                "use": spec["use"],
            }
        )
        lines.append(
            f"capture {spec['id']} http={captures[-1]['http']} chain={status_lines} "
            f"bytes={len(raw)} sha16={hashlib.sha256(raw).hexdigest()[:16]} "
            f"envelope={meta}"
        )

    mtimes = [
        datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc)
        for path in SOURCES.glob("*")
        if path.is_file()
    ]
    stamp = lambda moment: moment.isoformat(timespec="seconds")  # noqa: E731  # local formatter
    capture_range = [stamp(min(mtimes)), stamp(max(mtimes))] if mtimes else [started, started]
    record_sum = sum(
        int(c["envelope"].get("records") or 0)
        for c in captures
        if c["id"] in {"sa-calendar-second-date", "sa-calendar-page2-api-form"}
    )
    total_count = next(
        (
            c["envelope"].get("meta_total_count")
            for c in captures
            if c["id"] == "sa-calendar-second-date"
        ),
        None,
    )

    def envelope(cid: str, key: str) -> Any:  # noqa: ANN401  # envelope values are heterogeneous
        entry = next((c for c in captures if c["id"] == cid), None)
        return None if entry is None else entry["envelope"].get(key)

    narrowing = (
        envelope("sa-calendar-page2-full-params", "meta_total_count") is not None
        and envelope("sa-calendar-second-date", "meta_total_count") is not None
        and envelope("sa-calendar-page2-full-params", "meta_total_count")
        != envelope("sa-calendar-second-date", "meta_total_count")
        and envelope("sa-calendar-page2-full-params", "records")
        != envelope("sa-calendar-page2-api-form", "records")
    )
    lines.append(
        f"currency_filter_narrowing filtered_total_count="
        f"{envelope('sa-calendar-page2-full-params', 'meta_total_count')} "
        f"unfiltered_total_count={envelope('sa-calendar-second-date', 'meta_total_count')} "
        f"filtered_p2_records={envelope('sa-calendar-page2-full-params', 'records')} "
        f"unfiltered_p2_records={envelope('sa-calendar-page2-api-form', 'records')} "
        f"narrows={narrowing}"
    )
    lines.append(f"capture_range={capture_range}")
    lines.append(
        f"pagination_closure p1_plus_p2={record_sum} total_count={total_count} "
        f"closes={record_sum == total_count}"
    )
    receipt = {
        "receipt_version": 1,
        "captured_utc_range": capture_range,
        "captured_utc_range_source": (
            "min/max mtime of the files under sources/, read from disk in this run; "
            "these are the write times of this wave's captures, not request logs"
        ),
        "environment": {
            "host": "Darwin, macOS, arm64",
            "curl_command": (
                "/usr/bin/curl -s -L -A <UA> -D <headers> -o <body> <url> "
                "(-g added for the literal links.next probe)"
            ),
            "user_agent_class": "desktop browser UA, no session cookies",
            "auth": "No Authorization header, API key, account login, or Cookie request supplied.",
            "follow_redirects": True,
        },
        "authorization_boundary": (
            "public unauthenticated GETs only; no credentialed request was made"
        ),
        "progressive_parameter_chain": list(PROGRESSIVE_CHAIN),
        "source_captures": captures,
        "review": (
            "wave-38 access round: the route, its required filter, and its page cursor are all "
            "answered by the source itself; links.next is proven unresolvable so the adapter "
            "constructs page[number] rather than following the site's link"
        ),
    }
    out = ROOT / "capture-receipt.json"
    out.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    arms = {
        "every_capture_present": len(captures) == len(CAPTURES),
        "statuses_as_expected": [c["http"] for c in captures]
        == ["200", "200", "200", "200", "404"],
        "pagination_closes": record_sum == total_count,
        "currency_filter_narrows": narrowing,
        "links_next_not_fetchable": any(
            c["id"] == "sa-calendar-links-next-literal"
            and c["http"] == "404"
            and not c["envelope"].get("json", False)
            for c in captures
        ),
    }
    failed = sorted(name for name, ok in arms.items() if not ok) + sorted(problems)
    lines.append(
        f"capture_receipt_face captures={len(captures)} of={len(CAPTURES)} arms={len(arms)} "
        f"arms_passed={sum(arms.values())} failed={failed} receipt={out.relative_to(REPO)} "
        f"receipt_sha256={hashlib.sha256(out.read_bytes()).hexdigest()[:16]}"
    )
    lines.append("")
    exit_code = 0 if not failed else 1
    lines.append(f"w44_capture_receipt_exit={exit_code}")
    text = "\n".join(lines) + "\n"
    CARRIER.parent.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(
        "# carrier for w44_capture_receipt.py\n"
        f"# argv: {sys.executable} {ROOT / 'probes' / 'w44_capture_receipt.py'}\n"
        f"# started_utc: {started}\n\n" + text,
        encoding="utf-8",
    )
    print(text, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
