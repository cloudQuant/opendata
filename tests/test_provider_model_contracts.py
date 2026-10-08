"""AC2-10's per-model contract judgement, run against every registered engine declaration.

The engine's promise is that one declaration is as testable as one hand-written fetcher, so the
acceptance cases a model must pass are generated from the declaration itself rather than from a
per-model test file: which facets apply is decided by what the record declares (a required
parameter, a numeric column, a paging kind, a credential, a path template), and every fixture is
synthesized from the declared kinds and pointers. A model cannot pass by having thought to write a
favourable test, and it cannot hide a facet that its own declaration asks for.

Nothing here reaches a network. ``_no_real_transport`` replaces the engine's only send seam with a
failure, and each case injects a synthetic transport on the generated class, so a declaration that
somehow bypasses both fails loudly instead of sending a request.

Skips are counted and published, not swallowed: ``TestApplicabilityCensus`` asserts the
applicability split is derived from the declarations and prints how many models each face actually
judged, so a face that quietly stopped applying to anything is visible rather than green.

Two registered models cannot judge a capability none of them declares, so the suite runs against
probe declarations as well — records no provider registers, written to carry the facets the registry
does not yet use (a credential, page-by-page paging, a published total, a path template). The census
keeps the two populations apart, because "the engine passes" and "a registered model passes" are
different claims and only the second one is source work.
"""

from __future__ import annotations

from datetime import date

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
    paging_page_size,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
)
from opendata.data.providers._engine.testing import (
    SequencedResponseTransport,
    fixture_context,
    sample_value,
    synthetic_page,
    valid_query_kwargs,
)
from opendata.data.providers.catalog import PROVIDERS, engine_declared_models

#: Discovery walks the provider descriptors and their lazy bindings -- the same objects the registry
#: stores -- so a model that is registered is judged and a model no descriptor binds is not. The
#: helper lives in the catalog because a tool that inventories the declarations must ask the runtime
#: the same question this suite asks, and two discovery functions can drift apart.
REGISTRY_MODELS = engine_declared_models()

#: Routing id for the probe declarations. They are in no ``ProviderDescriptor``, so no runtime path
#: reaches them; the id only names the synthetic grant the fixtures build.
PROBE_SOURCE = "probe"

PROBE_COLUMNS = (
    ColumnSpec("trade_date", "date", source_key="d", required=True, nullable=False),
    ColumnSpec("value", "float", source_key="v", required=True, nullable=False, units="usd"),
    ColumnSpec("lots", "int", source_key="n", required=True, nullable=False),
    ColumnSpec("label", "str", source_key="l"),
    ColumnSpec("tags", "str_list", source_key="t"),
    ColumnSpec("flag", "bool", source_key="f"),
)

PROBE_PARAMS = (
    ParamSpec("symbol", "str", required=True),
    ParamSpec("start_date", "date", query_key="from", required=True),
    ParamSpec("frequency", "enum", enum=("daily", "weekly")),
    ParamSpec("limit", "int", default=1, note="declared page size, so one page can fill"),
)


