"""Source health patrol tests (B3.4 / AC-4 / AC-19).

Probes run against stub fetchers in a private registry - no real
network - covering the healthy/failing/verified-skipping branches, the
key configuration report and the HTTP surface.

The C16 guard cases are the load-bearing ones: they read the *real*
registry, so a newly verified leg without a working probe fails the
suite here instead of quietly weakening ``auto`` routing in production.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from opendata.data import registry as registry_module
from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ths.endpoints import FUND_ETF_DEPTH_DAYS
from opendata.data.registry import ProviderRegistry
from opendata.pipeline import patrol as patrol_module
from opendata.pipeline.key_health import (
    CLASS_CREDENTIAL_REJECTED,
    CLASS_PATROL_GAP,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_SOURCE_DEGRADED,
    CLASS_UNCLASSIFIED,
    LEVEL_ALERT,
    LEVEL_PRESENCE_ONLY,
    REDACTED_MARKER,
)
from opendata.pipeline.patrol import (
    PROBE_PARAMS,
    PROBE_RESOLVERS,
    PatrolProbeConfigError,
    PatrolProbeError,
    credential_health,
    first_live_contract,
    key_status,
    key_values,
    patrol,
    probe_params,
    rolling_fund_etf_params,
    rolling_futures_params,
    rolling_option_params,
)

#: A Key-in-query source writes the query, and so the credential, into the
#: message ``raise_for_status()`` raises. The patrol archives that message, so
#: the canary below is what proves nothing secret reaches a report or a payload.
CANARY = "CANARY-SECRET-do-not-render"
LEAKY_URL = f"https://api.stlouisfed.org/fred/series/observations?series_id=CPIA&api_key={CANARY}"
LEAKY_ORIGIN = "https://api.stlouisfed.org/fred/series/observations"


class StubQuery(QueryParams):
    symbol: str = ""
    asset_type: str = ""


class StubFetcher(Fetcher[StubQuery, object]):
    """Fetcher whose behaviour the test wires explicitly."""

    def __init__(
        self,
        capability: Capability,
        *,
        raise_on: Exception | None = None,
        returns: object = "ok",
        fails_first: int | None = None,
    ):
        self.capability = capability
        self.raise_on = raise_on
        self.returns = returns
        self.fails_first = fails_first
        self.calls = 0
        self.seen: dict[str, object] = {}

    def transform_query(self, **kwargs: object) -> StubQuery:
        return StubQuery(**kwargs)

    def extract_data(self, params: StubQuery, ctx: FetchContext) -> object:
        self.calls += 1
        self.seen = params.model_dump(exclude_defaults=True)
        blipped = self.fails_first is not None and self.calls > self.fails_first
        if self.raise_on is not None and not blipped:
            raise self.raise_on
        return "raw"

    def transform_data(self, raw: object, params: StubQuery) -> object:
        return self.returns


class StubCatalog:
    """Instrument-catalog stub the option probe reads instead of the net."""

    def __init__(self, rows: object):
        self.rows = rows

    def fetch(self, **kwargs: object) -> object:
        assert kwargs == {"asset_type": "options"}
        return self.rows


def _capability(domain: str, source: str = "ths", verified: bool = True) -> Capability:
    return Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="cn",
        source=source,
        verified=verified,
    )


def _registry(*fetchers: StubFetcher) -> ProviderRegistry:
    registry = ProviderRegistry()
    for fetcher in fetchers:
        registry.register(fetcher)
    return registry


def _allow_stub_ths_auto_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make local THS stubs credential-eligible without user environment state."""
    monkeypatch.setattr(registry_module, "_source_has_credentials", lambda source: source == "ths")


@pytest.fixture
def real_registry() -> ProviderRegistry:
    """The process registry with every bundled provider registered."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry()


@pytest.fixture
def offline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the option probe a stub catalog so no test hits the network."""
    rows = [
        _instrument("10011425.SH", date(2026, 12, 23)),
        _instrument("10011424.SH", date(2026, 10, 1)),
        _instrument("10011423.SH", None),
    ]
    monkeypatch.setattr(patrol_module, "_catalog_fetcher", lambda registry: StubCatalog(rows))


