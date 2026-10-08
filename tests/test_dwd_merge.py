"""dwd merge tests (A4.6, design §8.3).

The merge layer takes the authority source row by row, falls back to
the next source when the authority has no row for a key (leaving
``source`` as evidence), never lets a non-authority value win a
numeric disagreement - it only raises ``_diff_flag`` - and stamps the
point-in-time columns (``_merged_at``, ``_as_of``). Single-source
domains run in passthrough mode so ``layer=dwd`` stays uniform.

The service's merge unit is the window plus the affected keys, which
is how a corrected old key (adjustment recompute, financial
restatement) propagates into dwd.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from opendata.pipeline.dwd_merge import (
    DwdMergeService,
    DwdWriter,
    MergeStats,
    merge_source_frames,
)

pytestmark = pytest.mark.integration
KEY = ("symbol", "trade_date")
AS_OF = date(2024, 1, 31)
MERGED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _frame(
    rows: list[tuple[str, date, float]], *, source_close_offset: float = 0.0
) -> pd.DataFrame:
    """A contract-shaped stock-daily frame (all dwd value columns)."""
    closes = [row[2] + source_close_offset for row in rows]
    return pd.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "trade_date": [row[1] for row in rows],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * len(rows),
            "amount": [1000.0] * len(rows),
        }
    )


AUTHORITY_ROWS = [("600519", date(2024, 1, 2), 1688.0), ("000001", date(2024, 1, 2), 9.5)]


class TestMergeSourceFrames:
    def test_authority_row_wins_and_is_recorded(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.rows == 2
        assert stats.degraded_rows == 0
        assert set(merged["source"]) == {"ths"}
        assert merged.loc[merged["symbol"] == "600519", "close"].iloc[0] == 1688.0

    def test_numeric_disagreement_keeps_the_authority_value_and_flags(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.diff_flagged == 2
        assert set(merged["_diff_flag"]) == {1}
        # Values stay the authority's, the difference is only marked.
        assert merged.loc[merged["symbol"] == "000001", "close"].iloc[0] == 9.5

    def test_missing_authority_row_degrades_to_the_next_source(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS[:1]),
                "akshare": _frame(AUTHORITY_ROWS),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.rows == 2
        assert stats.degraded_rows == 1
        degraded = merged.loc[merged["symbol"] == "000001"].iloc[0]
        assert degraded["source"] == "akshare"  # 留痕：降级来源可见
        assert degraded["_diff_flag"] == 0  # only one source had the row

    def test_extra_diff_keys_mark_rows_outside_the_disagreement(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {"ths": _frame(AUTHORITY_ROWS)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
            extra_diff_keys={("000001", date(2024, 1, 2))},
        )

        assert stats.diff_flagged == 1
        assert merged.loc[merged["symbol"] == "000001", "_diff_flag"].iloc[0] == 1

    def test_point_in_time_columns_are_stamped(self):
        merged, _ = merge_source_frames(
            "stock_daily",
            {"ths": _frame(AUTHORITY_ROWS)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert set(merged["_as_of"]) == {AS_OF}
        assert set(merged["_merged_at"]) == {MERGED_AT.replace(tzinfo=None)}

    def test_single_source_domain_is_passthrough(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {"akshare": _frame(AUTHORITY_ROWS)},
            authority=("akshare",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.passthrough is True
        assert stats.diff_flagged == 0
        assert set(merged["source"]) == {"akshare"}

    def test_unknown_authority_source_fails_closed(self):
        with pytest.raises(ValueError, match="authority"):
            merge_source_frames(
                "stock_daily",
                {"akshare": _frame(AUTHORITY_ROWS)},
                authority=("ths",),
                key=KEY,
                as_of=AS_OF,
                merged_at=MERGED_AT,
            )


def _reader_for(frames: dict[str, pd.DataFrame], name: str):
    """A reader honouring the contract: window rows plus affected keys."""

    def read(start: date, end: date, affected_keys: set[tuple]) -> pd.DataFrame:
        frame = frames[name].copy()
        for symbol, trade_date in affected_keys:
            present = ((frame["symbol"] == symbol) & (frame["trade_date"] == trade_date)).any()
            if not present:
                frame = pd.concat(
                    [frame, _frame([(symbol, trade_date, 1.0)])],
                    ignore_index=True,
                )
        return frame

    return read


class TestDwdMergeService:
    def _service(self, frames, **overrides):
        written: list[pd.DataFrame] = []
        readers = overrides.pop(
            "readers", {name: _reader_for(frames, name) for name in ("ths", "akshare")}
        )
        service = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths", "akshare"),
            readers=readers,
            write_dwd=lambda frame: written.append(frame) or len(frame),
            key=KEY,
            merged_at=MERGED_AT,
            **overrides,
        )
        return service, written

    async def test_window_merge_writes_the_merged_frame(self):
        service, written = self._service(
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=1.0),
            }
        )

        stats = await service.run(date(2024, 1, 1), date(2024, 1, 31))

        assert isinstance(stats, MergeStats)
        assert stats.rows == 2
        assert written and len(written[0]) == 2

    async def test_affected_keys_extend_the_merge_unit(self):
        """修订传播: a corrected older key is recomputed with the window."""
        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        )

        stats = await service.run(
            date(2024, 1, 1),
            date(2024, 1, 31),
            affected_keys={("600519", date(2023, 12, 29))},
        )

        assert stats.rows == 3  # window rows plus the affected key
        assert ("600519", date(2023, 12, 29)) in set(
            zip(written[0]["symbol"], written[0]["trade_date"], strict=True)
        )

    async def test_service_writes_all_four_point_in_time_faces(self):
        """AC-9 item 6: authority/degrade/source/_diff_flag/_as_of land via the service.

        ``test_point_in_time_columns_are_stamped`` drives ``merge_source_frames``
        directly, so it cannot see the ``as_of=end`` the service passes down -
        the column a reader of ``layer=dwd`` actually queries is stamped here.
        """
        service, written = self._service(
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=1.0),
            }
        )

        stats = await service.run(date(2024, 1, 1), AS_OF)

        frame = written[0].set_index(list(KEY))
        assert stats.rows == len(written[0]) == 2
        assert set(written[0]["_as_of"]) == {AS_OF}
        assert set(written[0]["source"]) == {"ths"}
        assert written[0]["_merged_at"].nunique() == 1
        disagreeing = {(row[0], row[1]) for row in AUTHORITY_ROWS}
        assert set(frame.loc[list(disagreeing), "_diff_flag"]) == {1}

    async def test_service_degrades_per_key_and_keeps_the_source_evidence(self):
        """AC-9 item 6 (second half): a missing authority row is filled, not lost."""
        service, written = self._service(
            {
                "ths": _frame(AUTHORITY_ROWS[:1]),
                "akshare": _frame(AUTHORITY_ROWS),
            }
        )

        stats = await service.run(date(2024, 1, 1), AS_OF)

        frame = written[0].set_index(list(KEY))
        assert stats.degraded_rows == 1
        assert frame.loc[("000001", date(2024, 1, 2)), "source"] == "akshare"
        assert frame.loc[("600519", date(2024, 1, 2)), "source"] == "ths"

    async def test_revision_of_an_existing_key_changes_the_dwd_row(self):
        """AC-9 item 7: a corrected ods key rewrites the dwd row it already had.

        Presence in the merge unit is proved by
        :meth:`test_affected_keys_extend_the_merge_unit`; this drives the rest
        of the claim - the row's *value* travels. The reader here honours the
        window (unlike :func:`_reader_for`), so the revised key only reaches
        dwd through ``affected_keys``: dropping that argument empties the
        second write, which is what makes the value change measurable instead
        of trivially re-read.
        """
        revised_key = ("600519", date(2023, 12, 29))
        frames = {
            "ths": _frame([revised_key + (5.0,), ("000001", date(2024, 1, 2), 9.5)]),
            "akshare": _frame([revised_key + (5.0,), ("000001", date(2024, 1, 2), 9.5)]),
        }
        written: list[pd.DataFrame] = []

        def reader(name: str):
            def read(start: date, end: date, affected_keys: set[tuple]) -> pd.DataFrame:
                frame = frames[name]
                keys = list(zip(frame["symbol"], frame["trade_date"], strict=True))
                in_window = (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
                restated = pd.Series([key in affected_keys for key in keys], index=frame.index)
                return frame[in_window | restated].reset_index(drop=True)

            return read

        service = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths", "akshare"),
            readers={name: reader(name) for name in ("ths", "akshare")},
            write_dwd=lambda frame: written.append(frame) or len(frame),
            key=KEY,
            merged_at=MERGED_AT,
        )

        await service.run(date(2023, 12, 1), date(2023, 12, 31))
        before = written[0].set_index(list(KEY))
        assert float(before.loc[revised_key, "close"]) == 5.0

        frames["ths"] = _frame([revised_key + (6.5,), ("000001", date(2024, 1, 2), 9.5)])
        stats = await service.run(date(2024, 1, 1), AS_OF, affected_keys={revised_key})

        after = written[-1].set_index(list(KEY))
        assert stats.rows == 2  # the new window plus the restated key
        assert float(after.loc[revised_key, "close"]) == 6.5
        assert after.loc[revised_key, "source"] == "ths"
        assert after.loc[revised_key, "_as_of"] == AS_OF

        await service.run(date(2024, 1, 1), AS_OF)
        assert revised_key not in set(
            zip(written[-1]["symbol"], written[-1]["trade_date"], strict=True)
        )

    async def test_key_can_come_from_the_source_mapping(self):
        from opendata.data.mapping import DomainMapping, FieldMapping

        frames = {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        written: list[pd.DataFrame] = []
        service = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths",),
            readers={name: _reader_for(frames, name) for name in ("ths", "akshare")},
            write_dwd=lambda frame: written.append(frame) or len(frame),
            mappings={
                "ths": DomainMapping(
                    domain="stock_daily",
                    key=KEY,
                    fields={
                        "symbol": FieldMapping("symbol"),
                        "trade_date": FieldMapping("trade_date"),
                        "close": FieldMapping("close"),
                    },
                    adjust="unadjusted",
                    suspension="absent_row",
                    denominator="key_union",
                )
            },
            merged_at=MERGED_AT,
        )

        stats = await service.run(date(2024, 1, 1), date(2024, 1, 31))

        assert stats.rows == 2

    async def test_missing_key_definition_fails_closed(self):
        frames = {"ths": _frame(AUTHORITY_ROWS)}
        service = DwdMergeService(
            "stock_daily",
            sources=("ths",),
            authority=("ths",),
            readers={"ths": _reader_for(frames, "ths")},
            write_dwd=lambda frame: len(frame),
            merged_at=MERGED_AT,
        )

        with pytest.raises(ValueError, match="business key"):
            await service.run(date(2024, 1, 1), date(2024, 1, 31))

    async def test_hook_consumes_the_pipeline_context(self):
        from opendata.pipeline.runner import PipelineContext, Window

        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        )
        context = PipelineContext(
            domain="stock_daily",
            source="akshare",
            window=Window(start=date(2024, 1, 1), end=date(2024, 1, 31)),
            affected_keys=[("600519", date(2024, 1, 2))],
        )

        stats = await service.run_hook(context)

        assert stats.rows == 2
        assert written

    async def test_hook_reads_both_sources_through_the_same_union_window(self):
        from opendata.pipeline.runner import PipelineContext, Window

        frames = {
            "ths": _frame(AUTHORITY_ROWS),
            "akshare": _frame(AUTHORITY_ROWS, source_close_offset=1.0),
        }
        reads: dict[str, tuple[tuple[str, ...] | None, dict[str, Window | None] | None]] = {}

        def reader_for(source):
            def read(start, end, affected_keys, *, symbols=None, symbol_windows=None):
                reads[source] = (symbols, symbol_windows)
                return frames[source].copy()

            return read

        service, written = self._service(
            frames,
            readers={source: reader_for(source) for source in frames},
        )
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

        stats = await service.run_hook(context)

        expected = (("600519",), {"600519": union})
        assert reads == {"ths": expected, "akshare": expected}
        assert stats.rows == len(written[0])

    async def test_the_hook_re_spells_the_affected_keys_so_the_flag_lands(self):
        """Step 2 reports the keys it wrote in the *source* spelling; the merge
        indexes by contract key. Handing the raw spelling straight to
        ``extra_diff_keys`` left a row whose symbol was ``600888.SH`` and
        never marked the ``600888`` key it was meant to mark."""
        from opendata.data.mapping import require_domain_mapping
        from opendata.pipeline.runner import PipelineContext, Window

        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)},
            mappings={
                source: require_domain_mapping(source, "stock_daily")
                for source in ("ths", "akshare")
            },
        )
        context = PipelineContext(
            domain="stock_daily",
            source="ths",
            window=Window(start=date(2024, 1, 1), end=date(2024, 1, 31)),
            affected_keys=[("600888.SH", date(2023, 12, 29))],
        )

        stats = await service.run_hook(context)

        merged = written[0]
        flags = dict(
            zip(
                zip(merged["symbol"], merged["trade_date"], strict=True),
                merged["_diff_flag"],
                strict=True,
            )
        )
        assert stats.rows == 3  # the window plus the affected key
        # Both sources carry the same values for that key, so nothing but the
        # affected-key set can explain the flag.
        assert flags[("600888", date(2023, 12, 29))] == 1
        assert "600888.SH" not in flags

    async def test_a_source_without_a_mapping_keeps_its_keys_as_given(self):
        """Nothing to translate through means pass through, not a guess."""
        from opendata.pipeline.runner import PipelineContext, Window

        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        )
        context = PipelineContext(
            domain="stock_daily",
            source="ths",
            window=Window(start=date(2024, 1, 1), end=date(2024, 1, 31)),
            affected_keys=[("600888", date(2023, 12, 29))],
        )

        await service.run_hook(context)

        assert ("600888", date(2023, 12, 29)) in {
            (row.symbol, row.trade_date) for row in written[0].itertuples()
        }

    async def test_partitioned_hook_matches_whole_merge_and_shares_run_stamps(self):
        from opendata.pipeline.runner import PipelineContext, Window

        symbols = tuple(f"{index:06d}" for index in range(105))
        active_day = date(2024, 1, 2)
        revised_key = (symbols[0], date(2023, 12, 29))
        authority_rows = [
            (symbol, active_day, float(index + 10)) for index, symbol in enumerate(symbols[:-1])
        ] + [(*revised_key, 4.0)]
        secondary_rows = [
            (symbol, active_day, float(index + 11)) for index, symbol in enumerate(symbols)
        ] + [(*revised_key, 4.0)]
        frames = {"ths": _frame(authority_rows), "akshare": _frame(secondary_rows)}
        window = Window(start=date(2024, 1, 1), end=date(2024, 1, 31))
        windows = dict.fromkeys(symbols, window)
        affected = [revised_key]
        children = [
            PipelineContext(
                domain="stock_daily",
                source="ths",
                window=window,
                affected_keys=[revised_key] if offset == 0 else [],
                symbols=batch,
                source_windows={"ths": dict.fromkeys(batch, window)},
                comparison_windows=dict.fromkeys(batch, window),
                pipeline_id="partitioned-merge",
            )
            for offset in range(0, len(symbols), 50)
            for batch in (symbols[offset : offset + 50],)
        ]

        def scoped_readers(read_log):
            readers = {}
            for source in ("ths", "akshare"):

                def read(
                    start,
                    end,
                    affected_keys,
                    *,
                    symbols=None,
                    symbol_windows=None,
                    source=source,
                ):
                    selected_symbols = tuple(symbols or ())
                    read_log.append((source, selected_symbols))
                    frame = frames[source]
                    in_symbols = (
                        pd.Series(True, index=frame.index)
                        if symbols is None
                        else frame["symbol"].isin(selected_symbols)
                    )
                    in_window = frame["trade_date"].between(start, end)
                    row_keys = list(zip(frame["symbol"], frame["trade_date"], strict=True))
                    revised = pd.Series(
                        [key in affected_keys for key in row_keys], index=frame.index
                    )
                    return frame.loc[in_symbols & (in_window | revised)].copy()

                readers[source] = read
            return readers

        bounded_reads = []
        bounded_writes = []
        bounded = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths", "akshare"),
            readers=scoped_readers(bounded_reads),
            write_dwd=lambda frame: bounded_writes.append(frame.copy()) or len(frame),
            key=KEY,
            merged_at=None,
        )
        bounded_context = PipelineContext(
            domain="stock_daily",
            source="ths",
            window=window,
            affected_keys=affected,
            symbols=symbols,
            source_windows={"ths": windows},
            comparison_windows=windows,
            pipeline_id="partitioned-merge",
            partition_contexts=lambda: iter(children),
        )
        bounded_stats = await bounded.run_hook(bounded_context)

        whole_reads = []
        whole_writes = []
        whole = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths", "akshare"),
            readers=scoped_readers(whole_reads),
            write_dwd=lambda frame: whole_writes.append(frame.copy()) or len(frame),
            key=KEY,
            merged_at=MERGED_AT,
        )
        whole_context = PipelineContext(
            domain="stock_daily",
            source="ths",
            window=window,
            affected_keys=affected,
            symbols=symbols,
            source_windows={"ths": windows},
            comparison_windows=windows,
            pipeline_id="partitioned-merge",
        )
        whole_stats = await whole.run_hook(whole_context)

        assert bounded_stats == whole_stats
        assert len(bounded_writes) == 3
        bounded_frame = pd.concat(bounded_writes, ignore_index=True).sort_values(
            list(KEY), ignore_index=True
        )
        whole_frame = whole_writes[0].sort_values(list(KEY), ignore_index=True)
        pd.testing.assert_frame_equal(
            bounded_frame.drop(columns=["_merged_at"]),
            whole_frame.drop(columns=["_merged_at"]),
        )
        assert (
            bounded_frame.loc[
                (bounded_frame["symbol"] == symbols[0])
                & (bounded_frame["trade_date"] == revised_key[1]),
                "_diff_flag",
            ].iloc[0]
            == 1
        )
        assert (
            bounded_frame.loc[bounded_frame["symbol"] == symbols[-1], "source"].iloc[0] == "akshare"
        )
        assert bounded_frame["_merged_at"].nunique() == 1
        assert set(bounded_frame["_as_of"]) == {window.end}
        assert max(len(batch) for _, batch in bounded_reads) <= 50


class TestAmbiguousKey:
    """A key the leg carries twice is refused and reported, never raised away.

    The live shape C49 measured: ``stock_action``/ths publishes 603883's
    2024-06-27 ex-date twice with two different dividend plans (0.16 cash, and
    0.5 cash plus 0.3 bonus). The table is keyed on ``(symbol, ex_date)``, so at
    most one of the two could ever be kept and nothing in the contract says
    which. The merge used to raise on it - which undelivered the whole window
    and left ``dwd_stock_action`` empty - so the refusal is per key and reported.
    """

    @staticmethod
    def _leg(rows: list[tuple[str, date, float]], *, duplicate_first: int = 0) -> pd.DataFrame:
        """The leg, with its first key carried ``duplicate_first + 1`` times."""
        frame = _frame(rows)
        extra: list[pd.DataFrame] = []
        for round_index in range(duplicate_first):
            copy = frame.iloc[:1].copy()
            copy["close"] = copy["close"] + 0.3 * (round_index + 1)
            copy["amount"] = copy["amount"] * (2 + round_index)
            extra.append(copy)
        return pd.concat([frame, *extra], ignore_index=True)

    def test_the_ambiguous_key_is_refused_and_the_rest_of_the_window_lands(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {"ths": self._leg(AUTHORITY_ROWS, duplicate_first=1)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        landed = list(zip(merged["symbol"], merged["trade_date"], strict=True))
        assert landed == [("000001", date(2024, 1, 2))]  # the unambiguous key still lands
        assert stats.rows == 1
        assert stats.passthrough is True
        assert len(stats.colliding) == 1
        assert "600519" in stats.colliding[0]
        assert "2024" in stats.colliding[0]

    def test_a_key_carried_three_times_is_still_refused_whole(self):
        """Refusing is not "keep the first": no member of the group is trusted."""
        merged, stats = merge_source_frames(
            "stock_daily",
            {"ths": self._leg(AUTHORITY_ROWS, duplicate_first=2)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.colliding == ("ths: ('600519', datetime.date(2024, 1, 2))",)
        assert stats.rows == 1
        assert "600519" not in set(merged["symbol"])

    def test_a_missing_key_column_still_fails_closed(self):
        """The column-level disagreement is not a row-level judgement to make."""
        leg = _frame(AUTHORITY_ROWS).drop(columns=["trade_date"])

        with pytest.raises(ValueError, match="lacks key columns"):
            merge_source_frames(
                "stock_daily",
                {"ths": leg},
                authority=("ths",),
                key=KEY,
                as_of=AS_OF,
                merged_at=MERGED_AT,
            )

    async def test_the_service_reports_the_refusal_it_landed_around(self):
        """The run publishes the rows it wrote, so the refused key has to be visible.

        The single-source shape is what ``stock_action`` really runs: no second leg
        covers for the refused key, and a run that reported 55,073 rows for a table
        that kept 55,071 would be reporting a landing that did not happen.
        """
        written: list[pd.DataFrame] = []
        frames = {"ths": self._leg(AUTHORITY_ROWS, duplicate_first=1)}
        service = DwdMergeService(
            "stock_daily",
            sources=("ths",),
            authority=("ths",),
            readers={"ths": _reader_for(frames, "ths")},
            write_dwd=lambda frame: written.append(frame) or len(frame),
            key=KEY,
            merged_at=MERGED_AT,
        )

        stats = await service.run(date(2024, 1, 1), date(2024, 1, 31))

        assert stats.rows == 1
        assert len(stats.colliding) == 1
        assert written and len(written[0]) == 1

    def test_the_multi_source_winner_is_not_a_collision(self):
        """Two sources carrying one key is the normal case, not an ambiguity."""
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.colliding == ()
        assert stats.rows == 2


class TestPassthroughQueryFace:
    """判据 |08 的查询面：直通输出要能被 ``layer=dwd`` 的读法原样读回去.

    The dwd table is built from the same contract model the query layer reads
    (:func:`opendata.pipeline.ddl.contract_columns` plus the trace columns), and
    the endpoint picks its key out of that model too (:func:`opendata.api.data_query._key`).
    If the passthrough frame's columns drifted from that shape the landing would
    fail on an unknown column, or ``layer=dwd`` would order and filter on a column
    the table never had - so both halves are asserted against the real single-source
    domain, without a database.
    """

    DOMAIN = "stock_action"

    def _passthrough_frame(self) -> pd.DataFrame:
        from opendata.pipeline.ddl import contract_columns

        rows: dict[str, list] = {}
        for column in contract_columns(self.DOMAIN):
            if column.name == "symbol":
                rows[column.name] = ["600519", "000001"]
            elif column.name in {"ex_date", "report_period", "as_of", "date"}:
                rows[column.name] = [date(2024, 6, 27), date(2024, 6, 28)]
            else:
                rows[column.name] = [0.16, 0.5]
        return pd.DataFrame(rows)

    def test_passthrough_columns_are_exactly_the_dwd_table_columns(self):
        from opendata.api.data_query import _key
        from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, contract_columns

        key = _key(self.DOMAIN)
        merged, stats = merge_source_frames(
            self.DOMAIN,
            {"ths": self._passthrough_frame()},
            authority=("ths",),
            key=key,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )
        table_columns = [
            column.name for column in [*contract_columns(self.DOMAIN), *DWD_TRACE_COLUMNS]
        ]

        assert stats.passthrough is True
        assert stats.rows == 2
        assert set(merged.columns) == set(table_columns)
        # 直通不留空差异位：单源没有第二条腿可比
        assert set(merged["_diff_flag"]) == {0}

    def test_layer_dwd_query_builds_over_the_landed_columns(self):
        from opendata.api.data_query import _key
        from opendata.data.domains import dwd_table
        from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, contract_columns
        from opendata.pipeline.query import DataQuery, build_data_select

        key = _key(self.DOMAIN)
        columns = [column.name for column in [*contract_columns(self.DOMAIN), *DWD_TRACE_COLUMNS]]
        merged, _ = merge_source_frames(
            self.DOMAIN,
            {"ths": self._passthrough_frame()},
            authority=("ths",),
            key=key,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        sql, params = build_data_select(
            DataQuery(domain=self.DOMAIN, layer="dwd", page_size=20),
            table=dwd_table(self.DOMAIN),
            columns=columns,
            key=key,
            today=date(2026, 9, 26),
        )

        assert f"`{dwd_table(self.DOMAIN)}`" in sql
        assert f"`{key[1]}`" in sql  # 时间字段就是 key 里的日期列
        assert params["limit"] == 20
        # 查询选的每一列都在落地帧里，直通不会读到一个没写过的列
        assert set(key).issubset(set(merged.columns))


class TestRecomputeIdempotence:
    """判据 |09：重算同一批输入，落库面必须逐格不动."""

    FRAMES = {
        "ths": _frame(AUTHORITY_ROWS),
        "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
    }

    def _merge(self, frames=None, *, merged_at=MERGED_AT):
        return merge_source_frames(
            "stock_daily",
            self.FRAMES if frames is None else frames,
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=merged_at,
        )

    def test_a_second_recompute_of_the_same_input_is_the_same_frame(self):
        first, first_stats = self._merge()
        second, second_stats = self._merge()

        pd.testing.assert_frame_equal(first, second)
        assert first_stats == second_stats

    def test_the_input_dictionary_order_does_not_change_the_output(self):
        shuffled, shuffled_stats = self._merge(
            {name: self.FRAMES[name] for name in reversed(list(self.FRAMES))}
        )
        ordered, ordered_stats = self._merge()

        pd.testing.assert_frame_equal(shuffled, ordered)
        assert shuffled_stats == ordered_stats

    def test_a_recompute_at_a_later_clock_moves_only_the_audit_stamp(self):
        """Recompute reproducibility: business columns stay put.

        ``_merged_at`` is the stamp of the run that wrote the row, so it is the
        one column a second recompute is allowed to change; a row whose close
        moved between recomputes would mean the merge read something it had not
        been given.
        """
        base, _ = self._merge()
        later, _ = self._merge(merged_at=datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))

        unchanged = [column for column in base.columns if column != "_merged_at"]
        pd.testing.assert_frame_equal(base[unchanged], later[unchanged])
        assert set(later["_merged_at"]) == {datetime(2026, 9, 24, 9, 0)}

    def test_landing_the_same_key_twice_is_one_row_written_in_place(self):
        """The write is a key-level upsert, so a recompute cannot add a row.

        The warehouse truth is the e2e node below; this is the face the gate
        runs: the statement DwdWriter really emits must carry the business key in
        its insert list and must not rewrite it in the update part, while every
        value column - and the point-in-time columns - are rewritten.
        """
        merged, _ = self._merge()
        engine = _RecordingEngine()

        written = DwdWriter(engine).write(merged, table="dwd_stock_daily", key=KEY)
        again = DwdWriter(engine).write(merged, table="dwd_stock_daily", key=KEY)

        assert (written, again) == (2, 2)
        assert len(engine.calls) == 2
        statement, records = engine.calls[0]
        assert engine.calls[1][0] == statement  # the same statement, not a second insert path
        assert "ON DUPLICATE KEY UPDATE" in statement
        assert len(records) == 2
        insert_list = statement.split("VALUES")[0]
        update_part = statement.split("ON DUPLICATE KEY UPDATE")[1]
        for column in KEY:
            assert f"`{column}`" in insert_list
            assert f"`{column}` = new" not in update_part
        for column in ("close", "volume", "source", "_as_of", "_diff_flag"):
            assert f"`{column}` = new" in update_part

    def test_an_empty_recompute_writes_nothing(self):
        engine = _RecordingEngine()
        merged = pd.DataFrame(
            {name: pd.Series(dtype="object") for name in (*KEY, "close", "source")}
        )

        assert DwdWriter(engine).write(merged, table="dwd_stock_daily", key=KEY) == 0
        assert engine.calls == []


class _RecordingConnection:
    """The connection face ``DwdWriter.write`` uses, recording what it sent."""

    def __init__(self, sink: list[tuple[str, list[dict]]]) -> None:
        self._sink = sink

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: object, records: list[dict]) -> None:
        """Record the batch the writer sent (duck-typed SQLAlchemy face)."""
        self._sink.append((str(statement), list(records)))


class _RecordingEngine:
    """An engine stub: ``begin()`` yields a connection that records."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    def begin(self) -> _RecordingConnection:
        """The transaction context ``DwdWriter.write`` opens."""
        return _RecordingConnection(self.calls)


