"""Federal Reserve FOMC official document index metadata."""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import Field, StrictInt

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_clean_documents import (
    FederalReserveFomcDocument,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.federal_reserve._iter2_client import (
    FederalReserveSourceError,
    get_text,
)
from opendata.data.providers.federal_reserve.models._iter2_common import (
    FederalReserveDateQuery,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_FED_HOST = "www.federalreserve.gov"
_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_HISTORICAL_URL = "https://www.federalreserve.gov/monetarypolicy/fomc_historical.htm"
_HISTORICAL_YEAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
_DATE_IN_URL = re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)")


class FomcDocumentsQuery(FederalReserveDateQuery):
    """Index one official FOMC year and optionally narrow by document date."""

    year: StrictInt = Field(ge=1936, le=2027)


def _document_type(title: str, url: str) -> str:
    """Classify official links from their published title and URL text."""
    label = f"{title} {url}".casefold()
    if "beige book" in label or "beigebook" in label:
        return "beige_book"
    if "tealbook" in label or "teal book" in label:
        return "tealbook"
    if "implementation note" in label or "implementationnote" in label:
        return "implementation_note"
    if "press conference" in label or "pressconf" in label:
        return "press_conference"
    if "statement" in label or "fomcstatements" in label:
        return "statement"
    if "minutes" in label:
        return "minutes"
    if "economic projections" in label or "sep" in label:
        return "economic_projections"
    if "transcript" in label:
        return "transcript"
    if "agenda" in label:
        return "agenda"
    if "presentation" in label or "materials" in label:
        return "presentation_materials"
    if "memo" in label or "memorandum" in label:
        return "memo"
    return "other_fomc_document"


class FederalReserveFomcDocumentsFetcher(Fetcher[FomcDocumentsQuery, str]):
    """Enumerate date-addressable FOMC links from the Board's official index pages."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FomcDocuments"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_fomc_documents",
        period="event",
        market="us",
        source="federal_reserve",
        verified=False,
    )
    #: The contract ``domains.yaml`` declares for this domain — the governed query service
    #: serializes rows against it, so :meth:`transform_data` has to build exactly this class.
    #: ``federal_reserve_macro`` publishes a same-named 7-field look-alike; the five fields here are
    #: the published face (``date``/``doc_type``/``doc_format``/``url`` plus an optional
    #: ``release_date``) and the index page states no release date, so it stays unset.
    row_model: ClassVar[type[ContractModel]] = FederalReserveFomcDocument

    def transform_query(self, **kwargs: object) -> FomcDocumentsQuery:
        """Validate year and optional inclusive document-date bounds."""
        return FomcDocumentsQuery.model_validate(kwargs)

    def extract_data(self, params: FomcDocumentsQuery, ctx: FetchContext) -> str:
        """Fetch the year-specific historical index or the current meeting calendar."""
        if type(params) is not FomcDocumentsQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        if params.year <= 2020:
            url = _HISTORICAL_YEAR_URL.format(year=params.year)
        else:
            url = _CALENDAR_URL
        return get_text(url, ctx.timeout)

    def transform_data(
        self, raw: str, params: FomcDocumentsQuery
    ) -> tuple[FederalReserveFomcDocument, ...]:
        """Keep official date-stamped document URLs and reject malformed index links."""
        if not isinstance(raw, str) or not raw:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_FOMC_HTML")
        soup = BeautifulSoup(raw, "html.parser")
        rows: dict[str, FederalReserveFomcDocument] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href")
            if not isinstance(href, str):
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_FOMC_LINK")
            url = urljoin(_CALENDAR_URL, href)
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.hostname != _FED_HOST:
                continue
            if "/monetarypolicy/" not in parsed_url.path:
                continue
            match = _DATE_IN_URL.search(parsed_url.path)
            if match is None:
                continue
            date_text = match.group()
            try:
                document_date = date(int(date_text[:4]), int(date_text[4:6]), int(date_text[6:]))
            except ValueError:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_FOMC_DOCUMENT_DATE") from None
            if document_date.year != params.year:
                continue
            if params.start_date is not None and document_date < params.start_date:
                continue
            if params.end_date is not None and document_date > params.end_date:
                continue
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if not title:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_FOMC_TITLE")
            extension = parsed_url.path.rsplit(".", 1)[-1].casefold()
            document_format = extension if extension in {"pdf", "html", "htm"} else "html"
            if document_format == "htm":
                document_format = "html"
            normalized_url = parsed_url._replace(fragment="").geturl()
            rows[normalized_url] = FederalReserveFomcDocument(
                date=document_date,
                doc_type=_document_type(title, normalized_url),
                doc_format=document_format,
                url=normalized_url,
            )
            if len(rows) > params.max_records:
                raise FederalReserveSourceError("FEDERAL_RESERVE_TOO_MANY_RECORDS")
        return tuple(sorted(rows.values(), key=lambda item: (item.date, item.url)))


__all__ = ["FederalReserveFomcDocumentsFetcher", "FomcDocumentsQuery"]