def _instrument(symbol: str, delist_date: date | None) -> object:
    class _Row:
        pass

    row = _Row()
    row.symbol = symbol
    row.delist_date = delist_date
    return row


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test waits out the real retry backoff."""
    monkeypatch.setattr(patrol_module, "PROBE_RETRY_BACKOFF", 0.0)


class TestPatrol:
    async def test_healthy_fetcher_is_reported_ok(self):
        registry = _registry(StubFetcher(_capability("stock_daily")))

        results = await patrol(registry)

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].domain == "stock_daily"
        assert results[0].error is None
        assert results[0].latency_ms >= 0
        assert results[0].rows == 2

    async def test_failing_fetcher_marks_the_source_unhealthy(self):
        fetcher = StubFetcher(_capability("stock_daily"), raise_on=RuntimeError("boom"))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "boom" in (results[0].error or "")
        # auto routing must skip the broken source now
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_unverified_capabilities_are_not_probed(self):
        registry = _registry(
            StubFetcher(_capability("stock_daily", verified=False), raise_on=RuntimeError("x"))
        )

        results = await patrol(registry)

        assert results == []

    async def test_explicit_selection_probes_only_the_selected_registered_leg(self):
        selected = StubFetcher(_capability("stock_daily"))
        excluded = StubFetcher(_capability("economy_cpi", source="ecb"))
        registry = _registry(selected, excluded)

        results = await patrol(registry, capabilities=[selected.capability])

        assert [(result.domain, result.source) for result in results] == [("stock_daily", "ths")]
        assert selected.calls == 1
        assert excluded.calls == 0

    async def test_one_selected_source_failure_does_not_stop_other_p0_legs(self):
        failed = StubFetcher(_capability("stock_daily"), raise_on=RuntimeError("source down"))
        healthy = StubFetcher(_capability("stock_action"))
        registry = _registry(failed, healthy)

        results = await patrol(registry, capabilities=[failed.capability, healthy.capability])

        assert [(result.domain, result.ok) for result in results] == [
            ("stock_daily", False),
            ("stock_action", True),
        ]
        assert failed.calls == 2
        assert healthy.calls == 1

    async def test_selected_rolling_probe_keeps_full_registry_for_metadata_lookup(
        self, offline_catalog: None
    ):
        selected = StubFetcher(_capability("option_daily"))
        registry = _registry(selected)

        results = await patrol(registry, capabilities=[selected.capability])

        assert len(results) == 1
        assert results[0].ok is True
        assert selected.seen["symbol"] == "10011425.SH"
        assert selected.calls == 1

    async def test_probe_uses_the_minimal_params(self):
        fetcher = StubFetcher(_capability("stock_daily"))
        registry = _registry(fetcher)

        await patrol(registry)

        assert fetcher.calls == 1
        assert fetcher.seen["symbol"] == "600519"

    async def test_empty_probe_counts_as_unhealthy(self):
        fetcher = StubFetcher(_capability("stock_daily"), returns=())
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "no rows" in (results[0].error or "")
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_missing_probe_is_reported_without_blaming_the_source(self, monkeypatch):
        """An unconfigured probe is the patrol's gap, not a broken source."""
        _allow_stub_ths_auto_route(monkeypatch)
        fetcher = StubFetcher(_capability("not_a_real_domain"))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "PatrolProbeConfigError" in (results[0].error or "")
        assert fetcher.calls == 0
        # routing health untouched: the source is still reachable
        assert registry.resolve("equity", "not_a_real_domain", source="auto") is fetcher


class TestProbeRetry:
    """One blip is retried; a retry that saved the leg is named out loud."""

    async def test_a_single_blip_is_retried_and_the_source_stays_routable(self, monkeypatch):
        _allow_stub_ths_auto_route(monkeypatch)
        fetcher = StubFetcher(
            _capability("stock_daily"),
            raise_on=RuntimeError("connection reset"),
            fails_first=1,
        )
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is True
        assert results[0].attempts == 2
        assert fetcher.calls == 2
        # The blip never took the source out of ``auto`` routing.
        assert registry.resolve("equity", "stock_daily", source="auto") is fetcher

    async def test_a_leg_that_only_passed_on_retry_is_reported_flaky(self):
        fetcher = StubFetcher(
            _capability("stock_daily"), raise_on=RuntimeError("429"), fails_first=1
        )

        results = await patrol(_registry(fetcher))

        assert results[0].flaky is True
        # A pass the retry rescued is not a clean pass: the first failure
        # stays in the report instead of being greened away.
        assert "429" in (results[0].error or "")

    async def test_a_second_failure_still_marks_the_source_unhealthy(self):
        fetcher = StubFetcher(_capability("stock_daily"), raise_on=RuntimeError("boom"))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert results[0].attempts == patrol_module.PROBE_ATTEMPTS
        assert fetcher.calls == patrol_module.PROBE_ATTEMPTS
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_a_clean_pass_never_retries(self):
        fetcher = StubFetcher(_capability("stock_daily"))

        results = await patrol(_registry(fetcher))

        assert (results[0].ok, results[0].flaky, results[0].attempts) == (True, False, 1)
        assert results[0].error is None
        assert fetcher.calls == 1

    async def test_a_probe_config_gap_is_never_retried(self):
        """Retrying cannot fix the patrol's own gap: attempt two reads the same table."""
        fetcher = StubFetcher(_capability("not_a_real_domain"))

        results = await patrol(_registry(fetcher))

        assert results[0].attempts == 1
        assert fetcher.calls == 0


