"""Body decoders: the response bytes a declaration says it is, turned into record rows.

The engine's original face was JSON-only -- :func:`resolve_rows` follows a pointer inside the
parsed document. A census of the credential-free provider models found the two largest blockers
were shapes the JSON path cannot reach at all: delimited text (OECD SDMX-CSV, FINRA's
``|``-separated CSV) and CSV shipped inside an archive (Fama-French factor zips, the SEC
``EquityFTD`` download). Both are expressed by :class:`DecoderSpec` and decoded here, from the
bytes the transport returned -- never from a file extension or a URL suffix, which is a guess
about a body that has not arrived yet.

Every decoder produces the same shape the JSON path produces: a list of dicts keyed by the header
the body itself published. Values stay text; the declared column kinds are what turn ``"1.5"`` into
a float or ``"2026-01-01"`` into a date, and a blank cell is read as a null rather than as an empty
string, because that is the distinction AC2-10 asks each model to keep.

Nothing here performs I/O. Failures are :class:`ResponseDecodeError`, which names the model, the
decoder, the archive member it selected and what it actually saw -- the silent zero-row table is
the failure mode this module exists to refuse.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import zipfile
from typing import TYPE_CHECKING, Any, NoReturn

from opendata.data.providers._engine.http_json import ProviderEngineError

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpResponse
    from opendata.data.providers._engine.spec import DecoderSpec, ModelSpec

#: How many member names or header cells one refusal echoes; enough to diagnose, bounded so a
#: several-hundred-column archive or a megabyte body cannot turn an error into a log flood.
_ECHO_LIMIT = 8

#: A refusal per decoder face, so a reviewer can tell which guard fired from the code alone.
EMPTY_BODY = "EMPTY_BODY"
EMPTY_TABLE = "EMPTY_TABLE"
PAYLOAD_MISSING = "PAYLOAD_MISSING"
ZIP_MEMBER_MISSING = "ZIP_MEMBER_MISSING"
ZIP_MEMBER_AMBIGUOUS = "ZIP_MEMBER_AMBIGUOUS"
DELIMITER_MISMATCH = "DELIMITER_MISMATCH"
HEADER_INVALID = "HEADER_INVALID"
HEADER_DUPLICATE = "HEADER_DUPLICATE"
HEADER_MISMATCH = "HEADER_MISMATCH"
ROW_WIDER_THAN_HEADER = "ROW_WIDER_THAN_HEADER"
BAD_BODY = "BAD_RESPONSE"
JSON_DECODE_UNAVAILABLE = "JSON_DECODE_UNAVAILABLE"


class ResponseDecodeError(ProviderEngineError):
    """A response body that cannot become the record rows the declaration describes.

    Attributes:
        model: The declared model whose body was refused.
        decoder: Decoder kind the declaration asked for.
        member: Archive member selected, when the body was an archive.
        saw: What the decoder actually found, already attributed.
    """

    def __init__(
        self,
        code: str,
        *,
        model: str,
        decoder: str,
        saw: str,
        member: str | None = None,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        """Store the attribution a silent zero-row failure would have hidden."""
        super().__init__(code, status=status, url=url)
        self.model = model
        self.decoder = decoder
        self.member = member
        self.saw = saw

    def __str__(self) -> str:
        """Report the code, the model, the decoder, the member and what was seen."""
        base = super().__str__()
        member = "" if self.member is None else f" member={self.member!r}"
        return f"{base} model={self.model} decoder={self.decoder!r}{member} saw={self.saw}"


def _refuse(
    spec: ModelSpec,
    suffix: str,
    saw: str,
    *,
    member: str | None = None,
    url: str | None = None,
) -> NoReturn:
    """Raise the model-attributed decode refusal for ``suffix``."""
    raise ResponseDecodeError(
        f"{spec.error_prefix}_{suffix}",
        model=spec.model,
        decoder=spec.decoder.kind,
        saw=saw,
        member=member,
        url=url,
    )


def decode_response(
    spec: ModelSpec,
    response: HttpResponse,
    *,
    url: str | None = None,
) -> Any:  # noqa: ANN401 - the document shape a declaration selects
    """Return the record document ``spec``'s decoder reads out of one response.

    A JSON declaration hands back the document the transport parsed, exactly as before. A delimited
    declaration refuses a transport that returned no body at all rather than reading a document it
    never asked for: an absent payload is a wiring mistake, and treating it as an empty answer is
    how an instrument ends up printing 0.
    """
    decoder = spec.decoder
    if not decoder.delimited:
        if response.document is None and response.payload is not None:
            return _refuse(
                spec,
                JSON_DECODE_UNAVAILABLE,
                f"a raw body of {len(response.payload or '')} byte(s) with no parsed document",
                url=url,
            )
        return response.document
    if response.payload is None:
        _refuse(
            spec,
            PAYLOAD_MISSING,
            f"no raw body on the response for a {decoder.kind} decoder",
            url=url,
        )
    return decode_body(spec, response.payload, url=url)


def decode_body(
    spec: ModelSpec,
    payload: bytes | str,
    *,
    url: str | None = None,
) -> list[dict[str, Any]]:
    """Decode one raw body into header-keyed rows, refusing a silent empty table.

    Raises:
        ResponseDecodeError: The body is empty, is not an archive or has no member the selector
            names, or its header does not describe the delimited table the declaration claims.
    """
    decoder = spec.decoder
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    if not body.strip():
        _refuse(
            spec,
            EMPTY_BODY,
            f"{len(body)} byte(s), all blank, in the response body",
            url=url,
        )
    member: str | None = None
    if decoder.kind == "zip_csv":
        body, member = _selected_member(spec, body, decoder, url)
    if not body.strip():
        _refuse(
            spec,
            EMPTY_BODY,
            f"{len(body)} byte(s), all blank, in {member or 'the response body'}",
            member=member,
            url=url,
        )
    return _rows_from_text(spec, _as_text(spec, body, member, url), decoder, member, url)


def _selected_member(
    spec: ModelSpec, body: bytes, decoder: DecoderSpec, url: str | None
) -> tuple[bytes, str]:
    """Open the archive and return the bytes of the single member the selector names.

    Raises:
        ResponseDecodeError: The bytes are not an archive, or the selector names no member or more
            than one. A zip read as an arbitrary member would be a different model's answer.
    """
    selector = decoder.member or ""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        return _refuse(
            spec,
            BAD_BODY,
            f"{len(body)} byte(s) that is not a zip archive",
            member=selector,
            url=url,
        )
    with archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        matches = sorted(name for name in names if _member_matches(name, selector))
        if not matches:
            return _refuse(
                spec,
                ZIP_MEMBER_MISSING,
                (
                    f"{len(names)} archive member(s), none named {selector!r}"
                    f" (saw {names[:_ECHO_LIMIT]})"
                ),
                member=selector,
                url=url,
            )
        if len(matches) > 1:
            return _refuse(
                spec,
                ZIP_MEMBER_AMBIGUOUS,
                f"{len(matches)} member(s) match {selector!r}: {matches[:_ECHO_LIMIT]}",
                member=selector,
                url=url,
            )
        chosen = matches[0]
        info = archive.getinfo(chosen)
        if info.flag_bits & 0x1:
            return _refuse(
                spec, BAD_BODY, f"member {chosen!r} is encrypted", member=chosen, url=url
            )
        return archive.read(info), chosen


def _member_matches(name: str, selector: str) -> bool:
    """Whether archive member ``name`` is the one ``selector`` names.

    Exact, then basename, then a glob over either -- the forms a reviewer can read straight off the
    declaration. Case is compared as published, because archive member names are case-sensitive.
    """
    tail = name.rsplit("/", 1)[-1]
    selector_tail = selector.rsplit("/", 1)[-1]
    if name == selector or tail == selector_tail:
        return True
    return fnmatch.fnmatch(tail, selector_tail) or fnmatch.fnmatch(name, selector)


def _try_decode(body: bytes, encoding: str) -> str | None:
    """Decode ``body`` as ``encoding``, or report that neither it nor its name worked."""
    try:
        return body.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _as_text(spec: ModelSpec, body: bytes, member: str | None, url: str | None) -> str:
    """Decode the body bytes to text in the declared encoding, then the defaults."""
    for encoding in spec.decoder.encodings:
        text = _try_decode(body, encoding)
        if text is not None:
            return text
    return _refuse(
        spec,
        BAD_BODY,
        f"{len(body)} byte(s) undecodable as {'/'.join(spec.decoder.encodings)}",
        member=member,
        url=url,
    )


def _rows_from_text(
    spec: ModelSpec,
    text: str,
    decoder: DecoderSpec,
    member: str | None,
    url: str | None,
) -> list[dict[str, Any]]:
    """Split delimited ``text`` into header-keyed rows."""
    delimiter = decoder.effective_delimiter
    table = [
        row
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    preamble = decoder.preamble_rows
    if preamble:
        # The count is taken from the blank-filtered rows because that is what the source's own
        # reader counts: upstream hands pandas ``header=5`` with blank-line skipping already on.
        if len(table) <= preamble:
            return _refuse(
                spec,
                EMPTY_TABLE,
                f"a body of {len(table)} row(s) with a declared preamble of {preamble} "
                "leaves no header row",
                member=member,
                url=url,
            )
        table = table[preamble:]
    if not table:
        return _refuse(
            spec, EMPTY_TABLE, f"no header row in {len(text)} character(s)", member=member, url=url
        )
    names = _header_names(spec, table[0], delimiter, member, url)
    declared = {column.source_key or column.name for column in spec.columns}
    if not declared & set(names):
        return _refuse(
            spec,
            HEADER_MISMATCH,
            f"header {names[:_ECHO_LIMIT]} publishes none of the declared columns",
            member=member,
            url=url,
        )
    rows = [_row_dict(spec, names, record, member, url) for record in table[1:]]
    if not rows:
        return _refuse(
            spec,
            EMPTY_TABLE,
            f"a header of {len(names)} column(s) and 0 data row(s)",
            member=member,
            url=url,
        )
    return rows


def _header_names(
    spec: ModelSpec, header: list[str], delimiter: str, member: str | None, url: str | None
) -> list[str]:
    """Return the header's column names, trimmed of the blanks a trailing separator leaves.

    Raises:
        ResponseDecodeError: A column is unnamed, two columns share a name, or the split produced
            one column while the declaration describes more -- which is the wrong-delimiter reading,
            not a narrow table.
    """
    names = [cell.strip().lstrip("\ufeff") for cell in header]
    while names and not names[-1]:
        names.pop()
    if not names:
        return _refuse(spec, HEADER_INVALID, "an empty header row", member=member, url=url)
    if any(not name for name in names):
        return _refuse(
            spec,
            HEADER_INVALID,
            f"header {header[:_ECHO_LIMIT]} has an unnamed column",
            member=member,
            url=url,
        )
    if len(names) != len(set(names)):
        return _refuse(
            spec,
            HEADER_DUPLICATE,
            f"header {names[:_ECHO_LIMIT]} repeats a column name",
            member=member,
            url=url,
        )
    if len(names) == 1 and len(spec.columns) > 1:
        return _refuse(
            spec,
            DELIMITER_MISMATCH,
            (
                f"delimiter {delimiter!r} produced 1 column from header {names[0]!r},"
                f" while {len(spec.columns)} column(s) are declared"
            ),
            member=member,
            url=url,
        )
    return names


def _row_dict(
    spec: ModelSpec,
    names: list[str],
    record: list[str],
    member: str | None,
    url: str | None,
) -> dict[str, Any]:
    """Build one header-keyed row; a blank cell is a null, not an empty string."""
    extra = record[len(names) :]
    if any(cell.strip() for cell in extra):
        return _refuse(
            spec,
            ROW_WIDER_THAN_HEADER,
            f"a row of {len(record)} value(s) against {len(names)} header column(s)",
            member=member,
            url=url,
        )
    row: dict[str, Any] = {}
    for index, name in enumerate(names):
        cell = record[index].strip() if index < len(record) else ""
        row[name] = cell or None
    return row
