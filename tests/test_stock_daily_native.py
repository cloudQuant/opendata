"""Synthetic contract tests for source-native Eastmoney A-share daily responses."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from opendata.data.providers.akshare.stock_daily_native import (
    KLINE_FIELD_IDS,
    MAX_KLINE_ROWS,
    MAX_REQUEST_WINDOW_DAYS,
    MAX_RESPONSE_BYTES,
    StockDailyNativeEnvelope,
    StockDailyNativeEvidence,
    StockDailyNativeRequest,
    StockDailyNativeRow,
    parse_eastmoney_stock_daily_response,
    parse_verified_stock_daily_capture,
)

_UTC = timezone.utc
_LINE = "2026-10-20,1.2300,1.2500,1.2700,1.2200,001.00,0,,NULL,-0.0030,"
_OBSERVED_AT = datetime(2020, 1, 1, tzinfo=_UTC)


def _request(
    symbol: str = "600519.SH",
    *,
    start_date: str = "2026-10-19",
    end_date: str = "2026-10-22",
    request_source: Literal["akshare", "auto"] = "akshare",
) -> StockDailyNativeRequest:
    """Create one explicit bounded request for a synthetic source response."""
    return StockDailyNativeRequest(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        request_source=request_source,
    )


def _body(
    *lines: str,
    code: str = "600519",
    market: int = 1,
    rc: int = 0,
) -> bytes:
    """Serialize one synthetic Eastmoney-shaped response envelope."""
    return json.dumps(
        {"rc": rc, "data": {"code": code, "market": market, "klines": list(lines)}},
        separators=(",", ":"),
    ).encode("utf-8")


def _request_digest(request: StockDailyNativeRequest) -> str:
    """Return the legacy stock-job request summary used by its raw evidence writer."""
    from opendata.data.request_summary import request_summary_sha256

    return request_summary_sha256(
        {
            "adjust": "",
            "end_date": request.end_date,
            "market": "cn",
            "source": request.request_source,
            "start_date": request.start_date,
            "symbol": request.symbol,
        },
        allowed_fields=("adjust", "end_date", "market", "source", "start_date", "symbol"),
    )


def _rebuild_envelope(
    envelope: StockDailyNativeEnvelope, **updates: object
) -> StockDailyNativeEnvelope:
    """Revalidate a deliberately modified source-native envelope."""
    payload = envelope.model_dump(mode="python")
    payload.update(updates)
    return StockDailyNativeEnvelope.model_validate(payload)


def _linked_evidence(
    request: StockDailyNativeRequest,
    body: bytes,
    *,
    captured_at_utc: datetime = datetime(2026, 10, 21, tzinfo=_UTC),
) -> StockDailyNativeEvidence:
    """Build synthetic local evidence whose declared hashes match the inputs."""
    import hashlib

    return StockDailyNativeEvidence(
        capture_id="a" * 32,
        provider_id="akshare",
        dataset_id="iteration204.stock_daily",
        operation_id="stock-daily-wire",
        request_summary_sha256=_request_digest(request),
        http_status=200,
        response_sha256=hashlib.sha256(body).hexdigest(),
        response_size_bytes=len(body),
        manifest_sha256="b" * 64,
        captured_at_utc=captured_at_utc,
        retention_class="unknown",
        source_history_limit="unknown",
    )


def test_request_builds_fixed_sh_and_sz_routes() -> None:
    """Keep the configured route, request interval, and raw basis explicit."""
    sh_request = _request()
    sz_request = _request("000001.SZ")

    assert sh_request.request_parameters() == {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": ",".join(KLINE_FIELD_IDS),
        "klt": "101",
        "fqt": "0",
        "secid": "1.600519",
        "beg": "20261019",
        "end": "20261022",
    }
    assert sz_request.request_parameters()["secid"] == "0.000001"
    assert sh_request.exchange == "SH"
    assert sz_request.exchange == "SZ"


@pytest.mark.parametrize("symbol", ["920001.BJ", "BJ920001", "830001", "600519.SZ"])
def test_request_fails_closed_for_bse_or_mismatched_exchange(symbol: str) -> None:
    """Reject unsupported BSE products and symbols inconsistent with local routing."""
    with pytest.raises(ValueError):
        _request(symbol)


def test_request_accepts_explicit_exchange_only_when_it_matches_the_symbol() -> None:
    """Normalize an explicit matching route while retaining strict route identity."""
    request = StockDailyNativeRequest(
        symbol="600519",
        exchange=" sh ",
        start_date="20261019",
        end_date="20261022",
    )

    assert request.symbol == "600519"
    assert request.exchange == "SH"
    assert request.start_date == date(2026, 10, 19)


@pytest.mark.parametrize(
    ("symbol", "exchange", "message"),
    [
        (600519, None, "symbol must be a six-digit"),
        ("not-a-code", None, "symbol must be a six-digit code"),
        ("500001", None, "does not match the configured SH/SZ"),
        ("600519", 1, "exchange must be SH or SZ"),
        ("600519", "BJ", "only SH and SZ routes are supported"),
        ("600519", "SZ", "symbol and declared exchange do not match"),
    ],
)
def test_request_rejects_invalid_symbol_and_explicit_exchange(
    symbol: object, exchange: object, message: str
) -> None:
    """Reject non-code inputs, unsupported routes, and contradictory declarations."""
    values: dict[str, object] = {
        "symbol": symbol,
        "start_date": "2026-10-19",
        "end_date": "2026-10-22",
    }
    if exchange is not None:
        values["exchange"] = exchange

    with pytest.raises(ValueError, match=message):
        StockDailyNativeRequest.model_validate(values)


def test_request_rejects_non_object_payload() -> None:
    """Do not coerce non-object input into a market request."""
    with pytest.raises(ValueError, match="request must be an object"):
        StockDailyNativeRequest.model_validate(None)


@pytest.mark.parametrize(
    ("start_date", "end_date", "message"),
    [
        (datetime(2026, 10, 19), "2026-10-22", "must not include a time"),
        (123, "2026-10-22", "must use YYYY-MM-DD or YYYYMMDD"),
        ("2026/10/19", "2026-10-22", "must use YYYY-MM-DD or YYYYMMDD"),
        ("2026-02-30", "2026-10-22", "business date is invalid"),
        ("2026-10-23", "2026-10-22", "on or before"),
        (
            "2010-01-01",
            (date(2010, 1, 1) + timedelta(days=MAX_REQUEST_WINDOW_DAYS)).isoformat(),
            "exceeds the configured daily wire bound",
        ),
    ],
)
def test_request_rejects_invalid_or_unbounded_dates(
    start_date: object, end_date: object, message: str
) -> None:
    """Accept only explicit business dates and enforce the bounded query window."""
    with pytest.raises(ValueError, match=message):
        _request(start_date=start_date, end_date=end_date)  # type: ignore[arg-type]


def test_parser_preserves_zero_empty_and_unknown_tokens_verbatim() -> None:
    """Preserve all eleven lexical values without converting units or NULLs."""
    request = _request()
    envelope = parse_eastmoney_stock_daily_response(
        _body(_LINE), request=request, observed_at_utc=_OBSERVED_AT
    )
    row = envelope.rows[0]

    assert row.trade_date == date(2026, 10, 20)
    assert row.source_line == _LINE
    assert len(row.raw_tokens) == 11
    assert row.token("open") == "1.2300"
    assert row.token("volume") == "001.00"
    assert row.token("amount") == "0"
    assert row.token("amplitude") == ""
    assert row.token("change_percent") == "NULL"
    assert row.token("turnover_rate") == ""
    assert row.tokens_by_name() == dict(
        zip(
            (
                "trade_date",
                "open",
                "close",
                "high",
                "low",
                "volume",
                "amount",
                "amplitude",
                "change_percent",
                "change",
                "turnover_rate",
            ),
            _LINE.split(","),
            strict=True,
        )
    )
    assert "open" not in StockDailyNativeRow.model_fields
    assert envelope.requested_adjustment_parameter == "fqt=0"
    assert envelope.adjustment_semantics == "unverified"
    assert envelope.price_unit_semantics == "unknown"
    assert envelope.volume_unit_semantics == "unknown"
    assert envelope.amount_unit_semantics == "unknown"
    assert envelope.source_authorization_status == "unknown"
    assert envelope.history_coverage_status == "unknown"
    assert envelope.observed_at_status == "caller_declared_unverified"
    assert envelope.evidence is None


def test_row_rejects_tokens_that_differ_from_the_retained_source_line() -> None:
    """Prevent a caller from attaching edited tokens to an unchanged source line."""
    altered_tokens = list(_LINE.split(","))
    altered_tokens[1] = "999"
    with pytest.raises(ValueError, match="source line and raw tokens differ"):
        StockDailyNativeRow(
            row_index=1,
            trade_date=date(2026, 10, 20),
            source_line=_LINE,
            raw_tokens=tuple(altered_tokens),
        )


def test_row_rejects_unknown_field_names() -> None:
    """Keep token access restricted to the fixed eleven source fields."""
    row = parse_eastmoney_stock_daily_response(_body(_LINE), request=_request()).rows[0]
    with pytest.raises(ValueError, match="field_name is not in the fixed"):
        row.token("adjusted_close")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("response_symbol", "000001", "response symbol does not match"),
        ("response_market_id", 0, "response market ID does not match"),
        ("observed_start_date", date(2026, 10, 19), "observed bounds do not match"),
        ("duplicate_trade_dates", (date(2026, 10, 20),), "duplicate-date summary"),
        ("observed_at_status", "caller_declared_unverified", "timestamp status"),
    ],
)
def test_envelope_rejects_inconsistent_derived_metadata(
    field: str, value: object, message: str
) -> None:
    """Recompute identity and summaries instead of trusting caller-supplied metadata."""
    envelope = parse_eastmoney_stock_daily_response(_body(_LINE), request=_request())
    with pytest.raises(ValueError, match=message):
        _rebuild_envelope(envelope, **{field: value})


def test_envelope_rejects_a_retained_row_outside_the_request_window() -> None:
    """Validate the envelope boundary even when rows bypass the wire parser."""
    envelope = parse_eastmoney_stock_daily_response(_body(_LINE), request=_request())
    outside_line = _LINE.replace("2026-10-20", "2026-10-23", 1)
    outside_row = StockDailyNativeRow(
        row_index=1,
        trade_date=date(2026, 10, 23),
        source_line=outside_line,
        raw_tokens=tuple(outside_line.split(",")),
    )

    with pytest.raises(ValueError, match="outside the request window"):
        _rebuild_envelope(
            envelope,
            rows=(outside_row,),
            observed_start_date=date(2026, 10, 23),
            observed_end_date=date(2026, 10, 23),
        )


@pytest.mark.parametrize(
    ("evidence_change", "message"),
    [
        ({"provider_id": "other"}, "raw evidence identity or HTTP status"),
        ({"operation_id": "other-operation"}, "raw evidence identity or HTTP status"),
        ({"http_status": 201}, "raw evidence identity or HTTP status"),
        ({"request_summary_sha256": "c" * 64}, "request summary does not match"),
        ({"response_sha256": "c" * 64}, "response hash or size does not match"),
        ({"response_size_bytes": 1}, "response hash or size does not match"),
        (
            {"captured_at_utc": datetime(2019, 1, 1, tzinfo=_UTC)},
            "precedes the declared observation time",
        ),
    ],
)
def test_envelope_rejects_unlinked_or_inconsistent_evidence(
    evidence_change: dict[str, object], message: str
) -> None:
    """Ensure caller-declared evidence metadata cannot drift from body and query."""
    request = _request()
    body = _body(_LINE)
    envelope = parse_eastmoney_stock_daily_response(
        body, request=request, observed_at_utc=_OBSERVED_AT
    )
    evidence = _linked_evidence(request, body).model_dump(mode="python")
    evidence.update(evidence_change)

    with pytest.raises(ValueError, match=message):
        _rebuild_envelope(envelope, evidence=evidence)


@pytest.mark.parametrize(
    "captured_at_utc",
    [datetime(2026, 10, 21), datetime(2026, 10, 21, tzinfo=timezone(timedelta(hours=8)))],
)
def test_evidence_requires_utc_capture_time(captured_at_utc: datetime) -> None:
    """Reject naive or non-UTC local capture times at the evidence boundary."""
    with pytest.raises(ValueError, match="captured_at_utc must be timezone-aware UTC"):
        _linked_evidence(_request(), b"synthetic", captured_at_utc=captured_at_utc)


def test_envelope_rejects_non_utc_observation_time_at_model_boundary() -> None:
    """Keep model construction strict even when callers bypass the response parser."""
    envelope = parse_eastmoney_stock_daily_response(_body(_LINE), request=_request())
    payload = envelope.model_dump(mode="python")
    payload["observed_at_utc"] = datetime(2026, 10, 20)

    with pytest.raises(ValueError, match="observed_at_utc must be a timezone-aware UTC"):
        StockDailyNativeEnvelope.model_validate(payload)


def test_parser_keeps_source_order_duplicates_and_reports_them() -> None:
    """Do not drop duplicate source rows while exposing their ambiguous dates."""
    envelope = parse_eastmoney_stock_daily_response(_body(_LINE, _LINE), request=_request())

    assert len(envelope.rows) == 2
    assert tuple(row.row_index for row in envelope.rows) == (1, 2)
    assert envelope.duplicate_trade_dates == (date(2026, 10, 20),)
    assert envelope.observed_start_date == date(2026, 10, 20)
    assert envelope.observed_end_date == date(2026, 10, 20)


def test_empty_source_rows_do_not_claim_history_or_missing_date_semantics() -> None:
    """Keep an empty result distinct from evidence of a complete or empty history."""
    envelope = parse_eastmoney_stock_daily_response(_body(), request=_request())

    assert envelope.rows == ()
    assert envelope.observed_start_date is None
    assert envelope.observed_end_date is None
    assert envelope.history_coverage_status == "unknown"
    assert envelope.missing_date_interpretation == "unknown"


def test_parser_rejects_non_bytes_oversized_bodies_and_unvalidated_requests() -> None:
    """Bound caller-controlled response bodies before JSON parsing or hashing."""
    with pytest.raises(ValueError, match="body must be bytes"):
        parse_eastmoney_stock_daily_response(bytearray(_body(_LINE)), request=_request())
    with pytest.raises(ValueError, match="exceeds the local raw-body size bound"):
        parse_eastmoney_stock_daily_response(b" " * (MAX_RESPONSE_BYTES + 1), request=_request())
    with pytest.raises(TypeError, match="validated StockDailyNativeRequest"):
        parse_eastmoney_stock_daily_response(
            _body(_LINE),
            request="600519",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xff", "strict UTF-8 JSON"),
        (b"not-json", "strict UTF-8 JSON"),
        (b'{"rc":NaN,"data":{}}', "strict UTF-8 JSON"),
        (b"[]", "must be a JSON object"),
        (_body(_LINE, rc=True), "missing or non-success rc"),
        (b'{"rc":0}', "missing its data object"),
        (b'{"rc":0,"data":null}', "missing its data object"),
        (b'{"rc":0,"data":{"code":null,"market":1,"klines":[]}}', "symbol"),
        (_body(_LINE, market=True), "market does not match"),
        (b'{"rc":0,"data":{"code":"600519","market":1}}', "missing its klines list"),
        (b'{"rc":0,"data":{"code":"600519","market":1,"klines":""}}', "missing its klines list"),
        (_body(123), "row 1 must be a text line"),
        (_body("2026-02-30,1,2,3,4,5,6,7,8,9,10"), "invalid trade-date token"),
    ],
)
def test_parser_rejects_malformed_json_envelopes_and_rows(body: bytes, message: str) -> None:
    """Reject malformed encodings, identities, response shapes, and date tokens."""
    with pytest.raises(ValueError, match=message):
        parse_eastmoney_stock_daily_response(body, request=_request())


def test_parser_rejects_more_than_the_configured_row_limit() -> None:
    """Refuse oversized kline lists before materializing typed rows."""
    body = json.dumps(
        {
            "rc": 0,
            "data": {
                "code": "600519",
                "market": 1,
                "klines": [_LINE] * (MAX_KLINE_ROWS + 1),
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(ValueError, match="exceeds the local row-count bound"):
        parse_eastmoney_stock_daily_response(body, request=_request())


def test_parser_rejects_datetime_subclass_that_fails_during_utc_normalization() -> None:
    """Translate timestamp conversion failures into a bounded contract error."""

    class BrokenAstimezone(datetime):
        def astimezone(self, tz: object = None) -> datetime:
            raise OverflowError("synthetic datetime conversion failure")

    with pytest.raises(ValueError, match="outside the supported UTC range"):
        parse_eastmoney_stock_daily_response(
            _body(_LINE),
            request=_request(),
            observed_at_utc=BrokenAstimezone(2026, 10, 20, tzinfo=_UTC),
        )


@pytest.mark.parametrize(
    ("body", "query"),
    [
        (_body(_LINE, rc=1), _request()),
        (_body(_LINE, code="000001"), _request()),
        (_body(_LINE, market=0), _request()),
        (_body("2026-10-20,1,2,3,4,5,6,7,8,9"), _request()),
        (_body("2026-10-23,1,2,3,4,5,6,7,8,9,10"), _request()),
        (_body("2026-10-19x,1,2,3,4,5,6,7,8,9,10"), _request()),
    ],
)
def test_parser_rejects_wire_identity_shape_and_window_errors(
    body: bytes, query: StockDailyNativeRequest
) -> None:
    """Reject malformed records without turning them into normalized bars."""
    with pytest.raises(ValueError):
        parse_eastmoney_stock_daily_response(body, request=query)


def test_parser_rejects_duplicate_json_keys_and_non_utc_observation_time() -> None:
    """Reject ambiguous JSON and avoid silently relabeling non-UTC timestamps."""
    duplicate_key_body = b'{"rc":0,"rc":0,"data":{"code":"600519","market":1,"klines":[]}}'
    with pytest.raises(ValueError, match="strict UTF-8 JSON"):
        parse_eastmoney_stock_daily_response(duplicate_key_body, request=_request())
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        parse_eastmoney_stock_daily_response(
            _body(_LINE),
            request=_request(),
            observed_at_utc=datetime(2026, 10, 20),
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        parse_eastmoney_stock_daily_response(
            _body(_LINE),
            request=_request(),
            observed_at_utc=datetime(2026, 10, 20, tzinfo=timezone(timedelta(hours=8))),
        )


def test_verified_capture_uses_local_reader_without_claiming_source_authority(
    tmp_path: Path,
) -> None:
    """Verify local artifact hashes while keeping caller identity and rights unverified."""
    from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

    request = _request()
    body = _body(_LINE)
    writer = RawResponseArtifactWriter(
        tmp_path / "raw-evidence", max_response_bytes=2 * 1024 * 1024
    )
    capture = writer.capture(
        (body,),
        source=RawResponseSource(
            provider_id="akshare",
            dataset_id="iteration204.stock_daily",
            operation_id="stock-daily-wire",
        ),
        request_summary_sha256=_request_digest(request),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )

    envelope = parse_verified_stock_daily_capture(
        capture,
        artifact_writer=writer,
        request=request,
        observed_at_utc=_OBSERVED_AT,
    )

    assert envelope.evidence is not None
    assert envelope.evidence.response_sha256 == envelope.response_sha256
    assert envelope.evidence.response_size_bytes == len(body)
    assert envelope.evidence.request_summary_provenance == "caller_declared_unverified"
    assert envelope.evidence.http_status == 200
    assert envelope.evidence.http_status_provenance == "caller_declared_unverified"
    assert len(envelope.evidence.manifest_sha256) == 64
    assert envelope.evidence.source_identity_status == "caller_declared_unverified"
    assert envelope.evidence.retention_status == "caller_declared_unverified"
    assert envelope.source_identity_status == "unverified"
    assert envelope.source_authorization_status == "unknown"


def test_verified_capture_rejects_unsupported_objects_and_source_declarations(
    tmp_path: Path,
) -> None:
    """Fail closed on wrong capture types and inconsistent caller source identity."""
    from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

    request = _request()
    writer = RawResponseArtifactWriter(tmp_path / "raw-evidence")
    capture = writer.capture(
        (_body(_LINE),),
        source=RawResponseSource(
            provider_id="akshare",
            dataset_id="iteration204.stock_daily",
            operation_id="stock-daily-wire",
        ),
        request_summary_sha256=_request_digest(request),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )
    parser_kwargs = {
        "artifact_writer": writer,
        "request": request,
        "observed_at_utc": _OBSERVED_AT,
    }

    with pytest.raises(TypeError, match="CapturedRawResponse"):
        parse_verified_stock_daily_capture(object(), **parser_kwargs)
    with pytest.raises(TypeError, match="RawResponseArtifactWriter"):
        parse_verified_stock_daily_capture(
            capture, **{**parser_kwargs, "artifact_writer": object()}
        )
    with pytest.raises(TypeError, match="validated StockDailyNativeRequest"):
        parse_verified_stock_daily_capture(capture, **{**parser_kwargs, "request": object()})

    with pytest.raises(ValueError, match="manifest has an unsupported type"):
        parse_verified_stock_daily_capture(replace(capture, manifest=object()), **parser_kwargs)

    bad_source_manifest = capture.manifest.model_copy(update={"source": object()})
    with pytest.raises(ValueError, match="source identity has an unsupported type"):
        parse_verified_stock_daily_capture(
            replace(capture, manifest=bad_source_manifest), **parser_kwargs
        )

    for wrong_source in (
        RawResponseSource(
            provider_id="other-provider",
            dataset_id="iteration204.stock_daily",
            operation_id="stock-daily-wire",
        ),
        RawResponseSource(
            provider_id="akshare",
            dataset_id="iteration204.stock_daily",
            operation_id="other-operation",
        ),
    ):
        wrong_identity_manifest = capture.manifest.model_copy(update={"source": wrong_source})
        with pytest.raises(ValueError, match="not the declared AkShare"):
            parse_verified_stock_daily_capture(
                replace(capture, manifest=wrong_identity_manifest), **parser_kwargs
            )


def test_verified_capture_rejects_wrong_scope_and_tampered_body(tmp_path: Path) -> None:
    """Bind evidence to the exact query digest and refuse changed content-addressed bytes."""
    from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

    request = _request()
    body = _body(_LINE)
    writer = RawResponseArtifactWriter(
        tmp_path / "raw-evidence", max_response_bytes=2 * 1024 * 1024
    )
    capture = writer.capture(
        (body,),
        source=RawResponseSource(
            provider_id="akshare",
            dataset_id="iteration204.stock_daily",
            operation_id="stock-daily-wire",
        ),
        request_summary_sha256=_request_digest(request),
        http_status=200,
        retention_class="ephemeral",
        source_history_limit="snapshot_only",
    )
    with pytest.raises(ValueError, match="request does not match"):
        parse_verified_stock_daily_capture(
            capture,
            artifact_writer=writer,
            request=_request(end_date="2026-10-23"),
            observed_at_utc=_OBSERVED_AT,
        )

    capture.artifact_path.write_bytes(b"x" * len(body))
    with pytest.raises(ValueError, match="hash or size verification"):
        parse_verified_stock_daily_capture(
            capture,
            artifact_writer=writer,
            request=request,
            observed_at_utc=_OBSERVED_AT,
        )