def probe_models() -> list[tuple[str, ModelSpec]]:
    """Declarations that exercise the facets the registered population does not use.

    Measured, not assumed: ``TestApplicabilityCensus`` prints, per face, how many registered models
    it judged, and any face the registry leaves at zero is a facet only these probes carry. A case
    guarded by ``if the declaration carries it`` skips for a model without that facet, so a green
    suite alone would be reporting the absence of judgement rather than the presence of one; the
    census keeps the two claims apart.
    """
    return [
        (
            PROBE_SOURCE,
            ModelSpec(
                model="ProbePagedTotal",
                domain="probe_paged_total",
                asset_class="macro",
                period="d",
                market="cn",
                base_url="https://probe.test",
                path="/series/records",
                rows_pointer="data.records",
                params=PROBE_PARAMS,
                columns=PROBE_COLUMNS,
                pagination=PaginationSpec(
                    kind="page",
                    limit_key="limit",
                    offset_key="page",
                    total_key="total",
                    max_pages=8,
                ),
                credential="PROBE_PAGED_TOKEN",
                credential_query_key="token",
                scenario="离线判定面：分页 + 每页重复发布的总数 + 凭证",
                error_prefix="PROBE",
            ),
        ),
        (
            PROBE_SOURCE,
            ModelSpec(
                model="ProbeOffsetPath",
                domain="probe_offset_path",
                asset_class="equity",
                period="d",
                market="us",
                base_url="https://probe.test",
                path="/series/{symbol}/quotes.json",
                rows_pointer="result.items",
                params=PROBE_PARAMS,
                columns=PROBE_COLUMNS,
                pagination=PaginationSpec(kind="offset", limit_key="limit", offset_key="offset"),
                scenario="离线判定面：offset 翻页 + 路径模板寻址",
                error_prefix="PROBE",
            ),
        ),
        (
            PROBE_SOURCE,
            ModelSpec(
                model="ProbeCursorRows",
                domain="probe_cursor_rows",
                asset_class="equity",
                period="minute",
                market="us",
                base_url="https://probe.test",
                path="/stream/rows",
                rows_pointer="rows",
                params=PROBE_PARAMS,
                columns=PROBE_COLUMNS,
                pagination=PaginationSpec(
                    kind="cursor",
                    limit_key="limit",
                    cursor_key="after",
                    cursor_field="next_cursor",
                ),
                scenario="离线判定面：游标翻页，末页不发布游标即结束",
                error_prefix="PROBE",
            ),
        ),
        (
            PROBE_SOURCE,
            ModelSpec(
                model="ProbeStaticHeaders",
                domain="probe_static_headers",
                asset_class="equity",
                period="snapshot",
                market="us",
                base_url="https://probe.test",
                path="/identifiers/search.json",
                rows_pointer="meta.data",
                params=PROBE_PARAMS,
                columns=PROBE_COLUMNS,
                static_headers=(
                    ("User-Agent", "opendata-research/1.0 (contact@example.test)"),
                    ("Accept", "application/json"),
                ),
                scenario="离线判定面：源端要求的固定请求头，逐页都要带上",
                error_prefix="PROBE",
            ),
        ),
    ]


MODELS = sorted(
    [*REGISTRY_MODELS, *probe_models()],
    key=lambda pair: f"{pair[0]}::{pair[1].model}",
)
PROBE_IDS = {f"{PROBE_SOURCE}::{spec.model}" for _, spec in probe_models()}
IDS = [f"{source}::{spec.model}" for source, spec in MODELS]
every_model = pytest.mark.parametrize("source,spec", MODELS, ids=IDS, indirect=False)


def pages_for(spec: ModelSpec, count: int) -> list[HttpResponse]:
    """Cut a result of ``count`` rows into the pages the declaration's own page size implies.

    The engine ends a ``page``/``offset`` fetch on the first short page and a cursor fetch on the
    first page that publishes no cursor, so a count that fills its pages exactly needs one more,
    empty page; that last page is part of the script, and a fetcher that asks for one page beyond it
    hits the transport's loud failure rather than a repeated body. A declared total is repeated on
    every page because repeating it is what upstreams do, and a fixture that published it once would
    leave the agreement check unjudged.
    """
    paging = spec.pagination
    total_key = paging.total_key
    if paging.kind == "none":
        return [synthetic_page(spec, count, **({} if total_key is None else {total_key: count}))]
    size = paging_page_size(spec)
    if size <= 0:
        raise AssertionError(f"{spec.model}: the declaration gives no page size to script against")
    chunks = [min(size, count - start) for start in range(0, count, size)] or [0]
    pages: list[HttpResponse] = []
    seen = 0
    for index, chunk in enumerate(chunks):
        envelope: dict[str, object] = {} if total_key is None else {total_key: count}
        cursor_field = paging.cursor_field
        if paging.kind == "cursor" and cursor_field and index + 1 < len(chunks):
            envelope[cursor_field] = f"cursor-{index + 2}"
        pages.append(synthetic_page(spec, chunk, start=seen, **envelope))
        seen += chunk
    if paging.kind != "cursor" and chunks[-1] == size:
        trailing: dict[str, object] = {} if total_key is None else {total_key: count}
        pages.append(synthetic_page(spec, 0, start=seen, **trailing))
    return pages