def _leaky_error(status: int, reason: str) -> httpx.HTTPStatusError:
    """Build the failure a Key-in-query source raises through httpx.

    Args:
        status: HTTP status of the stub response.
        reason: Reason phrase the message carries.

    Returns:
        The exception with its response attached, which is how it arrives from
        a transport: the message names the full URL, query and credential
        included, because that is what ``raise_for_status()`` writes.
    """
    request = httpx.Request("GET", LEAKY_URL)
    response = httpx.Response(status, request=request, headers={"content-type": "text/plain"})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


class TestFailureGrading:
    """C28 guards for the seam between a probe failure and its grade.

    ``key_health`` owns the classification tables and is tested there. These
    cases cover what only the patrol can answer: that the catch site actually
    fills the two graded fields, that the message it archives is redacted, and
    that grading touches nothing else it did not touch before.
    """

    async def test_a_refused_key_is_graded_not_only_printed(self):
        fetcher = StubFetcher(
            _capability("stock_daily"), raise_on=_leaky_error(401, "Unauthorized")
        )

        results = await patrol(_registry(fetcher))

        assert results[0].failure_class == CLASS_CREDENTIAL_REJECTED
        # Metadata only, in both fields: the status and the host the Key was
        # sent to, never the query that carried it.
        assert results[0].attribution == f"status=401 {LEAKY_ORIGIN}"
        assert CANARY not in (results[0].error or "")

    async def test_a_probe_gap_is_graded_as_the_patrol_s_own(self):
        results = await patrol(_registry(StubFetcher(_capability("not_a_real_domain"))))

        assert results[0].failure_class == CLASS_PATROL_GAP
        # Nothing reached a source, so there is no attribution to name: the
        # patrol's own gap must not be dressed up as evidence about a Key.
        assert results[0].attribution is None

    async def test_a_rescued_leg_keeps_the_class_that_failed_first(self):
        fetcher = StubFetcher(
            _capability("stock_daily"),
            raise_on=_leaky_error(429, "Too Many Requests"),
            fails_first=1,
        )

        results = await patrol(_registry(fetcher))

        assert (results[0].ok, results[0].flaky) == (True, True)
        # A 429 the retry absorbed is quota consumed, not a clean pass.
        assert results[0].failure_class == CLASS_QUOTA_EXHAUSTED

    async def test_a_clean_leg_carries_no_grade(self):
        results = await patrol(_registry(StubFetcher(_capability("stock_daily"))))

        assert (results[0].failure_class, results[0].attribution) == (None, None)

    def test_key_values_reads_the_credential_fields_and_nothing_else(self):
        assert key_values(_settings(ths=CANARY, fred="")) == (CANARY,)

    async def test_a_failure_that_quotes_the_key_in_prose_is_scrubbed(self, monkeypatch):
        """The URL rule cannot catch a header, so the value pass has to.

        A failure shape that carries neither a code, a status nor a URL is the
        worst case for the report: the archived text is the whole story, and if
        the patrol archived it raw a credential would sit in an HTTP payload.
        """
        monkeypatch.setattr(patrol_module, "key_values", lambda: (CANARY,))
        fetcher = StubFetcher(
            _capability("stock_daily"),
            raise_on=RuntimeError(f"Authorization: Bearer {CANARY}"),
        )

        results = await patrol(_registry(fetcher))

        assert CANARY not in (results[0].error or "")
        assert REDACTED_MARKER in (results[0].error or "")
        # Scrubbing hides the value, not the failure: it stays unclassified and
        # therefore loud, rather than being smoothed into a known class.
        assert results[0].failure_class == CLASS_UNCLASSIFIED
        assert results[0].attribution == "RuntimeError"

    @pytest.mark.parametrize(
        ("status", "reason", "expected_class"),
        [
            (401, "Unauthorized", CLASS_CREDENTIAL_REJECTED),
            (429, "Too Many Requests", CLASS_QUOTA_EXHAUSTED),
            (503, "Service Unavailable", CLASS_SOURCE_DEGRADED),
        ],
        ids=["banned", "throttled", "degraded"],
    )
    async def test_grading_never_moves_the_routing_mark(
        self, status: int, reason: str, expected_class: str
    ):
        """Who a failure is blamed on is a report, not a routing rule.

        Before C28 every failure classed as one thing: unavailable. Each class
        added here has to leave that decision exactly where it was, or the
        graded plane would be quietly rerouting traffic.
        """
        fetcher = StubFetcher(_capability("stock_daily"), raise_on=_leaky_error(status, reason))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert (results[0].ok, results[0].failure_class) == (False, expected_class)
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_the_report_reads_the_legs_that_actually_ran(self):
        """The join is real: presence and these probe results, one call."""
        registry = _registry(
            StubFetcher(_capability("stock_daily"), raise_on=_leaky_error(401, "Unauthorized"))
        )
        results = await patrol(registry)

        health = credential_health(_settings(ths="t", fred="f"), results)

        assert health["ths"].level == LEVEL_ALERT
        assert health["ths"].classes[0][0] == CLASS_CREDENTIAL_REJECTED
        assert health["ths"].attributions == (f"status=401 {LEAKY_ORIGIN}",)
        # Configured, never seen to fail: presence-only, not healthy.
        assert health["fred"].level == LEVEL_PRESENCE_ONLY


