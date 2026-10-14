"""Falsification arms for the C102 ths offline-grant repair.

The repair added one keyword per offline fetch site: ``ctx=_offline_context(...)``.
That is only evidence if the grant is *load-bearing* — if the shipped guard still
refuses the shapes it is supposed to refuse. Each arm states the identity it must
produce, prints what it observed, and the process exits 1 if any arm reads the other
way. Every arm runs the real ``Fetcher.fetch`` pipeline against an
``httpx.MockTransport``; nothing here reaches a live host.

Arms:
    control           the repaired call returns contract rows (a green file is not all refusals)
    wrong-model       a grant naming a different canonical model is refused, 0 requests sent
    no-ctx            no context at all is refused (the pre-repair face), 0 requests sent
    injected-client   ``client=`` instead of ``mock_transport=`` is refused as unverified
    live-smoke-quota  24 attempts under the live-smoke profile is a quota error
    attempt-bound     a 1-send budget cuts the 20-page catalog loop short
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import contextlib
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

REPO = Path(__file__).resolve().parents[4]


def _load_test_module():
    spec = importlib.util.spec_from_file_location(
        "ths_provider_tests", REPO / "tests/test_ths_provider.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    from opendata.data.protocol import FetchContext
    from opendata.data.providers.ths import FuyaoCredentials, FuyaoHttpClient
    from opendata.data.providers.ths.models import instrument as instrument_module
    from opendata.data.providers.ths.models import stock_daily as stock_daily_module
    from opendata.data.providers.ths.models.instrument import ThsInstrumentFetcher
    from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
    from opendata.data.request_budget import (
        GrantDecision,
        RequestAttemptLimitError,
        RequestAuthorizationError,
        RequestBudgetProfile,
        RequestGrant,
        RequestOperation,
    )

    tests = _load_test_module()
    results: list[tuple[str, bool, str]] = []

    def record(arm: str, ok: bool, observed: str) -> None:
        results.append((arm, ok, observed))
        print(f"arm={arm} verdict={'PASS' if ok else 'FAIL'} observed={observed}")

    def envelope(items: list[dict]) -> bytes:
        return json.dumps(
            {
                "code": 0,
                "message": "success",
                "request_id": "arm",
                "data": {"timestamp": tests.SNAPSHOT_MS, "item": items},
            }
        ).encode()

    sent: list[str] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url.path))
        return httpx.Response(200, content=envelope([tests._bar_row()]))

    def full_page_handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        sent.append(f"offset={offset}")
        # Exactly PAGE_LIMIT rows: no page ever looks short, so only the send
        # budget can stop this loop.
        rows = [tests._instrument_row(f"60{offset + index:04d}.SH") for index in range(2)]
        return httpx.Response(200, content=envelope(rows))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(stock_daily_module, "client", tests._factory(ok_handler))

    # control: the repaired call shape returns contract rows.
    try:
        rows = ThsStockDailyFetcher().fetch(
            ctx=tests._offline_context("stock_daily"),
            symbol="600519.SH",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
        )
        record("control", len(rows) == 1 and rows[0].symbol == "600519.SH", f"rows={len(rows)}")
    except Exception as exc:
        record("control", False, f"{type(exc).__name__}: {exc}")

    # wrong-model: a grant for a different canonical model must not pass.
    sent.clear()
    try:
        ThsStockDailyFetcher().fetch(
            ctx=tests._offline_context("stock_action"),
            symbol="600519.SH",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
        )
        record("wrong-model", False, "no refusal")
    except RequestAuthorizationError as exc:
        record(
            "wrong-model",
            "no exact request grant is configured" in str(exc) and not sent,
            f"{exc!s} requests_sent={len(sent)}",
        )

    # no-ctx: the pre-repair face, still refused before any I/O.
    sent.clear()
    try:
        ThsStockDailyFetcher().fetch(
            symbol="600519.SH", start_date=date(2024, 1, 2), end_date=date(2024, 1, 2)
        )
        record("no-ctx", False, "no refusal")
    except RequestAuthorizationError as exc:
        record(
            "no-ctx",
            "no exact request grant is configured" in str(exc) and not sent,
            f"{exc!s} requests_sent={len(sent)}",
        )

    # injected-client: the seam the tests used before cannot serve a scoped request.
    @contextlib.contextmanager
    def injected_client(*, timeout_seconds: float | None = None):
        active = FuyaoHttpClient(
            credentials=FuyaoCredentials(
                "ths-test-key", base_url=f"https://{tests.MOCK_HOST}"
            ),
            client=httpx.Client(transport=httpx.MockTransport(ok_handler)),
            sleep=lambda seconds: None,
        )
        try:
            yield active
        finally:
            active.close()

    sent.clear()
    monkeypatch.setattr(stock_daily_module, "client", injected_client)
    try:
        ThsStockDailyFetcher().fetch(
            ctx=tests._offline_context("stock_daily"),
            symbol="600519.SH",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
        )
        record("injected-client", False, f"no refusal requests_sent={len(sent)}")
    except RequestAuthorizationError as exc:
        record(
            "injected-client",
            "transport is not verified" in str(exc) and not sent,
            f"{exc!s} requests_sent={len(sent)}",
        )

    # live-smoke-quota: the ceiling the fixture had to declare its way out of.
    try:
        RequestGrant(
            source="ths",
            canonical_model="stock_daily",
            operation=RequestOperation.QUERY,
            decision=GrantDecision.ALLOWED,
            rights_evidence="offline-arm:quota-ceiling",
            task_attempts=tests.OFFLINE_ATTEMPTS,
            source_attempts=tests.OFFLINE_ATTEMPTS,
            allowed_hosts=frozenset({tests.MOCK_HOST}),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            profile=RequestBudgetProfile.LIVE_SMOKE,
        )
        record("live-smoke-quota", False, "grant accepted 24 attempts under live-smoke")
    except ValueError as exc:
        record(
            "live-smoke-quota",
            "must be an integer from 0 through" in str(exc),
            f"{exc!s}",
        )

    # attempt-bound: a 1-send budget stops the paging loop after its first page.
    monkeypatch.setattr(instrument_module, "PAGE_LIMIT", 2)
    monkeypatch.setattr(instrument_module, "client", tests._factory(full_page_handler))
    sent.clear()
    try:
        ThsInstrumentFetcher().fetch(
            ctx=tests._offline_context("instrument", attempts=1), asset_type="a-share"
        )
        record("attempt-bound", False, "1-send budget served an unbounded loop")
    except RequestAttemptLimitError as exc:
        pages = [entry for entry in sent if entry.startswith("offset=")]
        record("attempt-bound", "allowance exhausted" in str(exc), f"{exc!s} pages_seen={len(pages)}")
    except Exception as exc:
        record("attempt-bound", False, f"{type(exc).__name__}: {exc}")
    finally:
        monkeypatch.undo()

    passed = sum(1 for _, ok, _ in results if ok)
    print(
        f"face: arms={len(results)} passed={passed} failed={len(results) - passed}"
        f" attempts={tests.OFFLINE_ATTEMPTS} host={tests.MOCK_HOST}"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
