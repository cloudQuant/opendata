"""Bounded EDGAR Form 4 search and ownership XML normalization."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import quote, urlencode, urlparse

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pydantic import Field, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.sec_iter2 import SecInsiderOwner, SecInsiderTransaction
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.sec.models._common import (
    MAX_XML_RESPONSE_BYTES,
    SecProviderError,
    canonical_cik,
    company_ticker_rows,
    ensure_request_capacity,
    fetch_bytes,
    fetch_json,
    resolve_ticker,
)

if TYPE_CHECKING:
    # B405: type-only import; runtime parsing uses defusedxml.
    from xml.etree.ElementTree import (  # nosec B405
        Element,
    )

SEC_BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
MAX_FEED_PAGE_SIZE = 100
MAX_FEED_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_FEED_PAGES = 3
MAX_FORM4_FILINGS = 10
MAX_INSIDER_WINDOW_DAYS = 120
MAX_TRANSACTION_ROWS = 10_000
MAX_INSIDER_LIMIT = 1_000
_TICKER_RE = re.compile(r"^[A-Za-z0-9.-]{1,20}$")
_ACCESSION_RE = re.compile(r"(?<!\d)(\d{10}-\d{2}-\d{6})(?!\d)")
_SAFE_XML_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.xml$", re.I)
_DECIMAL_RE = re.compile(r"^-?(?:\d+)(?:\.\d+)?$")


class InsiderTradingQuery(QueryParams):
    """Search a bounded issuer filing-date interval for Form 4 ownership XML."""

    symbol: str
    start_date: date = Field(default_factory=lambda: date.today() - timedelta(days=120))
    end_date: date = Field(default_factory=date.today)
    limit: int | None = Field(default=100, ge=1, le=MAX_INSIDER_LIMIT, strict=True)
    ownership_type: Literal["D", "I"] | None = None
    sort_by: Literal["filing_date", "updated_on"] = "filing_date"
    use_cache: bool | None = True
    market: Literal["us"] = "us"

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if _TICKER_RE.fullmatch(value) is None:
            raise ValueError("symbol must be a short ticker token")
        return value

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _strict_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("filing bounds must be dates without times")
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("filing bounds must use YYYY-MM-DD") from exc
            if parsed.isoformat() != value:
                raise ValueError("filing bounds must use YYYY-MM-DD")
            return parsed
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> InsiderTradingQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days > MAX_INSIDER_WINDOW_DAYS:
            raise ValueError("filing window may not exceed 120 days")
        if self.use_cache is False:
            raise ValueError("use_cache=False is unsupported by the governed SEC transport")
        return self


@dataclass(frozen=True)
class SecForm4Reference:
    """One SEC Atom filing reference, before its filing index/XML stages."""

    issuer_cik: str
    filing_cik: str
    accession_number: str
    filing_date: date
    form: Literal["4", "4/A"]


@dataclass(frozen=True)
class SecForm4Raw:
    """One filing reference plus its bounded primary XML payload."""

    reference: SecForm4Reference
    body: bytes


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: Element, name: str) -> list[Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _child(element: Element, name: str) -> Element | None:
    matches = _children(element, name)
    if len(matches) > 1:
        raise SecProviderError("SEC_INSIDER_XML_AMBIGUOUS_FIELD")
    return matches[0] if matches else None


def _text(element: Element | None) -> str | None:
    if element is None:
        return None
    value = " ".join("".join(element.itertext()).split())
    return value or None


def _nested_value(element: Element | None) -> str | None:
    if element is None:
        return None
    value = _child(element, "value")
    return _text(value) if value is not None else _text(element)


def _parse_iso_date(value: str | None, error_code: str) -> date:
    if value is None:
        raise SecProviderError(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SecProviderError(error_code) from None
    if parsed.isoformat() != value:
        raise SecProviderError(error_code)
    return parsed


def _parse_decimal(value: str | None, *, required: bool = False) -> Decimal | None:
    if value is None:
        if required:
            raise SecProviderError("SEC_INSIDER_XML_MISSING_NUMERIC_FIELD")
        return None
    if _DECIMAL_RE.fullmatch(value) is None:
        raise SecProviderError("SEC_INSIDER_XML_BAD_NUMERIC_FIELD")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise SecProviderError("SEC_INSIDER_XML_BAD_NUMERIC_FIELD") from None
    if not parsed.is_finite() or parsed < 0:
        raise SecProviderError("SEC_INSIDER_XML_BAD_NUMERIC_FIELD")
    return parsed


def _public_float(value: Decimal | None) -> float | None:
    """Project an SEC decimal into the public v4 float field safely."""
    if value is None:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise SecProviderError("SEC_INSIDER_XML_BAD_NUMERIC_FIELD") from None
    if not math.isfinite(result) or (value != 0 and result == 0.0):
        raise SecProviderError("SEC_INSIDER_XML_BAD_NUMERIC_FIELD")
    return result


def _parse_optional_date(value: str | None, error_code: str) -> date | None:
    return None if value is None else _parse_iso_date(value, error_code)


def _parse_optional_int(value: str | None, error_code: str) -> int | None:
    if value is None:
        return None
    if not value.isdigit():
        raise SecProviderError(error_code)
    return int(value)


def _parse_acquired_disposed(value: str | None) -> Literal["A", "D"]:
    if value == "A":
        return "A"
    if value == "D":
        return "D"
    raise SecProviderError("SEC_INSIDER_XML_BAD_ACQUIRED_DISPOSED")


def _parse_direct_indirect(value: str | None) -> Literal["D", "I"] | None:
    if value == "D":
        return "D"
    if value == "I":
        return "I"
    if value is None:
        return None
    raise SecProviderError("SEC_INSIDER_XML_BAD_OWNERSHIP_TYPE")


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise SecProviderError("SEC_INSIDER_XML_BAD_BOOLEAN")


def parse_form4_feed_page(
    body: bytes,
    *,
    issuer_cik: str,
    start_date: date,
    end_date: date,
) -> tuple[SecForm4Reference, ...]:
    """Read the SEC's company-search Atom feed, retaining only Form 4 filings."""
    if len(body) > MAX_FEED_RESPONSE_BYTES:
        raise SecProviderError("SEC_INSIDER_FEED_TOO_LARGE")
    try:
        root = SafeET.fromstring(body)
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        raise SecProviderError("SEC_INSIDER_FEED_BAD_XML") from None
    if _local_name(root.tag) not in {"feed", "rss"}:
        raise SecProviderError("SEC_INSIDER_FEED_BAD_SHAPE")
    references: list[SecForm4Reference] = []
    seen: set[str] = set()
    for entry in (element for element in root.iter() if _local_name(element.tag) == "entry"):
        terms = [
            element.attrib.get("term", "").strip()
            for element in entry.iter()
            if _local_name(element.tag) == "category"
        ]
        form = next((term for term in terms if term in {"4", "4/A"}), None)
        if form is None:
            continue
        link = next(
            (
                element.attrib.get("href")
                for element in entry.iter()
                if _local_name(element.tag) == "link" and element.attrib.get("href")
            ),
            None,
        )
        if not isinstance(link, str):
            raise SecProviderError("SEC_INSIDER_FEED_BAD_ENTRY")
        parsed_link = urlparse(link)
        if parsed_link.scheme != "https" or parsed_link.netloc.casefold() not in {
            "sec.gov",
            "www.sec.gov",
        }:
            raise SecProviderError("SEC_INSIDER_FEED_BAD_ENTRY")
        path_match = re.match(
            r"^/Archives/edgar/data/(?P<cik>\d+)/(?P<accession>\d{10}-\d{2}-\d{6})-index\.htm$",
            parsed_link.path,
            re.I,
        )
        if path_match is None:
            raise SecProviderError("SEC_INSIDER_FEED_BAD_ENTRY")
        filing_cik = canonical_cik(path_match.group("cik"))
        accession = path_match.group("accession")
        if canonical_cik(accession[:10]) != filing_cik:
            raise SecProviderError("SEC_INSIDER_FEED_BAD_ENTRY")
        if accession in seen:
            continue
        filing_date_text = None
        for element in entry.iter():
            if _local_name(element.tag) in {"filing-date", "filingDate"}:
                filing_date_text = _text(element)
                break
        if filing_date_text is None:
            for element in entry.iter():
                if _local_name(element.tag) in {"updated", "published"}:
                    candidate = _text(element)
                    if candidate is not None:
                        filing_date_text = candidate[:10]
                        break
        filed = _parse_iso_date(filing_date_text, "SEC_INSIDER_FEED_BAD_DATE")
        if not start_date <= filed <= end_date:
            continue
        seen.add(accession)
        references.append(
            SecForm4Reference(
                issuer_cik=issuer_cik,
                filing_cik=filing_cik,
                accession_number=accession,
                filing_date=filed,
                form=form,
            )
        )
    references.sort(key=lambda item: (item.filing_date, item.accession_number), reverse=True)
    return tuple(references)