class TestFieldCanary:
    """C25 guards: a column can hollow out inside a page of full size."""

    def test_shape_classes_a_column_by_how_much_of_it_was_filled(self):
        assert patrol_module.shape_of(5578, 0) == patrol_module.SHAPE_FULL
        # C14 accepted 9 of 5,578 rows without a listing date: a few records
        # missing a date is a normal catalog, not a collapse.
        assert patrol_module.shape_of(5578, 9) == patrol_module.SHAPE_FULL
        assert patrol_module.shape_of(5578, 265) == patrol_module.SHAPE_PARTIAL
        assert patrol_module.shape_of(5578, 5578) == patrol_module.SHAPE_HOLLOW

    def test_a_small_page_cannot_be_rescued_by_the_tolerance(self):
        """Every row empty is hollow even when the page is smaller than the tolerance."""
        assert patrol_module.shape_of(4, 4) == patrol_module.SHAPE_HOLLOW
        assert patrol_module.shape_of(20, 20) == patrol_module.SHAPE_HOLLOW

    def test_a_reading_that_counts_more_misses_than_rows_is_refused(self):
        """The patrol's own arithmetic bug must not read as a source problem."""
        with pytest.raises(ValueError):
            patrol_module.shape_of(10, 20)
        with pytest.raises(ValueError):
            patrol_module.shape_of(0, 0)

    def test_evaluate_canary_measures_the_watched_column(self):
        canary = patrol_module.FieldCanary(
            asset_type="a-share",
            field="list_date",
            allowed=frozenset({patrol_module.SHAPE_FULL}),
            reason="test",
        )
        rows = [_catalog_row(None)] * 50 + [_catalog_row(date(2001, 8, 27))] * 50

        reading = patrol_module.evaluate_canary(canary, rows)

        assert (reading.rows, reading.missing, reading.shape) == (
            100,
            50,
            patrol_module.SHAPE_PARTIAL,
        )
        assert reading.deviates is True

    def test_an_empty_page_is_unmeasurable_rather_than_hollow(self):
        """Zero rows is the row-count probe's business; a shape needs rows."""
        canary = patrol_module.FieldCanary(
            asset_type="a-share",
            field="list_date",
            allowed=frozenset({patrol_module.SHAPE_HOLLOW}),
            reason="test",
        )

        reading = patrol_module.evaluate_canary(canary, ())

        assert reading.shape == patrol_module.SHAPE_NO_READING
        assert reading.deviates is False

    def test_a_blank_string_counts_as_an_empty_cell(self):
        canary = patrol_module.FieldCanary(
            asset_type="a-share",
            field="name",
            allowed=frozenset({patrol_module.SHAPE_HOLLOW}),
            reason="test",
        )

        reading = patrol_module.evaluate_canary(canary, [_catalog_row(None, name="   ")])

        assert (reading.rows, reading.missing) == (1, 1)
        assert reading.shape == patrol_module.SHAPE_HOLLOW
        assert reading.deviates is False

    def test_a_shape_that_was_never_measured_is_the_alarm(self):
        reading = patrol_module.CanaryReading(
            asset_type="a-share-index",
            field="list_date",
            shape=patrol_module.SHAPE_HOLLOW,
            rows=1431,
            missing=1431,
            allowed=frozenset({patrol_module.SHAPE_FULL}),
        )

        assert reading.deviates is True

    def test_the_canary_table_cannot_describe_an_unroutable_leg(self):
        """A canary on a leg the patrol never probes is a dead judgement."""
        probe_keys = set(patrol_module.PROBE_PARAMS) | set(patrol_module.PROBE_RESOLVERS)

        assert set(patrol_module.FIELD_CANARIES) <= probe_keys

    def test_every_watched_column_is_a_field_of_the_contract(self):
        """A typo'd column would be swallowed forever as an unreadable page.

        ``_read_canary`` reports any failure to read as no-reading, so a field
        name that does not exist on the contract would look exactly like a
        network blip and the canary would never say anything.
        """
        from opendata.data.models import Instrument

        for canaries in patrol_module.FIELD_CANARIES.values():
            for canary in canaries:
                assert canary.field in Instrument.model_fields, canary
                assert canary.allowed and set(canary.allowed) <= {
                    patrol_module.SHAPE_FULL,
                    patrol_module.SHAPE_PARTIAL,
                    patrol_module.SHAPE_HOLLOW,
                }
                assert canary.reason

    def test_the_watched_columns_cover_the_collapse_c18_found(self):
        """AC-19's gap was specifically ``list_date`` on the catalog leg."""
        catalog = patrol_module.FIELD_CANARIES.get(("instrument", "ths"), ())

        assert any(c.field == "list_date" for c in catalog)


