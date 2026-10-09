"""Offline fixtures for engine-driven provider models.

Nothing here authorizes live access. A grant built by :func:`fixture_context`
is a local test credential for a *synthetic* transport, mirroring
``tests/provider_budget_fixtures.py``: it names one exact source/model/operation,
is finite, expires, and states its evidence as a test fixture. Deriving it from
the declaration rather than a hand-maintained table is what lets a new provider
model be tested without editing shared code.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import HttpResponse
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opendata.data.providers._engine.spec import ModelSpec

RIGHTS_EVIDENCE = "test-fixture:offline-only:fake-transport"

#: A value per declared kind, offset by ``index`` so a multi-record page is not N copies of one row.
_SAMPLE_VALUES: dict[str, tuple[object, ...]] = {
    "str": ("alpha", "beta", "gamma"),
    "int": (1, 2, 3),
    "float": (0.5, 1.5, 2.5),
    "bool": (True, False, True),
    "date": ("2026-01-01", "2026-01-02", "2026-01-03"),
    "enum": ("listed", "listed", "listed"),
    "str_list": (["one"], ["two", "three"], ["four"]),
}


def sample_value(kind: str, index: int = 0) -> object:
    """Return a raw JSON value of ``kind`` that the engine's row model will accept.

    Dates are published as ISO strings because that is what a JSON endpoint sends; the engine's
    declared ``date`` column is what turns it into a ``date``.
    """
    options = _SAMPLE_VALUES.get(kind)
    if options is None:
        raise AssertionError(f"no sample value for declared kind {kind!r}")
    return options[index % len(options)]


def synthetic_record(spec: ModelSpec, index: int = 0) -> dict[str, object]:
    """Build one raw record publishing every column ``spec`` declares, under its source keys."""
    record: dict[str, object] = {}
    for column in spec.columns:
        record[column.source_key or column.name] = sample_value(column.kind, index)
    return record


def synthetic_page(
    spec: ModelSpec, count: int = 1, *, start: int = 0, **envelope: object
) -> HttpResponse:
    """Wrap ``count`` synthetic records in the document shape ``spec`` points at.

    ``rows_pointer`` may be empty (the document *is* the list) or dotted, so the body is assembled
    by walking the pointer rather than hard-coding an envelope. ``start`` is the index of the first
    record on this page: a multi-page answer scripted without it publishes the same values on every
    page, which is precisely the repeated-page body a paging judgement exists to be able to tell
    apart from a result that advanced. ``envelope`` keys land at the document root because that is
    where the engine reads them — both ``total_key`` and ``cursor_field`` are resolved against the
    whole response, not against the record list's own nesting level — so a fixture that parked them
    beside the records would publish a total no judgement could see.
    """
    pointer = spec.rows_pointer
    records = [synthetic_record(spec, start + index) for index in range(count)]
    if not pointer:
        if envelope:
            raise AssertionError(
                f"{spec.model}: a document that is itself the record list cannot carry an envelope"
            )
        return HttpResponse(200, records)
    document: dict[str, object] = dict(envelope)
    parts = pointer.split(".")
    cursor: dict[str, object] = document
    for part in parts[:-1]:
        nested: dict[str, object] = {}
        cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = records
    return HttpResponse(200, document)


def _cell_text(value: object) -> str:
    """Render one synthetic sample the way a delimited body publishes it."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return "" if value is None else str(value)


def _preamble_rows(count: int) -> list[list[str]]:
    """Render ``count`` metadata rows that hold none of the declared column names.

    Structurally faithful rather than verbatim: a real preamble is a source's own text (the Fed's
    five ``Series Description``/``Unit:`` lines), and what the decoder depends on is only that the
    rows are non-blank and are not the header. A blank row would be dropped by the same
    blank-filter the decoder applies before it counts, which would silently shift the skip.
    """
    return [[f"preamble row {index} of {count}", "metadata"] for index in range(1, count + 1)]


def body_from_records(
    spec: ModelSpec,
    records: Sequence[Mapping[str, object]],
    *,
    keys: Sequence[str] | None = None,
    member: str | None = None,
    preamble_rows: int | None = None,
) -> bytes:
    """Render arbitrary ``records`` as the raw body ``spec``'s decoder declares.

    The lower half of :func:`synthetic_body`, exposed so a falsifier can script a body whose header
    or cells differ from the clean synthesis. ``keys`` is the header row and defaults to every
    declared source key in declared order; passing a shortened list is how a case publishes a table
    the source genuinely omitted a column from -- the only form a text body has for "this key is not
    here", since a cell left blank decodes back as a present key holding a null.

    A declaration that skips a preamble gets one, because a body without it is a different body than
    the endpoint publishes: ``preamble_rows`` defaults to the count the declaration names, and a
    falsifier that passes some other count is how an off-by-one skip is shown to be a refusal.

    SYNTHETIC like :func:`synthetic_body`: every byte is generated from the declaration, never
    recorded from an upstream response.

    Raises:
        AssertionError: The declaration reads a JSON document, so there is no delimited body to
            render -- the case asked the wrong fixture for its bytes.
    """
    decoder = spec.decoder
    if not decoder.delimited:
        raise AssertionError(f"{spec.model}: declares a json decoder, so no delimited body exists")
    header = (
        [column.source_key or column.name for column in spec.columns]
        if keys is None
        else list(keys)
    )
    table = (
        _preamble_rows(decoder.preamble_rows if preamble_rows is None else preamble_rows)
        + [header]
        + [[_cell_text(record.get(key)) for key in header] for record in records]
    )
    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=decoder.effective_delimiter, lineterminator="\n").writerows(table)
    text = buffer.getvalue()
    if decoder.kind != "zip_csv":
        return text.encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member or decoder.member or "records.csv", text)
    return archive.getvalue()


