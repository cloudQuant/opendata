"""Offline unit tests for the engine's body decoders: json, csv, tsv and zip_csv.

AC2-10 asks every provider model for a real extraction *and* a negative offline case, and the
failure mode this file exists to kill is the silent zero: an empty body, an archive with no such
member, a header with no data rows or a delimiter that never split anything must each raise a typed,
model-attributed refusal instead of handing the warehouse an empty table that prints 0.

SYNTHETIC FIXTURES ONLY. Every body here is written by this file or rendered by
``_engine/testing.py`` from a declaration; none of them is a recorded capture of OECD, FINRA,
SEC or Fama-French traffic, and none may be presented as one. The upstream shapes they imitate --
SDMX-CSV with a byte-order mark and a trailing separator, ``|``-separated FINRA downloads, one CSV
inside a zip -- are the blockers the credential-free model census names, and a live capture belongs
to its own model task.

No case reaches a socket: extraction runs through a synthetic transport, and the one case that
exercises the governed client replaces it with a local fake response object.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.decoders import (
    ResponseDecodeError,
    decode_body,
    decode_response,
)
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ColumnSpec, DecoderSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    fixture_context,
    synthetic_body,
    synthetic_raw_page,
)

SOURCE = "decodertest"
#: Columns a delimited body can carry: every kind a text cell can actually hold.
COLUMNS = (
    ColumnSpec("trade_date", "date", source_key="d", required=True, nullable=False),
    ColumnSpec("value", "float", source_key="v", required=True, nullable=False, units="usd"),
    ColumnSpec("shares", "int", source_key="n", required=True, nullable=False, units="shares"),
    ColumnSpec("label", "str", source_key="l"),
)
CSV_TEXT = 'd,v,n,l\n2026-01-01,1.5,100,alpha\n2026-01-02,2.5,200,"beta, gamma"\n'
TSV_TEXT = "d\tv\tn\tl\n2026-01-01\t1.5\t100\talpha\n2026-01-02\t2.5\t200\tbeta gamma\n"
PIPE_TEXT = "d|v|n|l\n2026-01-01|1.5|100|alpha\n2026-01-02|2.5|200|beta\n"


def decoder_spec(**overrides: object) -> ModelSpec:
    """One unpaged declaration reading a delimited body; defaults are synthetic fixture inputs."""
    fields: dict[str, object] = {
        "model": "DecoderProbe",
        "domain": "decoder_probe",
        "asset_class": "macro",
        "period": "d",
        "market": "us",
        "base_url": "https://decoder.test",
        "path": "/series/factors.csv",
        "columns": COLUMNS,
        "params": (ParamSpec("symbol", "str", required=True),),
        "scenario": "离线判定面：CSV/TSV/zip 响应体解码",
        "error_prefix": "DECODER",
    }
    fields.update(overrides)
    return ModelSpec(**fields)  # type: ignore[arg-type]


def fetch_through(spec: ModelSpec, response: HttpResponse) -> tuple[object, ...]:
    """Run one declaration's fetcher against one synthetic response body."""
    fetcher_type = make_http_json_fetcher(SOURCE, spec)
    fetcher_type.http_transport = FixedResponseTransport(response)  # type: ignore[attr-defined]
    return fetcher_type().fetch(  # type: ignore[attr-defined]
        ctx=fixture_context(SOURCE, spec, sends=2), symbol="ANY"
    )