class TestPatrolCanaryWiring:
    """A deviation pages, and nothing else changes."""

    @pytest.fixture
    def hollow_list_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            patrol_module,
            "FIELD_CANARIES",
            {
                ("instrument", "ths"): (
                    patrol_module.FieldCanary(
                        asset_type="a-share",
                        field="list_date",
                        allowed=frozenset({patrol_module.SHAPE_FULL}),
                        reason="test",
                    ),
                ),
            },
        )

    async def test_a_hollow_column_is_reported_on_a_leg_that_probed_ok(
        self, hollow_list_date: None
    ):
        rows = [_catalog_row(None) for _ in range(4)]
        fetcher = StubFetcher(_capability("instrument"), returns=rows)

        results = await patrol(_registry(fetcher))

        assert results[0].ok is True
        assert results[0].rows == 4
        deviation = results[0].field_deviations[0]
        assert (deviation.field, deviation.shape, deviation.missing) == (
            "list_date",
            patrol_module.SHAPE_HOLLOW,
            4,
        )
        # One probe call, one canary call: the alarm costs the leg one read.
        assert fetcher.calls == 2

    async def test_two_watched_columns_of_one_page_are_read_once(
        self, hollow_list_date: None, monkeypatch: pytest.MonkeyPatch
    ):
        """The shapes must come from the same answer, not from two builds."""
        monkeypatch.setattr(
            patrol_module,
            "FIELD_CANARIES",
            {
                ("instrument", "ths"): (
                    patrol_module.FieldCanary(
                        asset_type="a-share",
                        field="list_date",
                        allowed=frozenset({patrol_module.SHAPE_FULL}),
                        reason="test",
                    ),
                    patrol_module.FieldCanary(
                        asset_type="a-share",
                        field="name",
                        allowed=frozenset({patrol_module.SHAPE_FULL}),
                        reason="test",
                    ),
                    patrol_module.FieldCanary(
                        asset_type="futures",
                        field="list_date",
                        allowed=frozenset({patrol_module.SHAPE_PARTIAL}),
                        reason="test",
                    ),
                ),
            },
        )
        fetcher = StubFetcher(_capability("instrument"), returns=[_catalog_row(None)])

        results = await patrol(_registry(fetcher))

        assert [r.field for r in results[0].canaries] == ["list_date", "name", "list_date"]
        # probe + one page per distinct asset type, not one per column.
        assert fetcher.calls == 3

    async def test_a_field_collapse_never_moves_the_routing_mark(
        self, hollow_list_date: None, monkeypatch: pytest.MonkeyPatch
    ):
        """One column disagreeing with itself is not "this source is down".

        C19 measured the a-share catalog answering from two builds that differ
        in exactly this column; pulling ths out of ``auto`` over it would take
        down ten healthy legs to report one hollow date.
        """
        _allow_stub_ths_auto_route(monkeypatch)
        fetcher = StubFetcher(_capability("instrument"), returns=[_catalog_row(None)])
        registry = _registry(fetcher)
        marked: list[object] = []
        monkeypatch.setattr(registry, "mark_unavailable", lambda broken: marked.append(broken))

        results = await patrol(registry)

        assert results[0].ok is True
        assert results[0].field_deviations
        assert marked == []
        assert registry.resolve("equity", "instrument", source="auto") is fetcher

    async def test_a_clean_column_produces_no_deviation(self, hollow_list_date: None):
        fetcher = StubFetcher(_capability("instrument"), returns=[_catalog_row(date(2024, 1, 2))])

        results = await patrol(_registry(fetcher))

        assert results[0].field_deviations == ()

    async def test_a_leg_that_did_not_probe_gets_no_canary_reading(self):
        """No pass, nothing to measure: the row-count failure already spoke."""
        fetcher = StubFetcher(_capability("instrument"), raise_on=RuntimeError("boom"))

        results = await patrol(_registry(fetcher))

        assert results[0].canaries == ()

    async def test_the_probe_latency_excludes_the_canary_reads(
        self, hollow_list_date: None, monkeypatch: pytest.MonkeyPatch
    ):
        """Cross-round patrol archives compare this number; keep its meaning."""
        clock = {"t": 1000.0}
        monkeypatch.setattr(patrol_module.time, "perf_counter", lambda: clock["t"])

        async def slow_page(fetcher: object, asset_type: str) -> tuple[object, None]:
            clock["t"] += 60_000.0
            return [_catalog_row(None)], None

        monkeypatch.setattr(patrol_module, "_read_page", slow_page)
        fetcher = StubFetcher(_capability("instrument"), returns=[_catalog_row(None)])

        results = await patrol(_registry(fetcher))

        assert results[0].latency_ms == 0.0
        assert results[0].field_deviations

    async def test_a_canary_that_cannot_be_read_is_not_an_alarm(self, hollow_list_date: None):
        """ "Could not look" is not "the data is wrong" (C24's three-way verdict)."""
        fetcher = StubFetcher(_capability("instrument"), raise_on=RuntimeError("boom"))

        readings = await patrol_module._run_canaries(fetcher, _capability("instrument"))

        assert readings[0].shape == patrol_module.SHAPE_NO_READING
        assert readings[0].deviates is False
        assert "boom" in (readings[0].error or "")