def _parse_index_xml_name(document: Mapping[str, object], form: str) -> str:
    directory = document.get("directory")
    if not isinstance(directory, Mapping):
        raise SecProviderError("SEC_INSIDER_INDEX_BAD_SHAPE")
    items = directory.get("item")
    if not isinstance(items, list):
        raise SecProviderError("SEC_INSIDER_INDEX_BAD_SHAPE")
    candidates: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise SecProviderError("SEC_INSIDER_INDEX_BAD_SHAPE")
        name = item.get("name")
        kind = item.get("type")
        if (
            isinstance(name, str)
            and _SAFE_XML_FILENAME_RE.fullmatch(name)
            and isinstance(kind, str)
            and kind in {form, "4"}
        ):
            candidates.append(name)
    if len(candidates) != 1:
        raise SecProviderError("SEC_INSIDER_PRIMARY_XML_AMBIGUOUS")
    return candidates[0]


def _filing_urls(reference: SecForm4Reference) -> tuple[str, str]:
    archive = (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(reference.filing_cik)}/{reference.accession_number.replace('-', '')}"
    )
    return f"{archive}/index.json", archive


def resolve_primary_xml_url(
    reference: SecForm4Reference,
    index_document: Mapping[str, object],
) -> str:
    """Resolve one unambiguous XML Form 4 document from an official filing index."""
    filename = _parse_index_xml_name(index_document, reference.form)
    _, archive = _filing_urls(reference)
    return f"{archive}/{quote(filename, safe='')}"