def amend_first_record(
    spec: ModelSpec,
    pages: list[HttpResponse],
    key: str,
    *,
    value: object = None,
    drop: bool = False,
) -> list[HttpResponse]:
    """Rewrite the first raw record of every page in a prepared answer.

    A paged fetch transforms only after its last page arrives, so a falsifier must publish the wrong
    key on each page it scripted; amending one page alone would let a later page normalize cleanly,
    and the case would judge the fixture rather than the engine.
    """
    for page in pages:
        records = http_json.resolve_rows(page.document, spec.rows_pointer, spec)
        if records:
            if drop:
                records[0].pop(key, None)
            else:
                records[0][key] = value
    return pages


def unmeasured_transport(url: str, params: dict[str, str], **_: object) -> HttpResponse:
    """Fail instead of sending: this is the gate, not a fixture."""
    raise AssertionError(f"the contract suite must never send: {url}?{sorted(params.items())}")


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch):
    """Seal the engine's only send seam for the whole module."""
    monkeypatch.setattr(http_json, "_http_get_json", unmeasured_transport)


@pytest.fixture(autouse=True)
def _supply_declared_credential(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Give a model that declares a credential a fixture token, so other faces can run at all.

    The token is a local string for a synthetic transport and authorizes nothing. The face that
    judges the credential's absence deletes it itself, and does so after this fixture set it.
    """
    callspec = getattr(request.node, "callspec", None)
    spec = callspec.params["spec"] if callspec and "spec" in callspec.params else None
    if spec is not None and spec.credential:
        monkeypatch.setenv(spec.credential, "fixture-token-only")


def build(
    source: str, spec: ModelSpec, *pages: HttpResponse
) -> tuple[object, SequencedResponseTransport]:
    """Return a fetcher instance for ``spec`` served by ``pages`` in order, plus its recorder."""
    transport = SequencedResponseTransport(*pages)
    fetcher_type = make_http_json_fetcher(source, spec)
    fetcher_type.http_transport = transport  # type: ignore[attr-defined]
    return fetcher_type(), transport


def fetch(
    source: str, spec: ModelSpec, *pages: HttpResponse, **kwargs: object
) -> tuple[int, object]:
    """Fetch one declared model offline and return the row count with the send count."""
    fetcher, transport = build(source, spec, *pages)
    rows = fetcher.fetch(  # type: ignore[attr-defined]
        ctx=fixture_context(source, spec, sends=max(len(pages), 1) + 2), **kwargs
    )
    return len(transport.calls), rows


def code_raised(spec: ModelSpec, source: str, *pages: HttpResponse, **kwargs: object) -> str:
    """Return the stable code a failing fetch reported."""
    fetcher, _ = build(source, spec, *pages)
    with pytest.raises(ProviderEngineError) as raised:
        fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(source, spec, sends=len(pages) + 2), **kwargs
        )
    return str(raised.value.code)


def expected_row_value(column_kind: str, index: int = 0) -> object:
    """The normalized value a declared kind must produce from the fixture's own sample."""
    sample = sample_value(column_kind, index)
    return date.fromisoformat(sample) if column_kind == "date" else sample


@every_model
class TestDeclarationIsTheContract:
    """What every registered model must do, before and after a byte is sent."""

    def test_identity_and_capability_come_from_the_declaration(
        self, source: str, spec: ModelSpec
    ) -> None:
        fetcher, _ = build(source, spec)
        capability = fetcher.capability  # type: ignore[attr-defined]
        assert fetcher.canonical_model == spec.model  # type: ignore[attr-defined]
        assert (capability.source, capability.domain) == (source, spec.domain)
        assert capability.asset_class == spec.asset_class
        assert capability.period == spec.period
        assert capability.market == spec.market
        assert capability.verified is False, "a declaration is not a source verification"
        assert fetcher.async_mode == spec.async_mode  # type: ignore[attr-defined]

    def test_row_model_exposes_exactly_the_declared_columns(
        self, source: str, spec: ModelSpec
    ) -> None:
        fetcher, _ = build(source, spec)
        declared = [column.name for column in spec.columns]
        assert list(fetcher.row_model.model_fields) == declared  # type: ignore[attr-defined]

    def test_undeclared_parameter_is_refused_before_any_send(
        self, source: str, spec: ModelSpec
    ) -> None:
        kwargs = valid_query_kwargs(spec)
        code = code_raised(spec, source, HttpResponse(200, []), **kwargs, not_declared="x")
        assert code == f"{spec.error_prefix}_QUERY_INVALID"

    def test_illegal_value_for_every_declared_parameter_is_refused_before_send(
        self, source: str, spec: ModelSpec
    ) -> None:
        """Each declared parameter, on its own, must reject a value from another domain.

        One foreign object is used for every kind because the engine must enforce the declared
        domain, not accept whatever pydantic happens to coerce.
        """
        if not spec.params:
            pytest.skip(f"{spec.model} declares no parameters to reject")
        base = valid_query_kwargs(spec)
        for parameter in spec.params:
            kwargs = dict(base)
            kwargs[parameter.name] = object()
            code = code_raised(spec, source, HttpResponse(200, []), **kwargs)
            assert code == f"{spec.error_prefix}_QUERY_INVALID", parameter.name

    def test_required_parameter_omission_is_refused_before_send(
        self, source: str, spec: ModelSpec
    ) -> None:
        required = [parameter for parameter in spec.params if parameter.required]
        if not required:
            pytest.skip(f"{spec.model} declares no required parameter")
        for parameter in required:
            kwargs = {
                key: value
                for key, value in valid_query_kwargs(spec).items()
                if key != parameter.name
            }
            code = code_raised(spec, source, HttpResponse(200, []), **kwargs)
            assert code == f"{spec.error_prefix}_QUERY_INVALID", parameter.name

    def test_credential_requirement_fails_before_any_send(
        self, source: str, spec: ModelSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not spec.credential:
            pytest.skip(f"{spec.model} declares no credential")
        monkeypatch.delenv(spec.credential, raising=False)
        fetcher, transport = build(source, spec, *pages_for(spec, 1))
        with pytest.raises(ProviderEngineError) as raised:
            fetcher.fetch(  # type: ignore[attr-defined]
                ctx=fixture_context(source, spec, sends=4), **valid_query_kwargs(spec)
            )
        assert str(raised.value.code) == f"{spec.error_prefix}_CREDENTIAL_MISSING"
        assert transport.calls == [], "a missing credential must not send"
        monkeypatch.setenv(spec.credential, "fixture-token")
        pages = pages_for(spec, 1)
        fetcher, transport = build(source, spec, *pages)
        rows = fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(source, spec, sends=len(pages) + 2), **valid_query_kwargs(spec)
        )
        assert len(transport.calls) == len(pages), "the declaration fetches to its own page size"
        assert len(rows) == 1
        sent_keys = {key for call in transport.calls for key in call["params"]}  # type: ignore
        assert spec.credential_query_key in sent_keys, "the credential rides on the request"

    def test_typical_request_normalizes_the_declared_columns(
        self, source: str, spec: ModelSpec
    ) -> None:
        """The positive arm: a page publishing every declared key becomes one row per record."""
        _, rows = fetch(source, spec, *pages_for(spec, 2), **valid_query_kwargs(spec))
        assert len(rows) == 2  # type: ignore[arg-type]
        for index, row in enumerate(rows):
            for column in spec.columns:
                assert getattr(row, column.name) == expected_row_value(column.kind, index), (
                    column.name
                )

    def test_a_document_carrying_no_record_list_is_a_shape_failure(
        self, source: str, spec: ModelSpec
    ) -> None:
        """One falsifier for both pointer shapes: an empty document holds no records anywhere.

        With a declared pointer the key is missing; with an empty pointer the document is the
        record list and a mapping is not a list. The counterpart arm, that a real empty list is
        zero rows rather than a failure, is
        :meth:`test_empty_record_list_is_zero_rows_not_an_error`.
        """
        code = code_raised(spec, source, HttpResponse(200, {}), **valid_query_kwargs(spec))
        assert code == f"{spec.error_prefix}_SHAPE_INVALID"

    def test_empty_record_list_is_zero_rows_not_an_error(
        self, source: str, spec: ModelSpec
    ) -> None:
        _, rows = fetch(source, spec, *pages_for(spec, 0), **valid_query_kwargs(spec))
        assert rows == ()

    def test_missing_required_source_key_is_a_shape_failure(
        self, source: str, spec: ModelSpec
    ) -> None:
        required = [column for column in spec.columns if column.required]
        if not required:
            pytest.skip(f"{spec.model} declares no required column")
        for column in required:
            pages = amend_first_record(
                spec, pages_for(spec, 1), column.source_key or column.name, drop=True
            )
            code = code_raised(spec, source, *pages, **valid_query_kwargs(spec))
            assert code == f"{spec.error_prefix}_SHAPE_INVALID", column.name

    def test_optional_column_absent_reads_as_null(self, source: str, spec: ModelSpec) -> None:
        """The counterpart: an absent optional key is a null, not a shape failure."""
        optional = [column for column in spec.columns if not column.required]
        if not optional:
            pytest.skip(f"{spec.model} declares no optional column")
        pages = pages_for(spec, 1)
        for column in optional:
            amend_first_record(spec, pages, column.source_key or column.name, drop=True)
        _, rows = fetch(source, spec, *pages, **valid_query_kwargs(spec))
        for column in optional:
            assert getattr(rows[0], column.name) is None, column.name

    def test_boolean_published_for_a_numeric_column_is_refused(
        self, source: str, spec: ModelSpec
    ) -> None:
        numeric = [column for column in spec.columns if column.kind in ("int", "float")]
        if not numeric:
            pytest.skip(f"{spec.model} declares no numeric column")
        for column in numeric:
            pages = amend_first_record(
                spec, pages_for(spec, 1), column.source_key or column.name, value=True
            )
            code = code_raised(spec, source, *pages, **valid_query_kwargs(spec))
            assert code == f"{spec.error_prefix}_SHAPE_INVALID", column.name

    def test_non_finite_float_is_refused(self, source: str, spec: ModelSpec) -> None:
        floats = [column for column in spec.columns if column.kind == "float"]
        if not floats:
            pytest.skip(f"{spec.model} declares no float column")
        pages = amend_first_record(
            spec, pages_for(spec, 1), floats[0].source_key or floats[0].name, value=float("nan")
        )
        code = code_raised(spec, source, *pages, **valid_query_kwargs(spec))
        assert code == f"{spec.error_prefix}_SHAPE_INVALID"

    @pytest.mark.parametrize(
        ("status", "suffix"),
        [
            (400, "QUERY_REJECTED"),
            (401, "CREDENTIAL_REJECTED"),
            (403, "CREDENTIAL_REJECTED"),
            (404, "RESOURCE_MISSING"),
            (429, "RATE_LIMITED"),
            (500, "UPSTREAM_UNAVAILABLE"),
            (418, "HTTP_ERROR"),
        ],
    )
    def test_upstream_status_becomes_the_declared_code(
        self, source: str, spec: ModelSpec, status: int, suffix: str
    ) -> None:
        """Every model's failures are named, and the engine does not retry them itself."""
        code = code_raised(
            spec,
            source,
            HttpResponse(status, {}),
            **valid_query_kwargs(spec),
        )
        assert code == f"{spec.error_prefix}_{suffix}"

    def test_paging_drives_from_the_declaration_and_stops_on_a_short_page(
        self, source: str, spec: ModelSpec
    ) -> None:
        """The paging keys come from the declaration, and the fetch ends where the script ends."""
        paging = spec.pagination
        count = 1 if paging.kind == "none" else 2
        pages = pages_for(spec, count)
        fetcher, transport = build(source, spec, *pages)
        rows = fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(source, spec, sends=len(pages) + 2), **valid_query_kwargs(spec)
        )
        assert len(rows) == count
        assert len(transport.calls) == len(pages), paging.kind
        if paging.kind == "none":
            assert len(pages) == 1, "an unpaged declaration must ask once"
            return
        assert len(pages) > 1, f"{paging.kind} paging was scripted as a single page"
        first = transport.calls[0]["params"]
        second = transport.calls[1]["params"]
        if paging.offset_key:
            assert first[paging.offset_key] != second[paging.offset_key], paging.kind
        if paging.cursor_key:
            assert paging.cursor_key not in first
            assert second[paging.cursor_key] == "cursor-2"

    def test_declared_total_is_read_once_and_agrees_with_the_rows(
        self, source: str, spec: ModelSpec
    ) -> None:
        """A total repeated on every page is the size of the result, not one page's worth."""
        key = spec.pagination.total_key
        if key is None:
            pytest.skip(f"{spec.model} publishes no total")
        total = 3
        _, rows = fetch(source, spec, *pages_for(spec, total), **valid_query_kwargs(spec))
        assert len(rows) == total

    def test_rows_short_of_the_published_total_are_incomplete(
        self, source: str, spec: ModelSpec
    ) -> None:
        key = spec.pagination.total_key
        if key is None:
            pytest.skip(f"{spec.model} publishes no total")
        pages = pages_for(spec, 2)
        for page in pages:
            if isinstance(page.document, dict):
                page.document[key] = 5
        code = code_raised(spec, source, *pages, **valid_query_kwargs(spec))
        assert code == f"{spec.error_prefix}_INCOMPLETE"

    def test_pages_that_disagree_on_the_total_are_a_conflict(
        self, source: str, spec: ModelSpec
    ) -> None:
        key = spec.pagination.total_key
        if key is None:
            pytest.skip(f"{spec.model} publishes no total")
        pages = pages_for(spec, 2)
        documents = [page.document for page in pages]
        if len(documents) < 2 or not all(isinstance(document, dict) for document in documents):
            pytest.skip(f"{spec.model}: its own page size scripts one page only")
        documents[1][key] = 1  # type: ignore[index]
        code = code_raised(spec, source, *pages, **valid_query_kwargs(spec))
        assert code == f"{spec.error_prefix}_TOTAL_CONFLICT"

    def test_path_placeholders_never_reach_the_wire_unresolved(
        self, source: str, spec: ModelSpec
    ) -> None:
        if not spec.path_placeholders:
            pytest.skip(f"{spec.model} addresses its resource in the query string")
        kwargs = valid_query_kwargs(spec)
        fetcher, transport = build(source, spec, *pages_for(spec, 1))
        fetcher.fetch(ctx=fixture_context(source, spec, sends=4), **kwargs)  # type: ignore
        url = str(transport.calls[0]["url"])
        assert "{" not in url and "}" not in url, url
        for placeholder in spec.path_placeholders:
            assert str(kwargs[placeholder]) in url, placeholder
        # Substitution must not open a segment: the rendered URL has exactly as many slash-separated
        # parts as the declared origin plus the declared path, minus the one they share.
        assert (
            len(url.split("/")) == len(spec.base_url.split("/")) + len(spec.path.split("/")) - 1
        ), url

    def test_declared_static_headers_are_sent_on_every_page(
        self, source: str, spec: ModelSpec
    ) -> None:
        """A header the endpoint requires is not optional on the second page of a result.

        The claim this face refuses is the one an engine can hide: request options assembled inside
        the page loop can be dropped after the first send, which a single-page model never shows.
        The same assertion is the negative arm for the models that declare no header -- they must
        send an empty set rather than an invented one.
        """
        declared = dict(spec.static_headers)
        pages = pages_for(spec, 1 if spec.pagination.kind == "none" else 2)
        fetcher, transport = build(source, spec, *pages)
        fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(source, spec, sends=len(pages) + 2), **valid_query_kwargs(spec)
        )
        sent = [call["headers"] for call in transport.calls]
        assert len(sent) == len(pages), sent
        assert sent == [declared] * len(pages)