def zip_bytes(members: dict[str, str]) -> bytes:
    """Build an in-memory archive holding exactly ``members`` -- synthetic, never a capture."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, text in members.items():
            bundle.writestr(name, text)
    return archive.getvalue()


class TestCsvAndTsvDecode:
    """A delimited body becomes header-keyed rows, and only under its own decoder."""

    def test_csv_body_decodes_every_row_the_engine_asks_for(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        rows = fetch_through(spec, HttpResponse(200, None, payload=CSV_TEXT.encode()))
        assert len(rows) == 2
        assert [row.value for row in rows] == [1.5, 2.5]
        assert [row.shares for row in rows] == [100, 200]
        assert [row.label for row in rows] == ["alpha", "beta, gamma"]

    def test_tsv_body_decodes_under_the_tsv_decoder(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("tsv"))
        rows = fetch_through(spec, HttpResponse(200, None, payload=TSV_TEXT.encode()))
        assert len(rows) == 2
        assert [row.label for row in rows] == ["alpha", "beta gamma"]

    def test_the_declared_delimiter_overrides_the_kind_default(self) -> None:
        """FINRA's downloads separate with ``|``; the delimiter is a declared interface fact."""
        spec = decoder_spec(decoder=DecoderSpec("csv", delimiter="|"))
        rows = fetch_through(spec, HttpResponse(200, None, payload=PIPE_TEXT.encode()))
        assert [row.shares for row in rows] == [100, 200]

    @pytest.mark.parametrize(
        ("decoder", "payload"),
        [
            (DecoderSpec("tsv"), CSV_TEXT.encode()),
            (DecoderSpec("csv"), TSV_TEXT.encode()),
            (DecoderSpec("csv"), PIPE_TEXT.encode()),
            (DecoderSpec("csv", delimiter="|"), CSV_TEXT.encode()),
        ],
        ids=["csv-as-tsv", "tsv-as-csv", "csv-as-pipe", "pipe-as-comma"],
    )
    def test_the_same_bytes_under_the_wrong_delimiter_refuse_not_zero_rows(
        self, decoder: DecoderSpec, payload: bytes
    ) -> None:
        spec = decoder_spec(decoder=decoder)
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, payload)
        assert raised.value.code == "DECODER_DELIMITER_MISMATCH"
        assert raised.value.model == "DecoderProbe"

    def test_csv_bytes_under_the_json_decoder_refuse(self) -> None:
        """The negative control for the default decoder: no parsed document, no answer."""
        spec = decoder_spec()
        with pytest.raises(ResponseDecodeError) as raised:
            decode_response(
                spec,
                HttpResponse(200, None, payload=CSV_TEXT.encode()),
                url="https://decoder.test",
            )
        assert raised.value.code == "DECODER_JSON_DECODE_UNAVAILABLE"

    def test_bom_and_trailing_separator_still_decode(self) -> None:
        """OECD SDMX-CSV ships a byte-order mark and an empty last column per line."""
        body = ("d,v,n,l,\r\n2026-01-01,1.5,10,alpha,,\r\n2026-01-02,2.5,20,beta,,\r\n").encode(
            "utf-8-sig"
        )
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        rows = fetch_through(spec, HttpResponse(200, None, payload=body))
        assert [row.trade_date.isoformat() for row in rows] == ["2026-01-01", "2026-01-02"]

    def test_a_blank_cell_is_a_null_not_an_empty_string(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        body = "d,v,n,l\n2026-01-01,1.5,10,\n"
        (row,) = fetch_through(spec, HttpResponse(200, None, payload=body.encode()))
        assert row.label is None

    def test_a_blank_cell_in_a_non_nullable_column_is_refused(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        body = "d,v,n,l\n2026-01-01,,10,alpha\n"
        with pytest.raises(ProviderEngineError) as raised:
            fetch_through(spec, HttpResponse(200, None, payload=body.encode()))
        assert raised.value.code == "DECODER_SHAPE_INVALID"
        assert "value" in raised.value.rejected


class TestZipMemberDecoder:
    """``zip_csv`` reads the member the selector names, and refuses any other reading."""

    def test_the_named_member_decodes(self) -> None:
        payload = zip_bytes({"F-F_Research_Data_Factors_CSV.csv": CSV_TEXT, "readme.txt": "notes"})
        spec = decoder_spec(
            path="/downloads/F-F_Research_Data_Factors.zip",
            decoder=DecoderSpec("zip_csv", member="F-F_Research_Data_Factors_CSV.csv"),
        )
        rows = fetch_through(spec, HttpResponse(200, None, payload=payload))
        assert len(rows) == 2
        assert [row.value for row in rows] == [1.5, 2.5]

    def test_a_glob_selector_and_a_prefixed_member_name_both_match(self) -> None:
        payload = zip_bytes({"csv/series.csv": TSV_TEXT, "metadata.json": "{}"})
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="*.csv", delimiter="\t"))
        rows = decode_body(spec, payload)
        assert len(rows) == 2

    def test_a_selector_naming_no_member_refuses_and_says_what_the_archive_holds(self) -> None:
        payload = zip_bytes({"data.txt": "not a table", "readme.md": "notes"})
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="*factors*.csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, payload)
        assert raised.value.code == "DECODER_ZIP_MEMBER_MISSING"
        assert raised.value.member == "*factors*.csv"
        assert "data.txt" in str(raised.value)

    def test_a_selector_matching_two_members_refuses_rather_than_choosing(self) -> None:
        payload = zip_bytes({"a.csv": CSV_TEXT, "b.csv": CSV_TEXT})
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="*.csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, payload)
        assert raised.value.code == "DECODER_ZIP_MEMBER_AMBIGUOUS"
        assert "a.csv" in raised.value.saw and "b.csv" in raised.value.saw

    def test_bytes_that_are_not_an_archive_refuse(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="data.csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, CSV_TEXT.encode())
        assert raised.value.code == "DECODER_BAD_RESPONSE"

    def test_an_empty_member_refuses_instead_of_publishing_an_empty_table(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="data.csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, zip_bytes({"data.csv": "\n\n"}))
        assert raised.value.code == "DECODER_EMPTY_BODY"
        assert raised.value.member == "data.csv"


class TestSilentZeroIsRefused:
    """Every way a body can be nothing at all is a typed failure, never an empty table."""

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (b"", "DECODER_EMPTY_BODY"),
            (b"   \r\n\n", "DECODER_EMPTY_BODY"),
            (b"d,v,n,l\n", "DECODER_EMPTY_TABLE"),
            (b"unrelated,columns\n1,2\n", "DECODER_HEADER_MISMATCH"),
        ],
        ids=["empty", "blank", "header-only", "unrelated-header"],
    )
    def test_a_body_with_no_rows_refuses(self, payload: bytes, code: str) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, payload)
        assert raised.value.code == code

    def test_a_delimited_declaration_with_no_payload_refuses(self) -> None:
        """A transport that returned a JSON document cannot satisfy a CSV declaration."""
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_response(spec, HttpResponse(200, [{"d": "2026-01-01"}]))
        assert raised.value.code == "DECODER_PAYLOAD_MISSING"

    def test_a_row_wider_than_the_header_refuses(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, b"d,v,n,l\n2026-01-01,1.5,10,alpha,extra\n")
        assert raised.value.code == "DECODER_ROW_WIDER_THAN_HEADER"

    def test_a_repeated_header_name_refuses(self) -> None:
        """DictReader-style collision would silently drop one column's values."""
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, b"d,v,v\n2026-01-01,1.5,2.5\n")
        assert raised.value.code == "DECODER_HEADER_DUPLICATE"

    def test_an_unnamed_middle_column_refuses(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, b"d,,n\n2026-01-01,1.5,10\n")
        assert raised.value.code == "DECODER_HEADER_INVALID"

    def test_a_refusal_names_the_model_the_decoder_and_what_it_saw(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="factors.csv"))
        with pytest.raises(ResponseDecodeError) as raised:
            decode_body(spec, zip_bytes({"other.csv": CSV_TEXT}))
        message = str(raised.value)
        for fragment in ("DECODER_ZIP_MEMBER_MISSING", "DecoderProbe", "zip_csv", "factors.csv"):
            assert fragment in message

    def test_a_decode_refusal_is_still_an_engine_error(self) -> None:
        """Every suite that catches the engine family keeps catching this one."""
        assert issubclass(ResponseDecodeError, ProviderEngineError)