def _value(element: Element | None, name: str) -> str | None:
    return _nested_value(_child(element, name)) if element is not None else None


def parse_form4_xml(
    body: bytes,
    reference: SecForm4Reference,
) -> tuple[SecInsiderTransaction, ...]:
    """Parse both ownership transaction tables using the SEC XML field structure."""
    if len(body) > MAX_XML_RESPONSE_BYTES:
        raise SecProviderError("SEC_INSIDER_XML_TOO_LARGE")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise SecProviderError("SEC_INSIDER_XML_UNSAFE_DECLARATION")
    try:
        root = SafeET.fromstring(body)
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        raise SecProviderError("SEC_INSIDER_XML_BAD_XML") from None
    if _local_name(root.tag) != "ownershipDocument":
        raise SecProviderError("SEC_INSIDER_XML_BAD_SHAPE")
    root_form = _text(_child(root, "documentType"))
    if root_form is not None and root_form != reference.form:
        raise SecProviderError("SEC_INSIDER_FORM_MISMATCH")
    issuer = _child(root, "issuer")
    if issuer is None:
        raise SecProviderError("SEC_INSIDER_XML_MISSING_ISSUER")
    issuer_cik = canonical_cik(_text(_child(issuer, "issuerCik")))
    if issuer_cik != reference.issuer_cik:
        raise SecProviderError("SEC_INSIDER_ISSUER_MISMATCH")
    issuer_name = _text(_child(issuer, "issuerName"))
    symbol = _text(_child(issuer, "issuerTradingSymbol"))
    if not issuer_name:
        raise SecProviderError("SEC_INSIDER_XML_MISSING_ISSUER")
    report_text = _text(_child(root, "periodOfReport"))
    report_date = (
        None
        if report_text is None
        else _parse_iso_date(report_text, "SEC_INSIDER_XML_BAD_REPORT_DATE")
    )

    owners: list[SecInsiderOwner] = []
    owner_relations: list[Element | None] = []
    for owner in _children(root, "reportingOwner"):
        owner_id = _child(owner, "reportingOwnerId")
        relation = _child(owner, "reportingOwnerRelationship")
        if owner_id is None:
            raise SecProviderError("SEC_INSIDER_XML_BAD_OWNER")
        owner_cik = canonical_cik(_text(_child(owner_id, "rptOwnerCik")))
        owner_name = _text(_child(owner_id, "rptOwnerName"))
        if not owner_name:
            raise SecProviderError("SEC_INSIDER_XML_BAD_OWNER")
        owners.append(
            SecInsiderOwner(
                cik=owner_cik,
                name=owner_name,
                is_director=_parse_bool(_text(_child(relation, "isDirector")))
                if relation is not None
                else None,
                is_officer=_parse_bool(_text(_child(relation, "isOfficer")))
                if relation is not None
                else None,
                is_ten_percent_owner=_parse_bool(_text(_child(relation, "isTenPercentOwner")))
                if relation is not None
                else None,
                officer_title=_text(_child(relation, "officerTitle"))
                if relation is not None
                else None,
            )
        )
        owner_relations.append(relation)
    if not owners:
        raise SecProviderError("SEC_INSIDER_XML_MISSING_OWNER")

    footnotes: dict[str, str] = {}
    for footnote in (element for element in root.iter() if _local_name(element.tag) == "footnote"):
        footnote_id = footnote.attrib.get("id")
        note_text = _text(footnote)
        if footnote_id and note_text:
            if footnote_id in footnotes and footnotes[footnote_id] != note_text:
                raise SecProviderError("SEC_INSIDER_XML_DUPLICATE_FOOTNOTE")
            footnotes[footnote_id] = note_text

    output: list[SecInsiderTransaction] = []
    table_specs: tuple[tuple[str, str, Literal["non_derivative", "derivative"]], ...] = (
        ("nonDerivativeTable", "nonDerivativeTransaction", "non_derivative"),
        ("derivativeTable", "derivativeTransaction", "derivative"),
    )
    for table_name, transaction_name, security_type in table_specs:
        table = _child(root, table_name)
        if table is None:
            continue
        for transaction_index, transaction in enumerate(
            _children(table, transaction_name), start=1
        ):
            title = _value(transaction, "securityTitle")
            date_text = _value(transaction, "transactionDate")
            coding = _child(transaction, "transactionCoding")
            amounts = _child(transaction, "transactionAmounts")
            if title is None or coding is None or amounts is None:
                raise SecProviderError("SEC_INSIDER_XML_BAD_TRANSACTION")
            shares = _parse_decimal(_value(amounts, "transactionShares"))
            total_value = _parse_decimal(_value(amounts, "transactionTotalValue"))
            if shares is None and total_value is None:
                raise SecProviderError("SEC_INSIDER_XML_MISSING_NUMERIC_FIELD")
            # The normalized contract's shares column represents units in the table's own
            # security; total-value-only derivative filings retain that value separately.
            code_text = _value(coding, "transactionCode")
            acquired = _parse_acquired_disposed(_value(amounts, "transactionAcquiredDisposedCode"))
            post_amounts = _child(transaction, "postTransactionAmounts")
            ownership = _child(transaction, "ownershipNature")
            direct_indirect = _parse_direct_indirect(_value(ownership, "directOrIndirectOwnership"))
            underlying = _child(transaction, "underlyingSecurity")
            transaction_price = _parse_decimal(_value(amounts, "transactionPricePerShare"))
            shares_owned = (
                _parse_decimal(_value(post_amounts, "sharesOwnedFollowingTransaction"))
                if post_amounts is not None
                else None
            )
            owner_relation = next(
                (relation for relation in owner_relations if relation is not None), None
            )
            line_number = _parse_optional_int(
                _value(transaction, "reportLineNumber"),
                "SEC_INSIDER_XML_BAD_REPORT_LINE_NUMBER",
            )
            footnote_ids = {
                element.attrib.get("id", "").strip()
                for element in transaction.iter()
                if _local_name(element.tag) == "footnoteId" and element.attrib.get("id", "").strip()
            }
            note_texts = [footnotes[key] for key in sorted(footnote_ids) if key in footnotes]
            if len(footnote_ids) != len(note_texts):
                raise SecProviderError("SEC_INSIDER_XML_MISSING_FOOTNOTE")
            exercise_date = _parse_optional_date(
                _value(transaction, "exerciseDate"), "SEC_INSIDER_XML_BAD_EXERCISE_DATE"
            )
            expiration_date = _parse_optional_date(
                _value(transaction, "expirationDate"), "SEC_INSIDER_XML_BAD_EXPIRATION_DATE"
            )
            deemed_execution_date = _parse_optional_date(
                _value(transaction, "deemedExecutionDate"),
                "SEC_INSIDER_XML_BAD_DEEMED_EXECUTION_DATE",
            )
            underlying_shares = (
                _parse_decimal(_value(underlying, "underlyingSecurityShares"))
                if underlying is not None
                else None
            )
            underlying_value = (
                _parse_decimal(_value(underlying, "underlyingSecurityValue"))
                if underlying is not None
                else None
            )
            value_owned = _parse_decimal(_value(transaction, "valueOwned"))
            output.append(
                SecInsiderTransaction(
                    issuer_cik=issuer_cik,
                    issuer_name=issuer_name,
                    company_name=issuer_name,
                    symbol=symbol.upper() if symbol else None,
                    company_cik=issuer_cik,
                    owner_cik=owners[0].cik if owners else None,
                    owner_name=owners[0].name if owners else None,
                    owner_title=(
                        _text(_child(owner_relation, "officerTitle"))
                        if owner_relation is not None
                        else None
                    ),
                    ownership_type=direct_indirect,
                    transaction_type=code_text,
                    acquisition_or_disposition=acquired,
                    securities_owned=_public_float(shares_owned),
                    securities_transacted=_public_float(shares),
                    transaction_price=_public_float(transaction_price),
                    filing_url=(
                        "https://www.sec.gov/Archives/edgar/data/"
                        f"{int(reference.filing_cik)}/"
                        f"{reference.accession_number.replace('-', '')}/"
                        f"{reference.accession_number}-index.htm"
                    ),
                    owners=tuple(owners),
                    filing_date=reference.filing_date,
                    report_date=report_date,
                    form=reference.form,
                    accession_number=reference.accession_number,
                    security_type=security_type,
                    transaction_index=transaction_index,
                    report_line_number=line_number,
                    security_title=title,
                    transaction_date=_parse_iso_date(
                        date_text, "SEC_INSIDER_XML_BAD_TRANSACTION_DATE"
                    ),
                    transaction_code=code_text,
                    shares=shares,
                    transaction_total_value=total_value,
                    price_per_share=transaction_price,
                    conversion_or_exercise_price=_parse_decimal(
                        _value(transaction, "conversionOrExercisePrice")
                    ),
                    underlying_security_title=(
                        _value(underlying, "underlyingSecurityTitle")
                        if underlying is not None
                        else None
                    ),
                    acquired_disposed=acquired,
                    shares_owned_after=shares_owned,
                    direct_or_indirect=direct_indirect,
                    other=(
                        _parse_bool(_text(_child(owner_relation, "isOther")))
                        if owner_relation is not None
                        else None
                    ),
                    other_text=(
                        _text(_child(owner_relation, "otherText"))
                        if owner_relation is not None
                        else None
                    ),
                    transaction_timeliness=_value(coding, "transactionTimeliness"),
                    nature_of_ownership=_value(ownership, "natureOfOwnership"),
                    exercise_date=exercise_date,
                    expiration_date=expiration_date,
                    deemed_execution_date=deemed_execution_date,
                    underlying_security_shares=underlying_shares,
                    underlying_security_value=underlying_value,
                    conversion_exercise_price=_parse_decimal(
                        _value(transaction, "conversionOrExercisePrice")
                    ),
                    transaction_value=total_value,
                    value_owned=value_owned,
                    footnote="; ".join(note_texts) if note_texts else None,
                    director=owners[0].is_director if owners else None,
                    officer=owners[0].is_officer if owners else None,
                    ten_percent_owner=owners[0].is_ten_percent_owner if owners else None,
                )
            )
            if len(output) > MAX_TRANSACTION_ROWS:
                raise SecProviderError("SEC_INSIDER_TOO_MANY_TRANSACTIONS")
    return tuple(output)