@every_model
class TestAsyncApplicabilityIsDeclared:
    """AC2-16: applicability is stated on the declaration, decided before I/O."""

    def test_applicability_is_one_of_the_two_declared_modes(
        self, source: str, spec: ModelSpec
    ) -> None:
        assert spec.async_mode in ("bounded_thread", "unsupported")
        fetcher, _ = build(source, spec)
        assert fetcher.async_mode == spec.async_mode  # type: ignore[attr-defined]


class TestApplicabilityCensus:
    """The denominator and the applicability split, measured from the registry."""

    def test_discovery_accounted_for_every_registered_binding(self) -> None:
        bindings = sum(len(descriptor.fetcher_bindings) for descriptor in PROVIDERS)
        engine = len(REGISTRY_MODELS)
        identities = [f"{source}::{spec.model}" for source, spec in REGISTRY_MODELS]
        assert engine >= 1, "the engine drives no registered model, so this suite judged nothing"
        assert len(identities) == len(set(identities)), "one model registered twice"
        assert engine <= bindings
        print(
            f"\ncensus: bindings={bindings} engine_driven={engine} hand_written={bindings - engine}"
            f" sources={len({source for source, _ in REGISTRY_MODELS})}"
        )

    @pytest.mark.parametrize(
        "face",
        [
            ("params", lambda spec: bool(spec.params)),
            ("required_param", lambda spec: any(p.required for p in spec.params)),
            ("credential", lambda spec: bool(spec.credential)),
            ("required_column", lambda spec: any(c.required for c in spec.columns)),
            ("optional_column", lambda spec: any(not c.required for c in spec.columns)),
            ("numeric_column", lambda spec: any(c.kind in ("int", "float") for c in spec.columns)),
            ("float_column", lambda spec: any(c.kind == "float" for c in spec.columns)),
            ("int_column", lambda spec: any(c.kind == "int" for c in spec.columns)),
            ("date_column", lambda spec: any(c.kind == "date" for c in spec.columns)),
            ("bool_column", lambda spec: any(c.kind == "bool" for c in spec.columns)),
            ("str_list_column", lambda spec: any(c.kind == "str_list" for c in spec.columns)),
            ("enum_param", lambda spec: any(p.kind == "enum" for p in spec.params)),
            ("paged", lambda spec: spec.pagination.kind != "none"),
            ("page_kind", lambda spec: spec.pagination.kind == "page"),
            ("offset_kind", lambda spec: spec.pagination.kind == "offset"),
            ("cursor_kind", lambda spec: spec.pagination.kind == "cursor"),
            ("declares_total", lambda spec: spec.pagination.total_key is not None),
            ("path_template", lambda spec: bool(spec.path_placeholders)),
            ("empty_pointer", lambda spec: spec.rows_pointer == ""),
            ("row_envelope", lambda spec: bool(spec.row_envelope)),
            ("static_headers", lambda spec: bool(spec.static_headers)),
        ],
    )
    def test_face_applicability_is_measured_not_assumed(self, face) -> None:
        """Every capability the format can express is judged by at least one declaration.

        The gate is on the whole population, because a face the registry does not use is a source
        gap, not an engine gap; the printed split is what keeps the two apart in the record. A
        regression that made a declaration field unreadable — a path template that never parses, a
        kind list that stops matching — would drive a face to zero across both populations and fail
        here rather than turning the case into a silent skip.
        """
        name, predicate = face
        judged = [f"{source}::{spec.model}" for source, spec in MODELS if predicate(spec)]
        from_registry = [item for item in judged if item not in PROBE_IDS]
        print(
            f"\nface {name}: judged={len(judged)} of {len(MODELS)} "
            f"from_registry={len(from_registry)} from_probe={len(judged) - len(from_registry)} "
            f"{judged}"
        )
        assert judged, f"{name}: no declaration exercises it, so this face is unmeasured"

    def test_probe_population_is_the_part_that_is_not_source_work(self) -> None:
        """Probes are counted separately, so a reader cannot take them for implemented tasks."""
        registered_ids = {f"{source}::{spec.model}" for source, spec in REGISTRY_MODELS}
        assert registered_ids.isdisjoint(PROBE_IDS), "a probe id shadows a registered model"
        assert len(MODELS) == len(REGISTRY_MODELS) + len(PROBE_IDS)
        print(
            f"\npopulation: registered={len(registered_ids)} probe={len(PROBE_IDS)} "
            f"probe_ids={sorted(PROBE_IDS)}"
        )


