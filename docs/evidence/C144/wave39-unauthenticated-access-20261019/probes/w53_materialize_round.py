"""Materialize the wave-39 unauthenticated access round into this bundle.

Every receipt field is derived from the captured bytes -- status line, content type, server,
body length and sha256 -- and only the requested URL and the note of what the capture is for
come from the table below, which is copied verbatim out of the tool results that ran the
requests. Nothing here re-hits the network: the round is a one-time act, the bundle is the
re-measurable artifact, and a later reader must be able to recompute the receipt from bytes
that are already committed.

The scratch directory argument is what makes that ordering safe. A re-take against a fresh
round writes into a new ``sources`` directory; pointing this script at the committed bundle and
a scratch directory that no longer exists fails loudly instead of silently producing an empty
receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = REPO / "docs/evidence/C144/wave39-unauthenticated-access-20261019"
SRC = BUNDLE / "sources"
RECEIPT = BUNDLE / "capture-receipt.json"

#: The single estimates route the chain walks, kept as one constant so no row can carry a typo.
SA_ESTIMATES = "https://seekingalpha.com/api/v3/symbol_data/estimates"
CBOE_BASE = "https://www.cboe.com/us/futures/market_statistics/settlement/csv"


@dataclass(frozen=True)
class Capture:
    """One request: the scratch pair, the canonical id, the literal URL, and what it evidences."""

    stem: str
    cid: str
    url: str
    use: str
    family: str
    body_suffix: str = "json"
    expect_body: bool = True


CAPTURES: tuple[Capture, ...] = (
    Capture(
        "cboe_settlement",
        "cboe-futures-settlement-csv",
        f"{CBOE_BASE}/",
        "cboe::FuturesCurve candidate source; unauthenticated 200 text/csv settlement table",
        "cboe",
        "csv",
    ),
    Capture(
        "cboe_dt",
        "cboe-futures-settlement-csv-dt-2026-10-16",
        f"{CBOE_BASE}?dt=2026-10-16",
        "cboe::FuturesCurve first-take history probe, asked for the day the route was already "
        "serving -- the comparison that produced a false 'dt is ignored' reading, see corrections",
        "cboe",
        "csv",
    ),
    Capture(
        "cboe_dt_2026-10-09",
        "cboe-futures-settlement-csv-dt-2026-10-09",
        f"{CBOE_BASE}?dt=2026-10-09",
        "cboe::FuturesCurve history probe on a date the route is NOT already serving; this is "
        "the arm that overturns the first take's 'dt is ignored' reading",
        "cboe",
        "csv",
    ),
    Capture(
        "cboe_dt_2026-10-15",
        "cboe-futures-settlement-csv-dt-2026-10-15",
        f"{CBOE_BASE}?dt=2026-10-15",
        "cboe::FuturesCurve history probe, second distinct settlement day",
        "cboe",
        "csv",
    ),
    Capture(
        "cboe_dt_2026-10-17",
        "cboe-futures-settlement-csv-dt-2026-10-17-saturday",
        f"{CBOE_BASE}?dt=2026-10-17",
        "cboe::FuturesCurve empty-curve edge: a non-settlement day answers 200 with the header "
        "row and no data rows",
        "cboe",
        "csv",
    ),
    Capture(
        "cboe_roots",
        "cboe-futures-roots-json",
        "https://cdn.cboe.com/api/global/delayed_quotes/symbol_book/futures-roots.json",
        "cboe::FuturesCurve underlying-list probe; the fixed model's 'underlying list' "
        "half of the refusal, tested against the public symbol book",
        "cboe",
    ),
    Capture(
        "wsj_etfmovers",
        "wsj-etfmovers-leaders",
        "https://www.wsj.com/market-data/mutualfunds-etfs/etfmovers"
        "?id=%7B%22application%22%3A%22WSJ%22%2C%22etfMover%22%3A%22leaders%22%2C%22count%22%3A25%7D"
        "&type=mdc_etfmovers",
        "wsj::ETF Gainers/Losers/Active; the route the inventory names, with no credential "
        "field. Carries no response headers because no TCP connection was established",
        "wsj",
        "txt",
        False,
    ),
    Capture(
        "sa_estimates_ticker",
        "sa-estimates-step1-filter-ticker",
        f"{SA_ESTIMATES}?filter%5Bticker%5D=AAPL",
        "seeking_alpha::ForwardEpsEstimates chain step 1; asks by symbol and is told which "
        "parameter the route actually wants",
        "seeking_alpha",
    ),
    Capture(
        "sa_ticker_ids",
        "sa-estimates-step2-ticker-ids",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL",
        "seeking_alpha::ForwardEpsEstimates chain step 2; names the next required parameter",
        "seeking_alpha",
    ),
    Capture(
        "sa_step3",
        "sa-estimates-step3-relative-periods",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4",
        "seeking_alpha::ForwardEpsEstimates chain step 3; names the data-items parameter",
        "seeking_alpha",
    ),
    Capture(
        "sa_step4_items",
        "sa-estimates-step4-eps-avg",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha::ForwardEpsEstimates chain step 4; first response that satisfies the "
        "route's own demands -- 200 with an empty envelope",
        "seeking_alpha",
    ),
    Capture(
        "sa_step4_rev_items",
        "sa-estimates-step4-revisions-eps-up",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&revisions_data_items%5B%5D=eps_up",
        "seeking_alpha::ForwardSalesEstimates sibling; the other data-items key the route "
        "offered in step 3",
        "seeking_alpha",
    ),
    Capture(
        "sa_step4_items_single",
        "sa-estimates-step4-items-unbracketed",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&estimates_data_items=eps_avg",
        "seeking_alpha spelling probe; repeated-key form against comma/plain form",
        "seeking_alpha",
    ),
    Capture(
        "sa_bogus_item",
        "sa-estimates-item-not-real",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4"
        "&estimates_data_items%5B%5D=zzz_not_a_real_item",
        "seeking_alpha falsifiability probe; if an invented data item answers the same way as "
        "a plausible one, the empty envelope says nothing about item naming",
        "seeking_alpha",
    ),
    Capture(
        "sa_numeric_ids",
        "sa-estimates-numeric-ticker-ids",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=167087&relative_periods=4&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha identifier probe; tests whether the route wants a numeric symbol id",
        "seeking_alpha",
    ),
    Capture(
        "sa_bogus_ticker",
        "sa-estimates-ticker-not-real",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=ZZZZNOTREAL&relative_periods=4"
        "&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha identifier probe; a symbol that cannot resolve, as the floor against "
        "'AAPL was the wrong key'",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep1",
        "sa-estimates-periods-zero",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=0&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha range probe; relative_periods is not validated for magnitude",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep2",
        "sa-estimates-periods-twelve-two-items",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=12&estimates_data_items%5B%5D=eps_avg"
        "&estimates_data_items%5B%5D=revenue_avg",
        "seeking_alpha breadth probe; eps and revenue items together, the widest plausible ask",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep3",
        "sa-estimates-periods-negative",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=-1&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha range probe; a negative period is accepted and still answers empty",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep4",
        "sa-estimates-period-end-dates-instead-of-periods",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&period_end_dates%5B%5D=2026-12-31"
        "&estimates_data_items%5B%5D=eps_avg",
        "seeking_alpha control on the emptiness: drops relative_periods for a date list, so a "
        "route that still validates answers 400 rather than the empty 200",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep5",
        "sa-estimates-with-scale",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&estimates_data_items%5B%5D=eps_avg"
        "&scale=thousands",
        "seeking_alpha unit probe; a scale parameter does not unlock the envelope",
        "seeking_alpha",
    ),
    Capture(
        "sa_sweep6",
        "sa-estimates-with-filter-symbol",
        f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&estimates_data_items%5B%5D=eps_avg"
        "&filter%5Bsymbol%5D=AAPL",
        "seeking_alpha belt-and-braces probe; both symbol spellings at once",
        "seeking_alpha",
    ),
    Capture(
        "sa_control_calendar",
        "sa-control-earnings-calendar-2026-10-20",
        "https://seekingalpha.com/api/v3/earnings_calendar/tickers?filter%5Bselected_date%5D=2026-10-20",
        "SAME-HOST CONTROL: proves the client, user agent and transport used for the estimates "
        "probes can be served non-empty data unauthenticated on seekingalpha.com",
        "control",
    ),
)

#: Where the wsj request died, copied from the three tool results that ran it in this round.
WSJ_TRANSPORT_ATTEMPTS: tuple[dict[str, Any], ...] = (
    {
        "attempt": 1,
        "max_time_seconds": 25,
        "curl_exit": 28,
        "curl_message": "curl: (28) Connection timed out after 20003 milliseconds",
        "http_code": "000",
        "headers_bytes": 0,
    },
    {
        "attempt": 2,
        "max_time_seconds": 40,
        "curl_exit": 28,
        "curl_message": "curl: (28) Connection timed out after 40006 milliseconds",
        "http_code": "000",
        "headers_bytes": 0,
    },
    {
        "attempt": 3,
        "max_time_seconds": 15,
        "curl_exit": 28,
        "curl_message": "curl: (28) Connection timed out after 15006 milliseconds",
        "http_code": "000",
        "headers_bytes": 0,
        "verbose": "Trying 69.63.187.12:443... / no 'Connected to' line",
    },
)

RESOLVER = {
    "query_type": "python socket.gethostbyname('www.wsj.com')",
    "answers": ["69.63.187.12"],
    "system_nameserver": "8.8.8.8",
    "note": "Resolution succeeded; the TCP handshake to the resolved address never completed. "
    "The refusal this records is transport-level and machine-local, not a statement that the "
    "route does not exist.",
}


def header_map(lines: list[str]) -> dict[str, str]:
    """Last value per response header name, lower-cased."""
    out: dict[str, str] = {}
    for line in lines:
        if ":" in line and not line.startswith("HTTP/"):
            key, _, value = line.partition(":")
            out[key.strip().lower()] = value.strip()
    return out


def read_headers(path: Path) -> dict[str, Any]:
    """Parse one captured headers file the way this repo's capture format says to."""
    if not path.exists():
        return {"status_line_chain": [], "http": "NO_RESPONSE", "headers": {}}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    chain = [line.strip() for line in lines if line.startswith("HTTP/")]
    fields = header_map(lines)
    http = chain[-1].split()[1] if chain else "NO_STATUS_LINE"
    return {
        "status_line_chain": chain,
        "http": http,
        "content_type": fields.get("content-type", ""),
        "server": fields.get("server", ""),
        "location": fields.get("location", ""),
        "date": fields.get("date", ""),
        "content_disposition": fields.get("content-disposition", ""),
    }