class InsiderTradingFetcher(Fetcher[InsiderTradingQuery, tuple[SecForm4Raw, ...]]):
    """Search issuer Form 4 filings and fetch each bounded primary ownership XML document."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "InsiderTrading"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="sec_insider_trading",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> InsiderTradingQuery:
        """Validate issuer and filing-window filters before source I/O."""
        if set(kwargs) - {
            "symbol",
            "start_date",
            "end_date",
            "limit",
            "ownership_type",
            "sort_by",
            "use_cache",
            "market",
            "source",
        }:
            raise ValueError("unsupported query parameters")
        if kwargs.get("sort_by") == "updated_on":
            raise ValueError(
                "sort_by='updated_on' is unsupported: SEC filing data has "
                "no verified update timestamp"
            )
        return InsiderTradingQuery.model_validate(kwargs)

    def extract_data(
        self, params: InsiderTradingQuery, ctx: FetchContext
    ) -> tuple[SecForm4Raw, ...]:
        """Search a bounded feed window and fetch each selected filing index/XML pair."""
        # Worst case: ticker directory + three feed pages + three index/XML pairs.
        ensure_request_capacity(ctx, 1 + MAX_FEED_PAGES + MAX_FORM4_FILINGS * 2)
        identities = company_ticker_rows(ctx)
        identity = resolve_ticker(identities, params.symbol)
        references: list[SecForm4Reference] = []
        feed_complete = False
        for page_number in range(MAX_FEED_PAGES):
            query = urlencode(
                {
                    "action": "getcompany",
                    "CIK": identity["cik"],
                    "type": "4",
                    "dateb": params.end_date.strftime("%Y%m%d"),
                    "owner": "include",
                    "start": page_number * MAX_FEED_PAGE_SIZE,
                    "count": MAX_FEED_PAGE_SIZE,
                    "output": "atom",
                }
            )
            feed = fetch_bytes(
                ctx,
                f"{SEC_BROWSE_EDGAR_URL}?{query}",
                max_bytes=MAX_FEED_RESPONSE_BYTES,
            )
            page_refs = parse_form4_feed_page(
                feed,
                issuer_cik=identity["cik"],
                start_date=params.start_date,
                end_date=params.end_date,
            )
            references.extend(page_refs)
            # The Atom page carries all rows (including out-of-window rows) so use feed dates
            # to determine if the requested start boundary is covered before declaring empty.
            page_dates = _feed_dates(feed)
            if len(page_dates) < MAX_FEED_PAGE_SIZE or (
                page_dates and min(page_dates) < params.start_date
            ):
                feed_complete = True
                break
            if len(references) > MAX_FORM4_FILINGS:
                raise SecProviderError("SEC_INSIDER_TOO_MANY_FILINGS")
        if not feed_complete:
            raise SecProviderError("SEC_INSIDER_WINDOW_INCOMPLETE")
        unique: dict[str, SecForm4Reference] = {}
        for reference in references:
            unique.setdefault(reference.accession_number, reference)
        selected = tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.filing_date, item.accession_number),
                reverse=True,
            )
        )
        if len(selected) > MAX_FORM4_FILINGS:
            raise SecProviderError("SEC_INSIDER_TOO_MANY_FILINGS")

        filings: list[SecForm4Raw] = []
        for reference in selected:
            index_url, _ = _filing_urls(reference)
            index_document = fetch_json(ctx, index_url)
            document_url = resolve_primary_xml_url(reference, index_document)
            body = fetch_bytes(ctx, document_url, max_bytes=MAX_XML_RESPONSE_BYTES)
            filings.append(SecForm4Raw(reference=reference, body=body))
        return tuple(filings)

    def transform_data(
        self, raw: tuple[SecForm4Raw, ...], params: InsiderTradingQuery
    ) -> FetchResult:
        """Normalize Form 4 filing payloads into typed ownership transactions."""
        rows: list[SecInsiderTransaction] = []
        for filing in raw:
            rows.extend(parse_form4_xml(filing.body, filing.reference))
        if params.ownership_type is not None:
            rows = [row for row in rows if row.ownership_type == params.ownership_type]
        rows.sort(
            key=lambda row: (
                row.filing_date,
                row.accession_number,
                row.security_type,
                row.transaction_index,
            ),
            reverse=True,
        )
        limit = params.limit or 100
        page = rows[:limit]
        return tuple(page)


def _feed_dates(body: bytes) -> tuple[date, ...]:
    """Read every filing date on a feed page so pagination completeness is measurable."""
    if len(body) > MAX_FEED_RESPONSE_BYTES:
        raise SecProviderError("SEC_INSIDER_FEED_TOO_LARGE")
    try:
        root = SafeET.fromstring(body)
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        raise SecProviderError("SEC_INSIDER_FEED_BAD_XML") from None
    dates: list[date] = []
    entries = [element for element in root.iter() if _local_name(element.tag) == "entry"]
    for entry in entries:
        candidate = None
        for element in entry.iter():
            if _local_name(element.tag) in {"filing-date", "filingDate", "updated", "published"}:
                text = _text(element)
                if text is not None:
                    candidate = text[:10]
                    break
        dates.append(_parse_iso_date(candidate, "SEC_INSIDER_FEED_BAD_DATE"))
    return tuple(dates)


__all__ = [
    "InsiderTradingFetcher",
    "InsiderTradingQuery",
    "SecForm4Reference",
    "parse_form4_feed_page",
    "parse_form4_xml",
    "resolve_primary_xml_url",
]
