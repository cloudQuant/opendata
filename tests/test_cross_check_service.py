"""Cross-check service and alert governance tests (A4.5, design §8.2).

Alert governance has three rules from the design: a known-difference
whitelist (per domain+field) suppresses expected noise, a diff
fingerprint stops consecutive identical differences from re-alerting,
and leveling only escalates when the diff rate jumps. The service
ties the pieces together for the pipeline's ``cross_check`` hook:
read both sources for the window, compare, write ``dq_diff_report``
and hand the decision to the injected notifier.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from opendata.data.mapping import DomainMapping, FieldMapping
from opendata.pipeline.alerts import AlertDecision, AlertPolicy, fingerprint
from opendata.pipeline.cross_check import DiffSummary, Verdict
from opendata.pipeline.cross_check_service import CrossCheckService
from opendata.pipeline.runner import PipelineContext, Window

CHECKED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
BATCH_ID = "7a1f4c2e-8b3d-4e6a-9f2c-1d5b8e7a3c64"
WINDOW = Window(start=date(2024, 1, 1), end=date(2024, 1, 31))


def _summary(*, deviation=2, missing=0, rate_keys=10, field="close") -> DiffSummary:
    from opendata.pipeline.cross_check import FieldDiff

    return DiffSummary(
        domain="stock_daily",
        source_a="akshare",
        source_b="ths",
        checked_at=CHECKED_AT,
        batch_id=BATCH_ID,
        compared_keys=rate_keys,
        deviation_count=deviation,
        missing_count=missing,
        per_field={field: deviation},
        samples=[FieldDiff(("600519", date(2024, 1, 2)), field, 1.0, 2.0, 0.5, Verdict.DEVIATION)]
        if deviation
        else [],
    )


class TestAlertPolicy:
    def test_no_diffs_means_no_alert(self):
        decision = AlertPolicy().decide(_summary(deviation=0))

        assert decision.alert is False
        assert "consistent" in decision.reason

    def test_first_occurrence_alerts_at_warning(self):
        decision = AlertPolicy().decide(_summary())

        assert decision.alert is True
        assert decision.level == "warning"

    def test_repeated_fingerprint_is_deduplicated(self):
        policy = AlertPolicy()

        assert policy.decide(_summary()).alert is True
        second = policy.decide(_summary())
        assert second.alert is False
        assert "already alerted" in second.reason

    def test_whitelisted_domain_field_is_suppressed(self):
        policy = AlertPolicy(whitelist={("stock_daily", "close")})

        decision = policy.decide(_summary(field="close"))

        assert decision.alert is False
        assert "whitelist" in decision.reason

    def test_rate_spike_escalates_to_critical(self):
        policy = AlertPolicy()

        first = policy.decide(_summary(deviation=1, rate_keys=100))
        second = policy.decide(_summary(deviation=9, rate_keys=10, field="volume"))

        assert first.level == "warning"
        assert second.level == "critical"

    def test_fingerprint_ignores_the_batch_id(self):
        first = fingerprint(_summary())
        other = fingerprint(_summary())

        assert first == other


class TestCrossCheckService:
    def _service(self, frames, **overrides):
        written: list[DiffSummary] = []
        alerts: list[AlertDecision] = []
        # The notifier gets the summary too: a suppressed difference is a
        # recorded outcome, so the payload must reach the delivery layer.
        notified: list[DiffSummary] = []

        async def notifier(summary, decision):
            notified.append(summary)
            alerts.append(decision)

        mapping = DomainMapping(
            domain="stock_daily",
            key=("symbol", "trade_date"),
            fields={
                "symbol": FieldMapping("symbol", normalize="plain"),
                "trade_date": FieldMapping("trade_date"),
                "close": FieldMapping("close"),
            },
            adjust="unadjusted",
            suspension="absent_row",
            denominator="key_union",
            tolerances={"close": 1e-4},
        )
        service = CrossCheckService(
            "stock_daily",
            sources=("akshare", "ths"),
            mappings={"akshare": mapping, "ths": mapping},
            readers={
                name: (lambda window, name=name: frames[name].copy()) for name in ("akshare", "ths")
            },
            write_report=lambda summary: written.append(summary) or len(summary.samples),
            notifier=notifier,
            checked_at=CHECKED_AT,
            **overrides,
        )
        return service, written, alerts, notified

    def _frames(self, close_b=1700.0):
        base = pd.DataFrame(
            {
                "symbol": ["600519"],
                "trade_date": [date(2024, 1, 2)],
                "close": [1688.0],
            }
        )
        return {"akshare": base, "ths": base.assign(close=[close_b])}

    async def test_injected_difference_is_reported_and_alerted(self):
        service, written, alerts, notified = self._service(self._frames())

        summary = await service.run(BATCH_ID, WINDOW)

        assert summary.verdict is Verdict.DEVIATION
        assert written and written[0].deviation_count == 1
        assert len(alerts) == 1 and alerts[0].alert is True
        assert notified == [summary]

    async def test_consistent_sources_write_a_summary_without_alerts(self):
        service, written, alerts, _ = self._service(self._frames(close_b=1688.0))

        summary = await service.run(BATCH_ID, WINDOW)

        assert summary.verdict is Verdict.CONSISTENT
        assert written and written[0].compared_keys == 1
        assert alerts and alerts[0].alert is False

    async def test_whitelisted_difference_still_reaches_the_notifier(self):
        service, _, alerts, notified = self._service(
            self._frames(), policy=AlertPolicy(whitelist={("stock_daily", "close")})
        )

        await service.run(BATCH_ID, WINDOW)

        assert alerts and alerts[0].alert is False
        assert "whitelist" in alerts[0].reason
        assert len(notified) == 1

    async def test_hook_uses_the_pipeline_context_batch(self):
        service, _, _, _ = self._service(self._frames())
        context = PipelineContext(
            domain="stock_daily", source="akshare", window=WINDOW, affected_keys=[]
        )

        summary = await service.run_hook(context)

        assert summary.domain == "stock_daily"
        assert summary.batch_id == f"xcheck:stock_daily:{WINDOW.label()}"

    async def test_hook_reads_both_sources_through_the_same_union_window(self):
        frames = self._frames(close_b=1688.0)
        service, _, _, _ = self._service(frames)
        reads: dict[str, tuple[tuple[str, ...] | None, dict[str, Window | None] | None]] = {}

        def reader_for(source):
            def read(window, *, symbols=None, symbol_windows=None):
                reads[source] = (symbols, symbol_windows)
                return frames[source].copy()

            return read

        service.readers = {source: reader_for(source) for source in frames}
        union = Window(start=date(2020, 1, 2), end=date(2024, 1, 31))
        context = PipelineContext(
            domain="stock_daily",
            source="akshare",
            window=union,
            affected_keys=[],
            symbols=("600519",),
            source_windows={
                "akshare": {"600519": None},
                "ths": {"600519": union},
            },
            comparison_windows={"600519": union},
            pipeline_id="resume-fingerprint",
        )

        await service.run_hook(context)

        expected = (("600519",), {"600519": union})
        assert reads == {"akshare": expected, "ths": expected}

    async def test_unknown_source_mapping_fails_closed(self):
        mapping = DomainMapping(
            domain="stock_daily",
            key=("symbol", "trade_date"),
            fields={"symbol": FieldMapping("symbol"), "trade_date": FieldMapping("trade_date")},
            adjust="unadjusted",
            suspension="absent_row",
            denominator="key_union",
        )
        service = CrossCheckService(
            "stock_daily",
            sources=("akshare", "ths"),
            mappings={"akshare": mapping},
            readers={
                "akshare": lambda window: pd.DataFrame(),
                "ths": lambda window: pd.DataFrame(),
            },
            write_report=lambda summary: 0,
            notifier=None,
        )
        with pytest.raises(LookupError, match="ths"):
            await service.run(BATCH_ID, WINDOW)

    async def test_partitioned_hook_aggregates_once_with_global_sample_cap(self):
        from opendata.pipeline.cross_check_service import hook_batch_id
        from opendata.pipeline.runner import PipelineContext

        symbols = tuple(f"{index:06d}" for index in range(105))
        frame_a = pd.DataFrame(
            {
                "symbol": symbols,
                "trade_date": [date(2024, 1, 2)] * len(symbols),
                "close": [float(index) + 10 for index in range(len(symbols))],
            }
        )
        frame_b = frame_a.assign(close=frame_a["close"] + 1.0).iloc[:-1].copy()
        windows = dict.fromkeys(symbols, WINDOW)
        calls: list[tuple[str, tuple[str, ...]]] = []

        def readers(frames):
            result = {}
            for source in ("akshare", "ths"):

                def read(window, *, symbols=None, symbol_windows=None, source=source):
                    selected = tuple(symbols or ())
                    calls.append((source, selected))
                    return frames[source].loc[frames[source]["symbol"].isin(selected)].copy()

                result[source] = read
            return result

        bounded_service, written, alerts, notified = self._service(
            {"akshare": frame_a, "ths": frame_b}
        )
        bounded_service.readers = readers({"akshare": frame_a, "ths": frame_b})
        children = []
        for offset in range(0, len(symbols), 50):
            batch = symbols[offset : offset + 50]
            children.append(
                PipelineContext(
                    domain="stock_daily",
                    source="akshare",
                    window=WINDOW,
                    affected_keys=[(symbol, date(2024, 1, 2)) for symbol in batch],
                    symbols=batch,
                    source_windows={"akshare": dict.fromkeys(batch, WINDOW)},
                    comparison_windows=dict.fromkeys(batch, WINDOW),
                    pipeline_id="bounded-cross-check",
                )
            )
        context = PipelineContext(
            domain="stock_daily",
            source="akshare",
            window=WINDOW,
            affected_keys=[],
            symbols=symbols,
            source_windows={"akshare": windows},
            comparison_windows=windows,
            pipeline_id="bounded-cross-check",
            partition_contexts=lambda: iter(children),
        )

        bounded = await bounded_service.run_hook(context)
        assert bounded.compared_keys == 105
        assert bounded.deviation_count == 104
        assert bounded.missing_count == 1
        assert bounded.per_field == {"close": 104}
        assert len(bounded.samples) == 100
        assert bounded.checked_at == CHECKED_AT
        assert len(written) == 1 and written[0] == bounded
        assert len(alerts) == len(notified) == 1
        assert notified[0] == bounded
        assert max(len(batch) for _, batch in calls) <= 50

        calls.clear()
        full_service, _, _, _ = self._service({"akshare": frame_a, "ths": frame_b})
        full_service.readers = readers({"akshare": frame_a, "ths": frame_b})
        full = await full_service.run(
            hook_batch_id(context),
            WINDOW,
            symbols=symbols,
            symbol_windows=windows,
        )
        assert bounded == full
