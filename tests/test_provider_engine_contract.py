"""Offline contract suite for engine-driven provider models.

Every case here is a judgement the acceptance record asks of *each* provider
model (AC2-10: real extraction plus positive and negative offline cases;
AC2-16: async applicability declared before I/O). Running them against one
declaration proves the engine; running them against every registered engine
declaration proves the models without writing one bespoke test per model.

No case can reach a network: extraction goes through a synthetic transport that
fails loudly on an unkeyed request, and the only assertions about sending are
the number and exact identity of the recorded requests.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SyntheticTransport,
    fixture_context,
)
from opendata.data.request_budget import RequestAuthorizationError, RequestBudget

SOURCE = "engine"
URL = "https://engine.test/records"
DEMO_COLUMNS = (
    ColumnSpec(name="date", kind="date", source_key="d", required=True, nullable=False),
    ColumnSpec(name="value", kind="float", source_key="v", required=True, nullable=False),
    ColumnSpec(name="label", kind="str", source_key="l", required=False),
)
DEMO_PARAMS = (
    ParamSpec(name="start_date", kind="date", query_key="from", required=True),
    ParamSpec(name="end_date", kind="date", query_key="to"),
    ParamSpec(name="frequency", kind="enum", enum=("daily", "weekly")),
    ParamSpec(name="limit", kind="int", default=2),
)
UNPAGED = PaginationSpec(kind="none")


def demo_spec(**overrides: object) -> ModelSpec:
    """One small, fully declared model: enough to judge every engine semantic."""
    fields: dict[str, object] = {
        "model": "EngineDemo",
        "domain": "engine_demo",
        "asset_class": "macro",
        "period": "d",
        "market": "us",
        "base_url": "https://engine.test",
        "path": "/records",
        "params": DEMO_PARAMS,
        "columns": DEMO_COLUMNS,
        "rows_pointer": "data.records",
        "pagination": PaginationSpec(kind="page", limit_key="limit", offset_key="page"),
        "scenario": "offline engine contract verification",
        "error_prefix": "ENGINE",
    }
    fields.update(overrides)
    return ModelSpec(**fields)  # type: ignore[arg-type]


def paged(records: list[dict[str, object]], **envelope: object) -> HttpResponse:
    """Wrap records in the declared envelope shape."""
    return HttpResponse(200, {"data": {"records": records}, **envelope})


def record(index: int) -> dict[str, object]:
    """One raw record using source keys rather than contract names."""
    return {"d": f"2026-01-{index + 1:02d}", "v": float(index) + 0.5, "l": f"r{index}"}


def demo_fetcher(spec: ModelSpec):
    """Return a fresh instance of the fetcher one declaration produces."""
    return make_http_json_fetcher(SOURCE, spec)()


@pytest.fixture
def send_through(monkeypatch: pytest.MonkeyPatch):
    """Route every engine send through a test-provided synthetic transport."""

    def _patch(transport):
        monkeypatch.setattr(http_json, "_http_get_json", transport)
        return transport

    return _patch


def raised_code(call, *args, **kwargs) -> str:
    """Return the stable code a failing engine call reported."""
    with pytest.raises(ProviderEngineError) as raised:
        call(*args, **kwargs)
    return str(raised.value.code)


class TestDeclarationIsExecutable:
    """A declaration must become a fetcher that reports its own identity."""

    def test_capability_and_identity_come_from_the_declaration(self) -> None:
        fetcher = demo_fetcher(demo_spec())
        assert fetcher.capability.source == SOURCE
        assert fetcher.capability.domain == "engine_demo"
        assert fetcher.capability.asset_class == "macro"
        assert fetcher.capability.period == "d"
        assert fetcher.capability.market == "us"
        assert fetcher.capability.verified is False
        assert fetcher.canonical_model == "EngineDemo"
        assert fetcher.async_mode == "bounded_thread"

    def test_query_class_applies_declared_defaults(self) -> None:
        query = demo_fetcher(demo_spec()).transform_query(start_date=date(2026, 1, 1))
        assert query.start_date == date(2026, 1, 1)
        assert query.end_date is None
        assert query.limit == 2

    def test_query_class_refuses_undeclared_parameters(self) -> None:
        with pytest.raises(ProviderEngineError) as raised:
            demo_fetcher(demo_spec()).transform_query(start_date=date(2026, 1, 1), nope=1)
        assert raised.value.code == "ENGINE_QUERY_INVALID"

    def test_async_applicability_is_declared_before_io(self) -> None:
        assert demo_fetcher(demo_spec(async_mode="unsupported")).async_mode == "unsupported"
        with pytest.raises(ValueError, match="async_mode"):
            demo_spec(async_mode="native_async")


class TestDeclarationRejectsMalformedInput:
    """A model that cannot be reviewed cannot be constructed at all."""

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"columns": ()}, "without declared columns"),
            ({"scenario": " "}, "without a scenario"),
            ({"base_url": "http://engine.test"}, r"https origin"),
            ({"path": "/records?x=1"}, "bare absolute path"),
            ({"error_prefix": "engine"}, "upper-case"),
            ({"credential": "API KEY"}, "environment variable"),
            ({"credential": "KEY"}, "must declare its query key"),
        ],
    )
    def test_model_spec_rejects(self, overrides: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            demo_spec(**overrides)

    def test_duplicate_and_keyword_idents_are_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate column"):
            demo_spec(columns=(DEMO_COLUMNS[0], DEMO_COLUMNS[0]))
        with pytest.raises(ValueError, match="identifier"):
            ColumnSpec(name="class")
        with pytest.raises(ValueError, match="optional but declared non-nullable"):
            ColumnSpec(name="value", required=False, nullable=False)
        with pytest.raises(ValueError, match="cursor_key"):
            PaginationSpec(kind="cursor")


class TestPaginationAndCompleteness:
    """Paging keeps every raw page and refuses to call a partial set whole."""

    def test_six_rows_are_four_exact_sends(self, send_through) -> None:
        """Three full pages plus one short probe page, each request exactly keyed.

        A page-based endpoint cannot report its last page as short, so the
        engine probes until a page returns fewer rows than the declared page
        size; the fourth send is that probe, and it is asserted rather than
        tolerated.
        """
        keyed: dict[tuple[str, frozenset], HttpResponse] = {}
        for page_number in ("1", "2", "3"):
            index = int(page_number) - 1
            params = {"from": "2026-01-01", "limit": "2", "page": page_number}
            keyed[(URL, frozenset(params.items()))] = paged(
                [record(index * 2), record(index * 2 + 1)]
            )
        probe: dict[str, str] = {"from": "2026-01-01", "limit": "2", "page": "4"}
        keyed[(URL, frozenset(probe.items()))] = paged([])
        transport = send_through(SyntheticTransport(keyed))
        spec = demo_spec()
        rows = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec, sends=4),
            start_date=date(2026, 1, 1),
        )
        assert [row.value for row in rows] == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
        assert [row.date for row in rows] == [date(2026, 1, n) for n in range(1, 7)]
        assert len(transport.calls) == 4
        assert [call["params"]["page"] for call in transport.calls] == ["1", "2", "3", "4"]

    def test_unkeyed_request_fails_instead_of_reusing_a_fixture(self, send_through) -> None:
        transport = send_through(
            SyntheticTransport({(URL, frozenset({("from", "2026-01-02")})): paged([])})
        )
        spec = demo_spec(pagination=UNPAGED)
        with pytest.raises(AssertionError, match="no synthetic response is keyed"):
            demo_fetcher(spec).fetch(ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1))
        assert len(transport.calls) == 1

    def test_page_bound_overflow_is_incomplete_not_truncated(self, send_through) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0), record(1)])))
        spec = demo_spec(
            pagination=PaginationSpec(
                kind="page", limit_key="limit", offset_key="page", max_pages=2
            )
        )
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec, sends=5),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_INCOMPLETE"
        assert len(transport.calls) == 2

    def test_declared_total_must_match_the_rows_returned(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([record(0), record(1)], total=5)))
        spec = demo_spec(pagination=PaginationSpec(kind="none", total_key="total"))
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_INCOMPLETE"

    def test_declared_total_that_matches_passes(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([record(0), record(1)], total=2)))
        spec = demo_spec(pagination=PaginationSpec(kind="none", total_key="total"))
        rows = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1)
        )
        assert len(rows) == 2


class TestEmptyVersusMissing:
    """An empty result is a valid answer; a missing shape is a failure."""

    def test_empty_record_list_is_zero_rows_not_an_error(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([])))
        spec = demo_spec(pagination=UNPAGED)
        assert (
            demo_fetcher(spec).fetch(ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1))
            == ()
        )

    def test_absent_pointer_is_shape_invalid(self, send_through) -> None:
        send_through(FixedResponseTransport(HttpResponse(200, {"data": {}})))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"

    def test_null_pointer_is_shape_invalid_not_empty(self, send_through) -> None:
        send_through(FixedResponseTransport(HttpResponse(200, {"data": {"records": None}})))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"

    def test_records_instead_of_envelope_is_shape_invalid(self, send_through) -> None:
        send_through(FixedResponseTransport(HttpResponse(200, [record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"


class TestRejectionsBeforeAnySend:
    """Nothing may be sent for a request the declaration already refuses."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"end_date": date(2026, 1, 2)},
            {"start_date": "not-a-date"},
            {"start_date": 7},
            {"start_date": date(2026, 1, 1), "frequency": "monthly"},
            {"start_date": date(2026, 1, 1), "undeclared": 1},
            {"start_date": date(2026, 1, 1), "limit": "many"},
        ],
        ids=[
            "missing-required",
            "bad-date-type",
            "wrong-type",
            "out-of-enum",
            "undeclared",
            "bad-int",
        ],
    )
    def test_query_rejection_sends_nothing(self, send_through, kwargs: dict) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(demo_fetcher(spec).fetch, ctx=fixture_context(SOURCE, spec), **kwargs)
        assert code == "ENGINE_QUERY_INVALID"
        assert transport.calls == []

    def test_foreign_query_object_sends_nothing(self, send_through) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).extract_data,
            {"from": "2026-01-01"},
            fixture_context(SOURCE, spec),
        )
        assert code == "ENGINE_QUERY_INVALID"
        assert transport.calls == []

    def test_missing_credential_sends_nothing(self, send_through, monkeypatch) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        monkeypatch.delenv("ENGINE_DEMO_KEY", raising=False)
        spec = demo_spec(
            credential="ENGINE_DEMO_KEY", credential_query_key="apikey", pagination=UNPAGED
        )
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_CREDENTIAL_MISSING"
        assert transport.calls == []

    def test_configured_credential_travels_in_the_request(self, send_through, monkeypatch) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        monkeypatch.setenv("ENGINE_DEMO_KEY", " secret ")
        spec = demo_spec(
            credential="ENGINE_DEMO_KEY", credential_query_key="apikey", pagination=UNPAGED
        )
        demo_fetcher(spec).fetch(ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1))
        assert transport.calls[0]["params"]["apikey"] == "secret"

    def test_no_grant_configured_sends_nothing(self, send_through) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        denied = FetchContext(timeout=5.0, request_budget=RequestBudget())
        fetcher = demo_fetcher(spec)
        with pytest.raises(RequestAuthorizationError):
            fetcher.fetch(ctx=denied, start_date=date(2026, 1, 1))
        assert transport.calls == []

    def test_grant_for_another_model_sends_nothing(self, send_through) -> None:
        transport = send_through(FixedResponseTransport(paged([record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        context = fixture_context(SOURCE, demo_spec(model="EngineOther"), sends=1)
        fetcher = demo_fetcher(spec)
        with pytest.raises(RequestAuthorizationError):
            fetcher.fetch(ctx=context, start_date=date(2026, 1, 1))
        assert transport.calls == []


class TestUpstreamStatusClassification:
    """Each upstream status maps onto one stable, attributable code."""

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (400, "ENGINE_QUERY_REJECTED"),
            (401, "ENGINE_CREDENTIAL_REJECTED"),
            (403, "ENGINE_CREDENTIAL_REJECTED"),
            (404, "ENGINE_RESOURCE_MISSING"),
            (422, "ENGINE_QUERY_REJECTED"),
            (429, "ENGINE_RATE_LIMITED"),
            (500, "ENGINE_UPSTREAM_UNAVAILABLE"),
            (503, "ENGINE_UPSTREAM_UNAVAILABLE"),
            (418, "ENGINE_HTTP_ERROR"),
        ],
    )
    def test_status_maps_to_attributable_code(self, send_through, status: int, code: str) -> None:
        send_through(FixedResponseTransport(HttpResponse(status, {"error": "no"})))
        spec = demo_spec(pagination=UNPAGED)
        with pytest.raises(ProviderEngineError) as raised:
            demo_fetcher(spec).fetch(ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1))
        assert raised.value.code == code
        assert raised.value.status == status
        assert raised.value.url == URL

    def test_non_json_body_is_bad_response(self, monkeypatch) -> None:
        from opendata.data import http_client

        class _Raw:
            status_code = 200
            text = "<html>not json</html>"

        class _Client:
            def get(self, *args, **kwargs):
                return _Raw()

        monkeypatch.setattr(http_client, "get_shared_http_client", lambda: _Client())
        with pytest.raises(ProviderEngineError) as raised:
            http_json._http_get_json(URL, {}, timeout=1.0, source="engine")
        assert raised.value.code == "ENGINE_BAD_RESPONSE"


class TestColumnFidelity:
    """Rows expose exactly the declared columns and never invent a value."""

    def test_row_exposes_exactly_declared_columns(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([{"d": "2026-01-01", "v": 1.0, "junk": 9}])))
        spec = demo_spec(pagination=UNPAGED)
        (row,) = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1)
        )
        assert set(row.model_dump()) == {"date", "value", "label"}
        assert row.date == date(2026, 1, 1)
        assert row.label is None
        assert "junk" not in row.model_dump()

    def test_required_key_absent_is_shape_invalid(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([{"d": "2026-01-01"}])))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"

    def test_published_null_stays_null_for_nullable_column(self, send_through) -> None:
        send_through(FixedResponseTransport(paged([{"d": "2026-01-01", "v": None}])))
        spec = demo_spec(
            columns=(
                ColumnSpec(name="date", kind="date", source_key="d", required=True, nullable=False),
                ColumnSpec(name="value", kind="float", source_key="v", required=True),
            ),
            pagination=UNPAGED,
        )
        (row,) = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1)
        )
        assert row.value is None

    @pytest.mark.parametrize(
        "raw",
        [
            {"d": "2026-01-01", "v": "abc"},
            {"d": "01-2026", "v": 1.0},
            {"d": "2026-01-01", "v": True},
            "not-a-record",
        ],
        ids=["non-numeric", "bad-date", "bool-for-float", "record-not-mapping"],
    )
    def test_type_or_shape_violation_is_shape_invalid(self, send_through, raw) -> None:
        send_through(FixedResponseTransport(paged([raw] if isinstance(raw, dict) else [{}])))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"

    def test_non_finite_float_is_refused_not_rounded(self, send_through) -> None:
        body = json.loads('{"data": {"records": [{"d": "2026-01-01", "v": NaN}]}}')
        send_through(FixedResponseTransport(HttpResponse(200, body)))
        spec = demo_spec(pagination=UNPAGED)
        code = raised_code(
            demo_fetcher(spec).fetch,
            ctx=fixture_context(SOURCE, spec),
            start_date=date(2026, 1, 1),
        )
        assert code == "ENGINE_SHAPE_INVALID"

    def test_row_envelope_keys_are_copied_as_source_facts(self, send_through) -> None:
        send_through(
            FixedResponseTransport(paged([{"d": "2026-01-01", "v": 1.0}], units="Percent"))
        )
        spec = demo_spec(
            columns=(
                ColumnSpec(name="date", kind="date", source_key="d", required=True, nullable=False),
                ColumnSpec(
                    name="value", kind="float", source_key="v", required=True, nullable=False
                ),
                ColumnSpec(name="units", kind="str", required=True, nullable=False),
            ),
            row_envelope=("units",),
            pagination=UNPAGED,
        )
        (row,) = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1)
        )
        assert row.units == "Percent"

    def test_transform_data_refuses_foreign_raw(self) -> None:
        with pytest.raises(ProviderEngineError) as raised:
            demo_fetcher(demo_spec()).transform_data([paged([])], None)
        assert raised.value.code == "ENGINE_SHAPE_INVALID"

    def test_rows_are_frozen_contract_models(self, send_through) -> None:
        from opendata.data.models.base import ContractModel

        send_through(FixedResponseTransport(paged([record(0)])))
        spec = demo_spec(pagination=UNPAGED)
        (row,) = demo_fetcher(spec).fetch(
            ctx=fixture_context(SOURCE, spec), start_date=date(2026, 1, 1)
        )
        assert isinstance(row, ContractModel)
        with pytest.raises(ValidationError, match="frozen"):
            row.value = 9.0