class _CatalogRow:
    """Duck-typed catalog row carrying only the columns a canary watches."""

    def __init__(self, list_date: date | None, *, name: str = "贵州茅台") -> None:
        self.list_date = list_date
        self.name = name
        self.symbol = "600519.SH"


def _catalog_row(list_date: date | None, *, name: str = "贵州茅台") -> _CatalogRow:
    return _CatalogRow(list_date, name=name)


class TestProbeParamCoverage:
    """C16 guards: probe params are keyed by (domain, source) and complete."""

    def test_probe_tables_match_the_verified_capabilities_exactly(
        self, real_registry: ProviderRegistry
    ):
        verified = {(c.domain, c.source) for c in real_registry.capabilities() if c.verified}

        assert set(PROBE_PARAMS) | set(PROBE_RESOLVERS) == verified
        assert set(PROBE_PARAMS) & set(PROBE_RESOLVERS) == set()

    def test_every_probe_validates_offline(
        self,
        real_registry: ProviderRegistry,
        offline_catalog: None,
    ):
        """Each verified capability must accept its own probe params.

        ``transform_query`` is the same validate stage the real probe runs,
        so a leg that lands verified without a usable probe fails here.
        """
        for capability in real_registry.capabilities():
            if not capability.verified:
                continue
            fetcher = real_registry.resolve(
                capability.asset_class, capability.domain, source=capability.source
            )
            params = probe_params(capability, real_registry, today=date(2026, 9, 25))

            query = fetcher.transform_query(**params)

            assert set(params) <= set(query.model_fields_set)

    @pytest.mark.parametrize(
        ("domain", "period", "series_id"),
        [
            ("economy_cpi", "1M", "CPIAUCSL"),
            ("economy_gdp", "1Q", "GDPC1"),
            ("economy_unemployment", "1M", "UNRATE"),
        ],
        ids=["cpi-us-monthly", "gdp-us-quarterly", "unemployment-us-monthly"],
    )
    def test_fred_probe_uses_the_official_series_and_us_market(
        self,
        real_registry: ProviderRegistry,
        domain: str,
        period: str,
        series_id: str,
    ) -> None:
        capability = real_registry.resolve("macro", domain, source="fred").capability
        params = probe_params(capability, real_registry, today=date(2026, 9, 25))

        assert capability.verified is True
        assert (capability.period, capability.market) == (period, "us")
        assert params["series_id"] == series_id


class TestRollingProbes:
    """Legs whose instrument expires - or whose window does - are named anew."""

    def test_futures_contract_is_the_lead_month_after_today(self):
        params = rolling_futures_params(date(2026, 9, 25))

        assert params["symbol"] == "CU2611.SHF"
        assert params["start_date"] == date(2026, 9, 4)
        assert params["end_date"] == date(2026, 9, 25)

    def test_futures_contract_rolls_over_the_year(self):
        assert rolling_futures_params(date(2026, 12, 30))["symbol"] == "CU2702.SHF"

    def test_option_contract_comes_from_the_catalog(self, offline_catalog: None):
        params = rolling_option_params(date(2026, 9, 25), _registry())

        assert params["symbol"] == "10011425.SH"
        assert params["end_date"] == date(2026, 9, 25)

    def test_the_etf_probe_window_rolls_with_the_probe_date(self):
        """标的固定、窗口不固定：这条通道的可答深度本身就是「今天 - 1827 天」。"""
        params = rolling_fund_etf_params(date(2026, 9, 25))

        assert params["symbol"] == "510300"
        assert params["start_date"] == date(2026, 9, 4)
        assert params["end_date"] == date(2026, 9, 25)

    def test_the_etf_probe_never_asks_past_the_rolling_depth(self):
        """判据是相对量：窗口一旦长过深度，腿会在发请求前自己拒答（C20）。"""
        for probe_day in (date(2026, 9, 25), date(2031, 1, 3), date(2038, 7, 20)):
            params = rolling_fund_etf_params(probe_day)

            assert (probe_day - params["start_date"]).days < FUND_ETF_DEPTH_DAYS

    def test_pick_skips_contracts_expiring_inside_the_horizon(self):
        rows = [
            _instrument("10011425.SH", date(2026, 12, 23)),
            _instrument("10011420.SH", date(2026, 10, 1)),
            _instrument("10011421.SH", None),
        ]

        assert first_live_contract(rows, date(2026, 9, 25)) == "10011425.SH"

    def test_pick_raises_when_nothing_lives_on(self):
        rows = [_instrument("10011420.SH", date(2026, 10, 1))]

        with pytest.raises(PatrolProbeError):
            first_live_contract(rows, date(2026, 9, 25))