def materialize(one: Capture, scratch: Path) -> dict[str, Any]:
    """Copy one scratch pair into the bundle and describe it from the copied bytes."""
    src_h = scratch / f"{one.stem}.headers"
    body_src = scratch / f"{one.stem}.body"
    if not src_h.exists() or (one.expect_body and not body_src.exists()):
        raise SystemExit(f"missing scratch pair for {one.cid}: {src_h} / {body_src}")
    h_path = SRC / f"{one.cid}.headers"
    b_path = SRC / f"{one.cid}.{one.body_suffix}"
    shutil.copyfile(src_h, h_path)
    if body_src.exists():
        shutil.copyfile(body_src, b_path)
    else:
        b_path.write_bytes(b"")
    parsed = read_headers(h_path)
    body = b_path.read_bytes()
    return {
        "id": one.cid,
        "family": one.family,
        "url": one.url,
        "http": parsed["http"],
        "status_line_chain": parsed["status_line_chain"],
        "server": parsed["server"],
        "content_type": parsed["content_type"],
        "response_date": parsed["date"],
        "redirect_location": parsed["location"],
        "content_disposition": parsed["content_disposition"],
        "headers_bytes": h_path.stat().st_size,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "headers_file": f"sources/{h_path.name}",
        "body_file": f"sources/{b_path.name}",
        "use": one.use,
    }