class TestDeclarationRules:
    """Malformed declarations are refused when the record is written, not when it is queried."""

    @staticmethod
    def _base(**overrides: object) -> ModelSpec:
        fields: dict[str, object] = {
            "model": "RuleProbe",
            "domain": "rule_probe",
            "asset_class": "macro",
            "period": "d",
            "market": "us",
            "base_url": "https://probe.test",
            "path": "/records",
            "columns": (ColumnSpec("label", "str", required=True),),
            "params": (ParamSpec("symbol", "str", required=True),),
            "scenario": "offline declaration rule verification",
            "error_prefix": "PROBE",
        }
        fields.update(overrides)
        return ModelSpec(**fields)  # type: ignore[arg-type]

    def test_path_placeholder_may_be_required(self) -> None:
        spec = self._base(path="/series/{symbol}.json")
        assert spec.path_placeholders == ("symbol",)

    def test_path_placeholder_may_be_defaulted(self) -> None:
        spec = self._base(
            path="/series/{symbol}.json",
            params=(ParamSpec("symbol", "str", default="overall"),),
        )
        assert spec.path_placeholders == ("symbol",)

    def test_optional_path_placeholder_without_default_is_refused(self) -> None:
        """The falsifier: an addressable resource with no way to address it is not a declaration.

        ``render_path`` catches this at query time; the record is where the author is reading.
        """
        with pytest.raises(ValueError, match="must be required or defaulted"):
            self._base(path="/series/{symbol}.json", params=(ParamSpec("symbol", "str"),))

    def test_undeclared_path_placeholder_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a declared parameter"):
            self._base(path="/series/{missing}.json")

    def test_page_pagination_may_declare_its_offset_key(self) -> None:
        spec = self._base(
            pagination=PaginationSpec(kind="page", limit_key="limit", offset_key="page")
        )
        assert spec.pagination.offset_key == "page"

    def test_paging_without_an_offset_key_is_refused(self) -> None:
        """The falsifier: with nothing to advance, every request is the first request again."""
        with pytest.raises(ValueError, match="requires offset_key"):
            self._base(pagination=PaginationSpec(kind="page", limit_key="limit"))
        with pytest.raises(ValueError, match="requires offset_key"):
            self._base(pagination=PaginationSpec(kind="offset", limit_key="limit"))

    def test_unpaged_model_needs_no_paging_keys(self) -> None:
        spec = self._base(pagination=PaginationSpec(kind="none", total_key="total"))
        assert spec.pagination.kind == "none"

    def test_static_headers_may_be_declared(self) -> None:
        spec = self._base(static_headers=(("User-Agent", "opendata-research/1.0"),))
        assert spec.static_headers == (("User-Agent", "opendata-research/1.0"),)

    def test_header_value_with_a_line_break_is_refused(self) -> None:
        """The falsifier: CR/LF in a value is a second header, not one field."""
        with pytest.raises(ValueError, match="may not contain CR or LF"):
            self._base(static_headers=(("Accept", "application/json\r\nX-Injected: 1"),))

    def test_header_name_that_is_not_a_token_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an HTTP field name"):
            self._base(static_headers=(("Bad Header", "1"),))
        with pytest.raises(ValueError, match="not an HTTP field name"):
            self._base(static_headers=(("Accept\r\n", "1"),))

    def test_blank_header_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="declares no value"):
            self._base(static_headers=(("Accept", "   "),))

    def test_header_declared_in_two_casings_is_refused(self) -> None:
        """HTTP field names are case-insensitive, so two casings are one field written twice."""
        with pytest.raises(ValueError, match="declared twice"):
            self._base(static_headers=(("Accept", "a"), ("accept", "b")))

    def test_header_entry_that_is_not_a_pair_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"static header is a \(name, value\) pair"):
            self._base(static_headers=(("Accept",)))
