"""Branch-level tests for the immutable provider extractor-result captures.

The existing suite walks the accepted shapes. These tests walk the rejecting
ones: an identity that is not text, a cycle that closes through a list, a value
the JSON encoder refuses, a stored payload whose top level or element type is
wrong, a query context that is valid JSON but not the reviewed contract, and
every guard the writer runs before a capture may reach storage. Nothing here
reaches a database, a socket or a provider.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import date

import pytest

from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.pipeline.provider_model_raw import (
    MAX_QUERY_CONTEXT_BYTES,
    MAX_RAW_PAYLOAD_BYTES,
    MAX_RAW_ROWS,
    NativeRawCapture,
    _validated_query_payload,
    make_provider_model_capture,
    validate_provider_model_capture,
)

#: The encoder refuses integers longer than this many digits.
_HUGE_INTEGER = 10**5000
_CANONICAL = {"ensure_ascii": False, "allow_nan": False, "separators": (",", ":")}


def _fred_query(**updates: object) -> FredSeriesQuery:
    return FredSeriesQuery.model_validate(
        {
            "source": "fred",
            "market": "us",
            "series_id": "CPIAUCSL",
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 2, 1),
            **updates,
        }
    )


def _bls_query(**updates: object) -> BlsSeriesQuery:
    return BlsSeriesQuery.model_validate(
        {
            "source": "bls",
            "market": "us",
            "series_ids": ["LNS14000000"],
            "start_year": 2025,
            "end_year": 2025,
            "max_requests": 2,
            **updates,
        }
    )


def _capture(
    *,
    source: str = "fred",
    model: str = "FredSeries",
    raw: list[dict[str, object]] | None = None,
    params: object | None = None,
) -> NativeRawCapture:
    selected_params = _fred_query() if params is None else params
    return make_provider_model_capture(
        source=source,
        model=model,
        raw=[] if raw is None else raw,
        params=selected_params,  # type: ignore[arg-type]
    )


def _rehash(capture: NativeRawCapture, **updates: object) -> NativeRawCapture:
    """Mutate a capture and keep digests consistent so the next guard bites."""
    forged = replace(capture, **updates)
    if "raw_json" in updates:
        digest = hashlib.sha256(str(forged.raw_json).encode("utf-8")).hexdigest()
        forged = replace(forged, raw_sha256=digest)
    if "query_json" in updates:
        digest = hashlib.sha256(str(forged.query_json).encode("utf-8")).hexdigest()
        forged = replace(forged, query_sha256=digest)
    return forged


def _query_text(**mutations: object) -> str:
    """The canonical fred context with the supplied fields rewritten."""
    payload = json.loads(_capture().query_json)
    payload.update(mutations)
    return json.dumps(payload, **_CANONICAL)


def _reordered_query_text() -> str:
    """The canonical context with its object keys emitted in reverse order."""
    payload = json.loads(_capture().query_json)
    reordered = {key: payload[key] for key in reversed(list(payload))}
    return json.dumps(reordered, **_CANONICAL)


class TestReviewedIdentity:
    """Only a reviewed (source, model) pair over plain text is a binding."""

    def test_identity_that_is_not_text_is_not_reviewed(self) -> None:
        capture = _capture()

        for source, model in ((7, "FredSeries"), ("fred", None), (b"fred", "FredSeries")):
            with pytest.raises(ValueError, match="capture identity is not reviewed"):
                make_provider_model_capture(  # type: ignore[arg-type]
                    source=source,
                    model=model,
                    raw=[{"value": "1.0"}],
                    params=_fred_query(),
                )
            with pytest.raises(ValueError, match="capture identity is not reviewed"):
                validate_provider_model_capture(  # type: ignore[arg-type]
                    source=source,
                    model=model,
                    capture=capture,
                )

    def test_a_reviewed_model_from_another_family_is_not_the_binding(self) -> None:
        with pytest.raises(ValueError, match="does not match its reviewed identity"):
            _validated_query_payload("fred", "FredSeries", _bls_query())

    def test_query_fields_outside_the_model_are_rejected_without_echoing_them(self) -> None:
        query = _fred_query()
        query.__pydantic_extra__ = {"api_key": "injected-secret"}

        with pytest.raises(ValueError, match="reviewed contract") as exc_info:
            _capture(params=query)

        assert "injected-secret" not in str(exc_info.value)


class TestQueryPayloadGuards:
    """The dump/validate/re-dump loop rejects every shape it cannot prove."""

    def test_python_dump_must_carry_exactly_the_reviewed_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            FredSeriesQuery,
            "model_dump",
            lambda self, *args, **kwargs: {"source": "fred"},
        )

        with pytest.raises(ValueError, match="reviewed contract"):
            _capture()

    def test_revalidated_query_must_be_the_reviewed_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        query = _fred_query()
        monkeypatch.setattr(
            FredSeriesQuery,
            "model_validate",
            classmethod(lambda cls, payload, **kwargs: dict(payload)),
        )

        with pytest.raises(ValueError, match="reviewed contract"):
            _capture(params=query)

    def test_json_dump_must_be_a_mapping_of_the_reviewed_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_dump = FredSeriesQuery.model_dump

        def dump(self: object, *args: object, **kwargs: object) -> object:
            if kwargs.get("mode") == "json":
                return ["not", "a", "mapping"]
            return real_dump(self, *args, **kwargs)

        monkeypatch.setattr(FredSeriesQuery, "model_dump", dump)

        with pytest.raises(ValueError, match="reviewed contract"):
            _capture()


class TestRawPayloadGuards:
    """A cycle through a list and an unencodable number are both refusals."""

    def test_cycle_closed_through_a_list_is_rejected(self) -> None:
        loop: list[object] = []
        loop.append(loop)

        with pytest.raises(ValueError, match="contains a cycle"):
            _capture(raw=[{"loop": loop}])

    @pytest.mark.parametrize(
        "value",
        [
            _HUGE_INTEGER,
            chr(0xD800),
            {"nested": [_HUGE_INTEGER]},
        ],
        ids=["huge-integer", "lone-surrogate", "nested-huge-integer"],
    )
    def test_value_the_encoder_cannot_write_is_rejected(self, value: object) -> None:
        with pytest.raises(ValueError, match="invalid or exceeds its size limit"):
            _capture(raw=[{"value": value}])

    def test_row_limit_is_inclusive_and_survives_writer_validation(self) -> None:
        capture = _capture(raw=[{"index": row} for row in range(MAX_RAW_ROWS)])

        assert capture.raw_row_count == MAX_RAW_ROWS
        assert (
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=capture,
            )
            is capture
        )


class TestWriterCaptureGuards:
    """Nothing forged after the capture reaches a storage writer."""

    def test_only_the_reviewed_capture_type_is_accepted(self) -> None:
        class Impostor:
            source = "fred"

        with pytest.raises(ValueError, match="capture has an unknown type"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=Impostor(),  # type: ignore[arg-type]
            )

    def test_field_set_is_rechecked_against_the_contract(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture = _capture(raw=[{"value": "1.0"}])
        stripped = {
            field.name: field
            for field in fields(NativeRawCapture)
            if field.name != "schema_version"
        }
        monkeypatch.setattr(NativeRawCapture, "__dataclass_fields__", stripped)

        with pytest.raises(ValueError, match="fields differ from the reviewed contract"):
            validate_provider_model_capture(source="fred", model="FredSeries", capture=capture)

    @pytest.mark.parametrize(
        ("mutation", "expected"),
        [
            ({"model": "FredSeriesV2"}, "capture model does not match the writer"),
            ({"model": None}, "capture model does not match the writer"),
            ({"raw_scope": None}, "capture raw scope is invalid"),
            ({"schema_version": "1"}, "capture schema version is invalid"),
            ({"raw_row_count": -1}, "capture row count is invalid"),
            ({"raw_row_count": MAX_RAW_ROWS + 1}, "capture row count is invalid"),
            ({"raw_row_count": "1"}, "capture row count is invalid"),
            ({"raw_sha256": 12345}, "capture text fields are invalid"),
            ({"query_json": None}, "capture text fields are invalid"),
        ],
    )
    def test_outer_fields_are_rechecked(
        self,
        mutation: dict[str, object],
        expected: str,
    ) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match=expected):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=replace(capture, **mutation),
            )

    def test_payloads_over_the_byte_limit_are_refused_before_parsing(self) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="exceeds its payload size limit"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(
                    capture,
                    query_json=capture.query_json + " " * MAX_QUERY_CONTEXT_BYTES,
                ),
            )

        with pytest.raises(ValueError, match="exceeds its payload size limit"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(
                    capture,
                    raw_json=capture.raw_json + " " * MAX_RAW_PAYLOAD_BYTES,
                ),
            )

    def test_query_digest_is_rechecked(self) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="query digest does not match its context"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=replace(capture, query_sha256="f" * 64),
            )


class TestStoredPayloadGuards:
    """A stored payload is reparsed, recanonicalised and rebound to its query."""

    @pytest.mark.parametrize(
        ("raw_json", "row_count"),
        [
            ('{"value":"1.0"}', 1),
            ('"not-a-list"', 0),
            ("null", 0),
        ],
    )
    def test_top_level_shape_must_be_a_bounded_list(
        self,
        raw_json: str,
        row_count: int,
    ) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="invalid top-level shape"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(replace(capture, raw_row_count=row_count), raw_json=raw_json),
            )

    def test_stored_row_count_must_match_the_stored_payload(self) -> None:
        capture = _capture(raw=[{"value": "1.0"}])
        two_rows = '[{"value":"1.0"},{"value":"2.0"}]'

        with pytest.raises(ValueError, match="row count does not match its payload"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(
                    replace(capture, raw_row_count=1),
                    raw_json=two_rows,
                ),
            )

    def test_row_limit_of_a_stored_payload_is_enforced(self) -> None:
        capture = _capture(raw=[{"value": "1.0"}])
        too_many = "[" + ",".join(["{}"] * (MAX_RAW_ROWS + 1)) + "]"

        with pytest.raises(ValueError, match="invalid top-level shape"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(
                    replace(capture, raw_row_count=MAX_RAW_ROWS),
                    raw_json=too_many,
                ),
            )

    @pytest.mark.parametrize("raw_json", ["[1]", '[{"value":"1.0"}, "text"]', "[null]"])
    def test_rows_must_be_objects(self, raw_json: str) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="must contain only objects"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(capture, raw_json=raw_json),
            )

    @pytest.mark.parametrize("raw_json", ['[{"value":NaN}]', '[{"value":Infinity}]'])
    def test_non_finite_literals_never_reach_the_writer(self, raw_json: str) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="raw payload is invalid"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(capture, raw_json=raw_json),
            )

    @pytest.mark.parametrize(
        "mutations",
        [{"market": "cn"}, {"source": "bls"}, {"market": None}],
    )
    def test_query_context_identity_is_rebound(
        self,
        mutations: dict[str, object],
    ) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="context does not satisfy its reviewed contract"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(capture, query_json=_query_text(**mutations)),
            )

    def test_query_context_model_type_is_rechecked(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            FredSeriesQuery,
            "model_validate_json",
            classmethod(lambda cls, payload, **kwargs: {"not": "a query"}),
        )
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="context does not satisfy its reviewed contract"):
            validate_provider_model_capture(source="fred", model="FredSeries", capture=capture)

    def test_query_context_must_be_canonical(self) -> None:
        capture = _capture(raw=[{"value": "1.0"}])

        with pytest.raises(ValueError, match="query context is not canonical JSON"):
            validate_provider_model_capture(
                source="fred",
                model="FredSeries",
                capture=_rehash(capture, query_json=_reordered_query_text()),
            )

    def test_a_clean_capture_survives_the_whole_writer_gate(self) -> None:
        capture = _capture(raw=[{"value": "."}, {"value": "3.1"}])

        checked = validate_provider_model_capture(
            source="fred",
            model="FredSeries",
            capture=capture,
        )

        assert checked is capture
        assert checked.raw_row_count == 2
        assert json.loads(checked.raw_json) == [{"value": "."}, {"value": "3.1"}]
        assert json.loads(checked.query_json)["series_id"] == "CPIAUCSL"
        assert hashlib.sha256(checked.raw_json.encode("utf-8")).hexdigest() == checked.raw_sha256


def test_equity_and_bls_bindings_validate_their_own_contexts() -> None:
    fmp = EquityHistoricalQuery.model_validate({"source": "fmp", "market": "us", "symbol": "BRK.B"})
    fmp_capture = _capture(
        source="fmp",
        model="EquityHistorical",
        params=fmp,
        raw=[{"close": 102.0}],
    )
    bls_capture = _capture(
        source="bls",
        model="BlsSeries",
        params=_bls_query(),
        raw=[{"series_id": "LNS14000000", "value": None}],
    )

    assert (
        validate_provider_model_capture(
            source="fmp",
            model="EquityHistorical",
            capture=fmp_capture,
        )
        is fmp_capture
    )
    assert json.loads(bls_capture.raw_json)[0]["value"] is None
    assert (
        validate_provider_model_capture(
            source="bls",
            model="BlsSeries",
            capture=bls_capture,
        ).raw_scope
        == "extract_data_output"
    )