class TestDeclarationRules:
    """A decoder the engine cannot carry out is refused while the record is written."""

    def test_the_default_decoder_is_json_and_changes_nothing(self) -> None:
        assert decoder_spec().decoder == DecoderSpec()
        assert not decoder_spec().decoder.delimited
        assert decode_response(decoder_spec(), HttpResponse(200, {"data": []})) == {"data": []}

    @pytest.mark.parametrize(
        ("kind", "options", "message"),
        [
            ("xml", {}, "unknown decoder kind"),
            ("json", {"delimiter": "|"}, "no delimiter"),
            ("json", {"member": "a.csv"}, "no zip member"),
            ("zip_csv", {}, "must name the member"),
            ("csv", {"member": "a.csv"}, "no member to select"),
            ("csv", {"delimiter": "||"}, "one non-line character"),
            ("csv", {"delimiter": "\n"}, "one non-line character"),
            ("csv", {"encoding": " "}, "cannot be blank"),
        ],
        ids=[
            "unknown-kind",
            "json-delimiter",
            "json-member",
            "zip-without-member",
            "csv-with-member",
            "two-character-delimiter",
            "newline-delimiter",
            "blank-encoding",
        ],
    )
    def test_malformed_decoders_are_refused(
        self, kind: str, options: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            decoder_spec(decoder=DecoderSpec(kind, **options))  # type: ignore[arg-type]

    def test_a_delimited_body_may_not_declare_a_rows_pointer(self) -> None:
        with pytest.raises(ValueError, match="no nested record list"):
            decoder_spec(decoder=DecoderSpec("csv"), rows_pointer="data.records")

    def test_effective_delimiter_is_the_kind_default_unless_declared(self) -> None:
        assert DecoderSpec("csv").effective_delimiter == ","
        assert DecoderSpec("tsv").effective_delimiter == "\t"
        assert DecoderSpec("zip_csv", member="a.csv").effective_delimiter == ","
        assert DecoderSpec("zip_csv", member="a.csv", delimiter=";").effective_delimiter == ";"


class TestProductionBodyReading:
    """The governed client keeps the bytes whole for a decoder and parses only for JSON."""

    class _RawResponse:
        """A stand-in for the shared client's response -- local, never a capture."""

        status_code = 200

        def __init__(self, content: bytes) -> None:
            """Hold the body bytes and the text a JSON path would parse."""
            self.content = content
            self.text = content.decode("utf-8")

    def _patch_client(self, monkeypatch: pytest.MonkeyPatch, response: object) -> None:
        from opendata.data import http_client

        class _Client:
            def get(self, *args: object, **kwargs: object) -> object:
                return response

        monkeypatch.setattr(http_client, "get_shared_http_client", lambda: _Client())

    def test_a_csv_body_arrives_as_payload_without_a_json_parse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_client(monkeypatch, TestProductionBodyReading._RawResponse(CSV_TEXT.encode()))
        response = http_json._http_get_json(
            "https://decoder.test/series/factors.csv",
            {},
            timeout=1.0,
            source="decodertest",
            decoder="csv",
        )
        assert response.payload == CSV_TEXT.encode()
        assert response.document is None

    def test_the_same_bytes_as_json_is_the_unchanged_bad_response_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_client(monkeypatch, TestProductionBodyReading._RawResponse(CSV_TEXT.encode()))
        with pytest.raises(ProviderEngineError) as raised:
            http_json._http_get_json(
                "https://decoder.test/series/factors.csv", {}, timeout=1.0, source="decodertest"
            )
        assert raised.value.code == "DECODERTEST_BAD_RESPONSE"

    def test_a_declared_decoder_reaches_the_engine_seam_without_a_json_parse(self) -> None:
        """The declaration's body shape travels with the send; the fake never sees a socket."""
        spec = decoder_spec(decoder=DecoderSpec("zip_csv", member="series.csv"))
        rows = fetch_through(
            spec, HttpResponse(200, None, payload=zip_bytes({"series.csv": CSV_TEXT}))
        )
        assert len(rows) == 2


class TestSyntheticBodyFixture:
    """The shared fixture renders the bytes a declaration reads, and refuses the wrong ask."""

    def test_synthetic_body_renders_the_declared_header_and_rows(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("tsv"))
        body = synthetic_body(spec, 3, start=0)
        assert body.decode().splitlines()[0] == "d\tv\tn\tl"
        assert len(decode_body(spec, body)) == 3

    def test_synthetic_raw_page_carries_no_document(self) -> None:
        spec = decoder_spec(decoder=DecoderSpec("csv"))
        page = synthetic_raw_page(spec, 2)
        assert page.document is None
        assert len(fetch_through(spec, page)) == 2

    def test_a_json_declaration_cannot_ask_for_a_delimited_body(self) -> None:
        with pytest.raises(AssertionError, match="no delimited body"):
            synthetic_body(decoder_spec(), 1)