@pytest.mark.e2e
class TestDwdWriteAgainstMysql:
    TABLE = "dwd_stock_daily"

    def test_merge_writes_rows_with_trace_columns_and_is_idempotent(self):
        from sqlalchemy import create_engine, pool, text

        from opendata.core.config import settings
        from opendata.pipeline.dwd_merge import DwdWriter

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        try:
            merged, _ = merge_source_frames(
                "stock_daily",
                {"ths": _frame(AUTHORITY_ROWS)},
                authority=("ths",),
                key=KEY,
                as_of=AS_OF,
                merged_at=MERGED_AT,
            )
            writer = DwdWriter(engine)
            writer.write(merged, table=self.TABLE, key=KEY)
            # Re-running the same merge must update in place.
            merged.loc[:, "close"] = merged["close"] + 1.0
            writer.write(merged, table=self.TABLE, key=KEY)

            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT symbol, close, source, _diff_flag, _as_of "
                        "FROM `dwd_stock_daily` WHERE symbol = '600519'"
                    )
                ).all()
            assert len(rows) == 1
            assert rows[0][0] == "600519"
            assert rows[0][1] == pytest.approx(1689.0)
            assert rows[0][2] == "ths"
            assert rows[0][3] == 0
            assert rows[0][4] == AS_OF
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM `dwd_stock_daily` WHERE symbol IN ('600519', '000001')")
                )
            engine.dispose()