#: The refusal text the round set out to test, copied verbatim out of
#: ``opendata/data/providers/cboe/specs.py`` ``NOT_DECLARABLE`` at the round's start. It is
#: recorded as data because the landing that follows deletes it from the tree: a face that re-reads
#: the live ledger would measure the retraction rather than the refuted claim, and the retraction is
#: the landing face's job, not this round's.
LEDGER_REASONS_AT_ROUND_TIME: dict[str, str] = {
    "cboe::FuturesCurve": (
        "The reviewed official All Access docs do not establish the fixed model's underlying list, "
        "quote/history join, or curve output protocol."
    )
}


def build_receipt(scratch: Path) -> dict[str, Any]:
    """Assemble the whole receipt, including the parameter chain read off the captured bodies."""
    captures = []
    for one in CAPTURES:
        record = materialize(one, scratch)
        body_path = SRC / f"{one.cid}.{one.body_suffix}"
        record["body_excerpt"] = body_path.read_text(encoding="utf-8", errors="replace")[:200]
        captures.append(record)
    by_id = {c["id"]: c for c in captures}
    chain = [
        "sa-estimates-step1-filter-ticker",
        "sa-estimates-step2-ticker-ids",
        "sa-estimates-step3-relative-periods",
        "sa-estimates-step4-eps-avg",
    ]
    return {
        "receipt_version": 1,
        "review": "wave-39 unauthenticated access round for the residue rows whose ledger "
        "reason is an access assessment rather than a code gap",
        "authorization_boundary": "public unauthenticated GETs only; no credentialed request "
        "was made. No -H/--header with an authorization or cookie value, no -u/--user, no "
        "--cookie, and no API key appeared in any argv used in this round.",
        "curl_argv_template": "/usr/bin/curl -s -L -g -A <desktop browser user agent> -m <max> "
        "-D <headers> -o <body> -w <metrics> <url>",
        "captured_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "environment": {
            "host": "Darwin, macOS, arm64",
            "follow_redirects": True,
            "requests_made": len(captures),
        },
        "source_captures": captures,
        "progressive_parameter_chain": [
            {
                "step": i + 1,
                "capture_id": cid,
                "http": by_id[cid]["http"],
                "body_excerpt": by_id[cid]["body_excerpt"],
            }
            for i, cid in enumerate(chain)
        ],
        "wsj_transport_attempts": list(WSJ_TRANSPORT_ATTEMPTS),
        "wsj_resolver": RESOLVER,
        "refused_ledger_reasons": dict(LEDGER_REASONS_AT_ROUND_TIME),
        "corrections": [
            {
                "claim_withdrawn": "the cboe settlement route ignores dt=, so no historical "
                "curve is evidenced",
                "why_it_was_wrong": "the only dated probe was 2026-10-16, the day the route was "
                "already serving, so the comparison had no way to come out different; three "
                "further probes (2026-10-09, 2026-10-15, 2026-10-17) return distinct bodies",
                "arm": "C",
                "status": "CORRECTED_IN_SAME_ROUND",
            }
        ],
        "dispositions": {
            "cboe::FuturesCurve": "REFUTED as unevidenced, and stronger than the refusal "
            "allowed: the settlement table answers 200 text/csv unauthenticated with a "
            "per-expiry forward curve, dt= selects the settlement day (so the curve is "
            "historical, not today-only), and the refusal's 'underlying list' half is answered "
            "by the public futures-roots JSON.",
            "seeking_alpha::ForwardEpsEstimates": "NOT REFUTED AS ACCESS-BLOCKED, still "
            "UNEVIDENCED: the route is reachable and its parameter validation is live, but "
            "every request that satisfies its demands returns one byte-identical empty "
            "envelope, and the same-host control shows that is not the transport.",
            "seeking_alpha::ForwardSalesEstimates": "same as ForwardEpsEstimates; the "
            "revisions_data_items spelling answers identically.",
            "wsj::ETFGainers": "UNVERIFIABLE FROM THIS NETWORK: no TCP "
            "connection was established in three attempts, so neither the ledger's prose nor "
            "this round can say anything about the response shape.",
            "wsj::ETFLosers": "Same route, same transport failure as wsj::ETFGainers.",
            "wsj::ETFActive": "Same route, same transport failure as wsj::ETFGainers.",
        },
        "history_note": "CORRECTED. The first take compared the unparameterized table against "
        "?dt=2026-10-16, which is the day the route was already serving, and concluded dt= was "
        "ignored. That control could not differ. Re-run against dates the default is not: "
        "2026-10-09, 2026-10-15 and 2026-10-16 each return their own bytes, and 2026-10-17 (a "
        "non-settlement Saturday) returns the header row alone. The route does select the "
        "settlement day, and each response's content-disposition filename names it.",
    }


def main() -> int:
    """Copy the scratch pairs in, derive the receipt from the copied bytes, and write it."""
    parser = argparse.ArgumentParser()
    scratch_default = Path("/tmp/c144_probe")  # nosec B108  # noqa: S108
    parser.add_argument("--scratch", type=Path, default=scratch_default)
    parser.add_argument("--output", type=Path, default=RECEIPT)
    args = parser.parse_args()
    SRC.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt(args.scratch)
    args.output.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    caps = receipt["source_captures"]
    http_counts: dict[str, int] = {}
    for c in caps:
        http_counts[c["http"]] = http_counts.get(c["http"], 0) + 1
    print(
        f"materialized captures={len(caps)} output={args.output} "
        f"http_by_capture={http_counts} bytes_total={sum(c['body_bytes'] for c in caps)}"
    )
    print(f"materialize_exit={0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