class _NotRoutable(ProviderRegistry):
    """Registry that advertises a verified capability it cannot route to."""

    def resolve(self, *args: object, **kwargs: object) -> Fetcher[Any, Any]:
        raise LookupError("no registered capability")


class TestProbePlumbing:
    """The code that assembles a probe, not only the tables it reads."""

    def test_the_option_probe_uses_the_registered_catalog_leg(self, real_registry):
        catalog = next(
            c
            for c in real_registry.capabilities()
            if c.domain == "instrument" and c.source == "ths"
        )
        expected = real_registry.resolve(catalog.asset_class, "instrument", source="ths")

        assert patrol_module._catalog_fetcher(real_registry) is expected

    def test_a_missing_catalog_leg_is_the_patrols_own_gap(self):
        with pytest.raises(PatrolProbeConfigError):
            patrol_module._catalog_fetcher(ProviderRegistry())

    def test_the_option_resolver_falls_back_to_the_process_registry(
        self, offline_catalog: None, real_registry: ProviderRegistry
    ):
        params = rolling_option_params(date(2026, 9, 25))

        assert params["symbol"] == "10011425.SH"

    async def test_an_unroutable_capability_is_skipped_not_blamed(self):
        registry = _NotRoutable()
        registry.register(StubFetcher(_capability("stock_daily")))

        assert await patrol(registry) == []

    def test_run_patrol_is_the_sync_entry_a_cron_calls(self, monkeypatch):
        async def fake_patrol(*args: object) -> list[str]:
            return ["probed"]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)

        assert patrol_module.run_patrol() == ["probed"]


class TestKeyStatus:
    def test_ths_requires_a_key(self, monkeypatch):
        keys = key_status(_settings(ths="", fred=""))

        assert keys["ths"]["required"] is True
        assert keys["ths"]["configured"] is False

    def test_configured_keys_are_reported(self, monkeypatch):
        keys = key_status(_settings(ths="t", fred="f"))

        assert keys["ths"]["configured"] is True
        assert keys["fred"]["configured"] is True


def _settings(ths: str, fred: str):
    class _S:
        fuyao_api_key = ths
        fred_api_key = fred

    return _S()


