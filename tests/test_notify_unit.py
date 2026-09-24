"""Notify hook unit tests (AC-11, gate-runnable).

The warehouse round trip lives in the e2e suite; these cover the pure
helpers: the symbol extraction from the runner's affected keys and the
hook factory.
"""

from __future__ import annotations

from datetime import date

from opendata.pipeline.notify import BatchNotifier, _symbols_of, build_notify_hook
from opendata.pipeline.runner import PipelineContext, Window


def _context(keys) -> PipelineContext:
    return PipelineContext(
        domain="stock_daily",
        source="ths",
        window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
        affected_keys=keys,
    )


class TestSymbolsOf:
    def test_symbols_are_deduped_in_first_seen_order(self):
        symbols = _symbols_of(
            _context(
                [
                    ("600519", date(2026, 9, 23)),
                    ("600519", date(2026, 9, 22)),
                    ("000001", date(2026, 9, 23)),
                ]
            )
        )

        assert symbols == ("600519", "000001")

    def test_empty_keys_yield_nothing(self):
        assert _symbols_of(_context([])) == ()

    def test_empty_key_tuples_are_skipped(self):
        assert _symbols_of(_context([(), ("000001", date(2026, 9, 23))])) == ("000001",)


class TestBuildHook:
    def test_returns_a_batch_notifier(self):
        hook = build_notify_hook(object(), batch_id="a" * 36, layer="ods", table="t")

        assert isinstance(hook, BatchNotifier)
        assert callable(hook)

    def test_notifier_defaults_batch_id(self):
        notifier = build_notify_hook(object())

        assert notifier.batch_id is None  # generated per call
        assert notifier.layer == "ods"