def synthetic_body(
    spec: ModelSpec, count: int = 1, *, start: int = 0, member: str | None = None
) -> bytes:
    """Render ``count`` records as the raw body ``spec``'s decoder declares.

    SYNTHETIC: every byte is generated from the declaration's own columns and sample values by this
    fixture. It is not a recorded upstream capture and must never be presented as one -- a real
    OECD, FINRA or Fama-French body belongs to its own model's verification task.

    The header row is the declared source keys in declared order, because those names are the only
    keys the row normalizer reads. A ``zip_csv`` declaration is served a real in-memory archive
    holding one member named by ``member`` (or by its own selector), so the selector itself is
    what a case exercises.

    Raises:
        AssertionError: The declaration reads a JSON document, so there is no delimited body to
            render -- the case asked the wrong fixture for its bytes.
    """
    return body_from_records(
        spec,
        [synthetic_record(spec, start + index) for index in range(count)],
        member=member,
    )


def synthetic_raw_page(
    spec: ModelSpec,
    count: int = 1,
    *,
    start: int = 0,
    member: str | None = None,
    status: int = 200,
) -> HttpResponse:
    """Return one response whose body is the synthetic raw bytes ``spec``'s decoder reads.

    ``document`` stays empty on purpose: a delimited declaration never reads it, and populating it
    would let a decoder that ignored the payload pass on a JSON fixture's rows.
    """
    body = synthetic_body(spec, count, start=start, member=member)
    return HttpResponse(status, None, payload=body)


def valid_query_kwargs(spec: ModelSpec) -> dict[str, object]:
    """Return one accepted query for ``spec``: every required parameter, by declared kind."""
    kwargs: dict[str, object] = {}
    for parameter in spec.params:
        if not parameter.required and parameter.default is None:
            continue
        value: object = {
            "str": "sampler",
            "int": 1,
            "float": 1.5,
            "bool": True,
            "date": date(2026, 1, 1),
            "str_list": ["one"],
        }[parameter.kind]
        if parameter.kind == "enum":
            value = parameter.enum[0]
        kwargs[parameter.name] = value
    return kwargs


def fixture_context(source: str, spec: ModelSpec, *, sends: int = 3) -> FetchContext:
    """Return one finite, single-identity grant context for a declared model.

    Args:
        source: Provider routing id.
        spec: The model declaration being exercised.
        sends: Bound on sends. The bounded-request profile is used because a
            declared ``max_pages`` can exceed the live-smoke per-source ceiling
            of three; the bound still has to be stated, so a fetcher that loops
            past its own declaration fails instead of hanging.

    Returns:
        A :class:`FetchContext` carrying a budget that allows exactly this
        model's QUERY operation against the declaration's own host.
    """
    host = urlsplit(spec.base_url).netloc
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=source,
        canonical_model=spec.model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=RIGHTS_EVIDENCE,
        task_attempts=sends,
        source_attempts=sends,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=sends,
        source_attempts=sends,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


class SyntheticTransport:
    """A replaceable transport that records every request it is asked to serve.

    Responses are keyed by an exact ``(url, params)`` pair, so a test states
    precisely which request must produce which body; an unkeyed request raises
    instead of quietly returning someone else's fixture.
    """

    def __init__(
        self, responses: dict[tuple[str, frozenset[tuple[str, str]]], HttpResponse]
    ) -> None:
        """Store the keyed responses and start with an empty call log."""
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    def record(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Append one request to the call log.

        Every transport shares this recorder, so a new request option cannot be recorded by one
        class and dropped by the other two -- which is what makes the recorded request the thing
        a case asserts on rather than a copy of what the case hoped to send.
        """
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
                "source": source,
                "headers": dict(headers or {}),
            }
        )

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Return the response keyed to this exact request, or fail loudly."""
        key = (url, frozenset(params.items()))
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        try:
            return self._responses[key]
        except KeyError:
            raise AssertionError(
                f"no synthetic response is keyed for {url}?{sorted(params.items())}"
            ) from None


class FixedResponseTransport(SyntheticTransport):
    """A transport answering every request with one canned response.

    Used by rejection cases where the body is irrelevant, e.g. an upstream 429
    or a malformed document; keyed requests stay available for shape cases.
    """

    def __init__(self, response: HttpResponse) -> None:
        """Start from an empty response table and keep the single response."""
        super().__init__({})
        self._response = response

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Record the request and return the canned response."""
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        return self._response


class SequencedResponseTransport(SyntheticTransport):
    """A transport answering the n-th request with the n-th prepared page.

    Paging cases need a different body per send, and they need to be falsifiable about how many
    sends happened: asking for a page beyond the script raises instead of repeating the last
    response, which is exactly the failure mode a pager that cannot tell "end of result" from
    "same page again" would otherwise pass. Requests stay recorded, so a case can also assert the
    paging keys each send carried.
    """

    def __init__(self, *pages: HttpResponse) -> None:
        """Start from an empty response table and keep the page script."""
        super().__init__({})
        self._pages = list(pages)

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Serve the next scripted page.

        Raises:
            AssertionError: The engine asked for more pages than the script prepared.
        """
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        if not self._pages:
            raise AssertionError(
                f"the paging script ran out after {len(self.calls) - 1} page(s); "
                f"request {sorted(params.items())} had no prepared body"
            )
        return self._pages.pop(0)