@pytest.mark.asyncio
class TestHealthApi:
    async def test_sources_endpoint_requires_auth(self, test_client):
        response = await test_client.get("/api/v1/health/sources")

        assert response.status_code == 401

    async def test_sources_endpoint_lists_credentials(
        self, test_client, test_user_token, monkeypatch
    ):
        monkeypatch.setattr("opendata.api.pipeline.key_status", lambda: _status("t", "f"))
        response = await test_client.get(
            "/api/v1/health/sources",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        sources = response.json()["data"]["sources"]
        assert sources["ths"]["configured"] is True

    async def test_sources_endpoint_grades_keys_without_rendering_one(
        self, test_client, test_user_token, monkeypatch
    ):
        """The read-only health page carries levels, never values."""
        monkeypatch.setattr("opendata.api.pipeline.key_status", lambda: _status("t", "f"))
        monkeypatch.setattr(patrol_module, "key_status", lambda settings=None: _status("t", "f"))
        response = await test_client.get(
            "/api/v1/health/sources",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        data = response.json()["data"]
        ths = data["credential_health"]["ths"]
        # A Key that was merely found in the settings is not a Key that works:
        # the row says presence-only, names what no check ruled out, and says so
        # in the note the operator reads.
        assert (data["sources"]["ths"]["configured"], ths["level"]) == (True, LEVEL_PRESENCE_ONLY)
        assert "issuer 未提供到期日" in ths["note"]
        assert "状态未知" in ths["note"]
        assert "本轮没有 401/403/429 观测或主动验证" in ths["note"]
        assert "凭据有效性与剩余配额未知" in ths["note"]
        assert "Key 有效" not in ths["note"]
        assert "key-expiry" in ths["unverified"]
        assert CANARY not in response.text
        assert "api_key" not in response.text

    async def test_patrol_endpoint_grades_the_leg_that_failed(
        self, test_client, test_user_token, monkeypatch
    ):
        """The alert a caller reads carries the class, not the Key.

        This is the whole path a credential can leak by: the source's own
        message, archived into ``error``, summarised into ``attribution``, and
        graded into a note. All three are rendered here and checked.
        """
        registry = _registry(
            StubFetcher(_capability("stock_daily"), raise_on=_leaky_error(401, "Unauthorized"))
        )
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        response = await test_client.post(
            "/api/v1/health/patrol",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        data = response.json()["data"]
        leg = next(iter(data["results"]))
        assert (leg["ok"], leg["failure_class"]) == (False, CLASS_CREDENTIAL_REJECTED)
        assert leg["attribution"] == f"status=401 {LEAKY_ORIGIN}"
        assert data["credential_health"]["ths"]["level"] == LEVEL_ALERT
        assert data["credential_health"]["ths"]["owner"] == "凭据负责人"
        assert CANARY not in response.text
        assert "api_key" not in response.text

    async def test_manual_patrol_feeds_the_passive_key_health_store(
        self, test_client, test_user_token, monkeypatch
    ):
        """The manual probe producer records only its sanitized class/status."""
        from opendata.pipeline.key_health_notifications import (
            _OBSERVATIONS,
            recent_patrol_observations,
        )

        _OBSERVATIONS.clear()
        registry = _registry(
            StubFetcher(_capability("stock_daily"), raise_on=_leaky_error(429, "Too Many Requests"))
        )
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        try:
            response = await test_client.post(
                "/api/v1/health/patrol",
                headers={"Authorization": f"Bearer {test_user_token}"},
            )

            snapshot = recent_patrol_observations()
            assert response.status_code == 200
            assert len(snapshot.observations) == 1
            assert snapshot.observations[0].source == "ths"
            assert snapshot.observations[0].failure_class == CLASS_QUOTA_EXHAUSTED
            assert snapshot.observations[0].attribution == "status=429"
            assert CANARY not in str(snapshot.observations)
        finally:
            _OBSERVATIONS.clear()

    async def test_patrol_endpoint_reports_probe_results(
        self, test_client, test_user_token, monkeypatch
    ):
        registry = _registry(StubFetcher(_capability("stock_daily")))
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        response = await test_client.post(
            "/api/v1/health/patrol",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["count"] == 1
        assert data["healthy"] == 1
        assert data["flaky"] == 0

    async def test_patrol_endpoint_reports_a_rescued_leg_as_flaky(
        self, test_client, test_user_token, monkeypatch
    ):
        fetcher = StubFetcher(
            _capability("stock_daily"), raise_on=RuntimeError("timeout"), fails_first=1
        )
        registry = _registry(fetcher)
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        response = await test_client.post(
            "/api/v1/health/patrol",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        leg = next(iter(response.json()["data"]["results"]))
        # Routed as healthy, reported as flaky: the two are not the same claim.
        assert (leg["ok"], leg["attempts"], leg["flaky"]) == (True, 2, True)
        assert response.json()["data"]["flaky"] == 1

    async def test_patrol_endpoint_reports_a_hollow_column_on_a_healthy_leg(
        self, test_client, test_user_token, monkeypatch
    ):
        """AC-19's gap was a column the payload had no field for at all."""
        monkeypatch.setattr(
            patrol_module,
            "FIELD_CANARIES",
            {
                ("instrument", "ths"): (
                    patrol_module.FieldCanary(
                        asset_type="a-share",
                        field="list_date",
                        allowed=frozenset({patrol_module.SHAPE_FULL}),
                        reason="test",
                    ),
                ),
            },
        )
        registry = _registry(StubFetcher(_capability("instrument"), returns=[_catalog_row(None)]))
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        response = await test_client.post(
            "/api/v1/health/patrol",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        data = response.json()["data"]
        leg = next(iter(data["results"]))
        # The leg is still counted healthy; the column is its own report.
        assert (data["healthy"], leg["ok"], data["field_deviations"]) == (1, True, 1)
        assert leg["canaries"][0]["shape"] == patrol_module.SHAPE_HOLLOW
        assert leg["canaries"][0]["deviates"] is True


async def _patrol_done(registry: ProviderRegistry):
    return await patrol(registry)


def _status(ths: str, fred: str):
    return {
        "ths": {"required": True, "configured": bool(ths), "endpoint": "fuyao.aicubes.cn"},
        "fred": {"required": True, "configured": bool(fred), "endpoint": "api.stlouisfed.org"},
    }
