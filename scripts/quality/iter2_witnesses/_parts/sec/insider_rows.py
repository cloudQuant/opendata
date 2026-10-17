"""Wave 21 shard of the ``sec`` offline witness: the EDGAR Form 4 ownership row.

This file publishes ``MODELS`` for the one sec inventory row no witness covered,
``OBB2-sec-InsiderTrading``, and ``iter2_claims_fixtures`` merges it into
``scripts/quality/iter2_witnesses/sec.py``'s ``CONFIG["models"]`` (a duplicated key fails closed).

Seam (never a socket, never ``OPENDATA_ALLOW_LIVE_E2E``): the bound fetcher is
``InsiderTradingFetcher`` (``models/iter2_insider.py:598``). It declares ``http_transport = None``
and hands that attribute to three of its four legs, so a witness that only installed a transport
on the class would leave the Atom feed leg pointed at the real governed client: the feed fetch
(``iter2_insider.py:665-669``) calls ``fetch_bytes`` with no ``transport`` argument at all.
Declaring ``client_seam`` on ``models/_common.py``'s ``get_shared_http_client`` is what puts all
four legs -- ticker directory, feed page, filing index, primary XML -- behind one fake, and the
runner's process-wide socket block makes the claim enforceable rather than intended.

Fan-out is why this row stayed last. ``extract_data`` preflights
``ensure_request_capacity(ctx, 1 + MAX_FEED_PAGES + MAX_FORM4_FILINGS * 2)``
(``iter2_insider.py:647``) before its first request, so a carrier has to answer 1 + 3 + 20 = 24
legs or the fetch refuses with ``SEC_REQUEST_BUDGET_EXCEEDED`` on the grant the runner derives
from the carrier itself (``iter2_claims_probe_runner._carrier_sends``). The 24 here are not
padding: the window is built so the fetcher *consumes* every one of them, measured as
``seam_requests=24`` with ``rows=11`` on the direct and service phases. Two full
``MAX_FEED_PAGE_SIZE`` pages do not yet reach the start boundary, the short third page ends the
walk (``iter2_insider.py:680-684``), and each of the ten selected filings then costs its own
index/XML pair.

Page order, not filing order: the only leg whose answer depends on request order is the feed,
whose ``sequence`` is consumed once per page as the fetcher walks ``start=0, 100, 200``. The
twenty archive legs each carry their own ``url_contains`` needle, built the way the source builds
the request (``_filing_urls`` plus the name ``resolve_primary_xml_url`` reads off the index), so a
filing's ``index.json`` can never answer another filing's leg and this carrier does not have to
guess the selection order. Each Form 4 body repeats its own accession inside ``securityTitle``, in
both ownership tables, which is the face that pairs a row's ``accession_number`` with the document
that produced it (arm ``E_pairing_census`` of
``docs/evidence/C126/sec-insider-witness-20261017/probes/carrier_falsification.py``).

Feed shape: every ``<entry>`` on a page carries a ``filing-date`` because ``_feed_dates``
(``iter2_insider.py:736-755``) reads all of them -- including the rows whose ``category term`` is
not a Form 4 -- and it is their minimum that decides whether the window is covered. Out-of-window
``term="4"`` rows therefore cannot pad a page: one date before ``start_date`` ends the walk on
page one and leaves eleven of the 24 declared legs unanswered. The padding here is other form
types, which ``parse_form4_feed_page`` skips before it reads their link.

Refusal: ``SEC_INSIDER_PRIMARY_XML_AMBIGUOUS``. ``_parse_index_xml_name``
(``iter2_insider.py:316-338``) counts the index rows whose name ends ``.xml`` and whose type is the
filing's form, and requires exactly one. The tamper appends a second such row to the newest
filing's index document, built from ``_index_items`` -- the same function that builds the
untampered three -- so the counterfact states a shape the source can meet rather than a string
copied off a run. Two rows claiming to be the Form 4 document leaves the ownership document with
no unique address. The declaration carries no ``expected_rejected``/``missing``/``invalid`` face:
``SecProviderError`` refuses a whole published filing before any contract column is addressed,
which is code-only by the same reading as the other twenty-three sec rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

from opendata.data.providers.sec.models.iter2_insider import (
    MAX_FEED_PAGE_SIZE,
    MAX_FEED_PAGES,
    MAX_FORM4_FILINGS,
)

_COMMON_MODULE: Final[str] = "opendata.data.providers.sec.models._common"
_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": [_COMMON_MODULE],
    "attr": "get_shared_http_client",
}
_SOURCE_REFUSAL_CLASS: Final[str] = "SecProviderError"
_INSIDER_FILES: Final[list[str]] = [
    _COMMON_MODULE.replace(".", "/") + ".py",
    "opendata/data/providers/sec/models/iter2_insider.py",
]
_INSIDER_CONTRACT: Final[list[str]] = [
    "opendata/data/models/sec_iter2.py",
    "opendata/data/domains.yaml",
]

_ISSUER_CIK: Final[str] = "0000320193"
_ISSUER_CIK_INT: Final[int] = 320193
_ISSUER_NAME: Final[str] = "Apple Inc."
_SYMBOL: Final[str] = "AAPL"
_ARCHIVE_ROOT: Final[str] = "https://www.sec.gov/Archives/edgar/data"
_TICKER_NEEDLE: Final[str] = "files/company_tickers.json"
_FEED_NEEDLE: Final[str] = "cgi-bin/browse-edgar"

_WINDOW_START: Final[date] = date(2026, 4, 1)
_WINDOW_END: Final[date] = date(2026, 4, 30)
_FORM4_DAY_STRIDE: Final[int] = 3
_TAIL_PAGE_ROWS: Final[int] = 40
_FILLER_FORMS: Final[tuple[str, ...]] = ("10-Q", "8-K", "S-8", "424B2", "DEF 14A")


def _iso(day_offset: int) -> str:
    """One window date, counted from the start boundary the query passes."""
    value = _WINDOW_START + timedelta(days=day_offset)
    if value > _WINDOW_END:
        raise AssertionError(f"fixture date {value} leaves the declared window")
    return value.isoformat()


def _filings() -> tuple[dict[str, Any], ...]:
    """The ``MAX_FORM4_FILINGS`` in-window filings, newest first, as one dict each."""
    filings: list[dict[str, Any]] = []
    for index in range(MAX_FORM4_FILINGS):
        filing_offset = (_WINDOW_END - _WINDOW_START).days - _FORM4_DAY_STRIDE * index
        derivative = index == _DERIVATIVE_FILING_INDEX
        filings.append(
            {
                "index": index,
                "accession": f"{_ISSUER_CIK}-26-{101 + index:06d}",
                "form": "4",
                "filing_date": _iso(filing_offset),
                "report_date": _iso(filing_offset - 1),
                "transaction_date": _iso(filing_offset - 2),
                "owner_cik": f"{1234567 + index:010d}",
                "owner_name": f"Example Officer {index + 1:02d}",
                "officer_title": "Chief Operating Officer" if index % 2 else "General Counsel",
                "is_director": "1" if index % 5 == 0 else "0",
                "is_officer": "0" if index % 7 == 3 else "1",
                "acquired_disposed": "A" if index % 2 else "D",
                "direct_or_indirect": "I" if index % 4 == 0 else "D",
                "shares": 1000 + 250 * index,
                "price": f"{170 + index}.50",
                "shares_owned_after": 50_000 + 1000 * index,
                "primary_document": f"form4-{index + 1:02d}.xml",
                "derivative": derivative,
            }
        )
    return tuple(filings)


#: One filing also publishes an option exercise, so ``security_type`` carries both literals.
_DERIVATIVE_FILING_INDEX: Final[int] = 3


def _seam_json(url_contains: str, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "application/json",
        "body": document,
    }


def _seam_text(url_contains: str, lines: list[str]) -> dict[str, Any]:
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "text/xml",
        "body": lines,
    }


def _ticker_directory_document() -> dict[str, Any]:
    """The CIK map form of ``company_tickers.json``, with a second issuer to resolve against."""
    return {
        "0": {"cik_str": _ISSUER_CIK_INT, "ticker": _SYMBOL, "title": _ISSUER_NAME},
        "1": {
            "cik_str": 1067983,
            "ticker": "BRK-A",
            "title": "Berkshire Hathaway Inc.",
        },
    }


def _feed_entry(form: str, filed: str, accession: str) -> str:
    return (
        "<entry>"
        f'<category term="{form}"/>'
        f"<filing-date>{filed}</filing-date>"
        f'<link href="{_ARCHIVE_ROOT}/{_ISSUER_CIK_INT}/{accession}-index.htm"/>'
        "</entry>"
    )


def _atom_page(in_window: tuple[dict[str, Any], ...], *, page: int) -> list[str]:
    """One Atom company-search page of exactly ``MAX_FEED_PAGE_SIZE`` rows, or a short tail."""
    rows = MAX_FEED_PAGE_SIZE if page < MAX_FEED_PAGES - 1 else _TAIL_PAGE_ROWS
    entries: list[str] = [
        _feed_entry(filing["form"], filing["filing_date"], filing["accession"])
        for filing in in_window
    ]
    for filler in range(rows - len(entries)):
        accession = f"{_ISSUER_CIK}-26-{900 + page * 100 + filler:06d}"
        entries.append(
            _feed_entry(_FILLER_FORMS[filler % len(_FILLER_FORMS)], _iso(filler % 29), accession)
        )
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:edgar="https://www.sec.gov/Archives/edgar">',
        f"<title>EDGAR Filings Feed: Form 4 for CIK {_ISSUER_CIK} (page {page + 1})</title>",
        *entries,
        "</feed>",
    ]


def _feed_pages(filings: tuple[dict[str, Any], ...]) -> list[list[str]]:
    """Split the ten filings over the walk's three pages, filling the first two to the cap."""
    per_page = -(-len(filings) // (MAX_FEED_PAGES - 1))
    pages: list[list[str]] = []
    for page in range(MAX_FEED_PAGES):
        chunk = filings[page * per_page : (page + 1) * per_page]
        pages.append(_atom_page(tuple(chunk), page=page))
    return pages


def _archive_prefix(filing: dict[str, Any]) -> str:
    return f"{_ARCHIVE_ROOT}/{_ISSUER_CIK_INT}/{filing['accession'].replace('-', '')}"


def _index_items(filing: dict[str, Any]) -> list[dict[str, str]]:
    """The filing index directory rows the source reads to address the primary document."""
    return [
        {
            "name": f"{filing['accession']}-index.htm",
            "type": "INDEX",
            "format": "text-html",
        },
        {
            "name": filing["primary_document"],
            "type": filing["form"],
            "format": "text/xml",
        },
        {"name": "full.html", "type": "TEXT", "format": "text/html"},
    ]


def _index_document(filing: dict[str, Any]) -> dict[str, Any]:
    return {
        "accessionNumber": filing["accession"],
        "cik": str(_ISSUER_CIK_INT),
        "form": filing["form"],
        "filingDate": filing["filing_date"],
        "name": _ISSUER_NAME,
        "directory": {
            "name": filing["accession"].replace("-", ""),
            "item": _index_items(filing),
        },
    }


def _form4_xml(filing: dict[str, Any]) -> list[str]:
    """One published Form 4 ownership document, carrying its own accession in the security title."""
    title = f"Common Stock, accession {filing['accession']}"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ownershipDocument>",
        f"<documentType>{filing['form']}</documentType>",
        f"<periodOfReport>{filing['report_date']}</periodOfReport>",
        "<issuer>",
        f"<issuerCik>{_ISSUER_CIK}</issuerCik>",
        f"<issuerName>{_ISSUER_NAME}</issuerName>",
        f"<issuerTradingSymbol>{_SYMBOL}</issuerTradingSymbol>",
        "</issuer>",
        "<reportingOwner>",
        "<reportingOwnerId>",
        f"<rptOwnerCik>{filing['owner_cik']}</rptOwnerCik>",
        f"<rptOwnerName>{filing['owner_name']}</rptOwnerName>",
        "</reportingOwnerId>",
        "<reportingOwnerRelationship>",
        f"<isDirector>{filing['is_director']}</isDirector>",
        f"<isOfficer>{filing['is_officer']}</isOfficer>",
        "<isTenPercentOwner>0</isTenPercentOwner>",
        f"<officerTitle>{filing['officer_title']}</officerTitle>",
        *(
            ["<isOther>1</isOther>", "<otherText>Trust account</otherText>"]
            if filing["direct_or_indirect"] == "I"
            else []
        ),
        "</reportingOwnerRelationship>",
        "</reportingOwner>",
        "<nonDerivativeTable>",
        "<nonDerivativeTransaction>",
        f"<securityTitle><value>{title}</value></securityTitle>",
        f"<transactionDate><value>{filing['transaction_date']}</value></transactionDate>",
        "<transactionCoding>",
        f"<transactionCode>{'S' if filing['index'] % 3 else 'A'}</transactionCode>",
        "<equitySwapInvolved>0</equitySwapInvolved>",
        "</transactionCoding>",
        "<transactionAmounts>",
        f"<transactionShares><value>{filing['shares']}</value></transactionShares>",
        f"<transactionPricePerShare><value>{filing['price']}</value></transactionPricePerShare>",
        "<transactionAcquiredDisposedCode>"
        f"<value>{filing['acquired_disposed']}</value></transactionAcquiredDisposedCode>",
        "</transactionAmounts>",
        "<postTransactionAmounts><sharesOwnedFollowingTransaction>"
        f"<value>{filing['shares_owned_after']}</value></sharesOwnedFollowingTransaction>"
        "</postTransactionAmounts>",
        "<ownershipNature>",
        f"<directOrIndirectOwnership><value>{filing['direct_or_indirect']}</value>"
        "</directOrIndirectOwnership>",
        *(
            ["<natureOfOwnership><value>By trust account</value></natureOfOwnership>"]
            if filing["direct_or_indirect"] == "I"
            else []
        ),
        "</ownershipNature>",
        "</nonDerivativeTransaction>",
        "</nonDerivativeTable>",
    ]
    if filing["derivative"]:
        lines.extend(_derivative_table(filing))
    lines.append("</ownershipDocument>")
    return lines


def _derivative_table(filing: dict[str, Any]) -> list[str]:
    """The option exercise half of one filing, so both ``security_type`` literals are produced."""
    return [
        "<derivativeTable>",
        "<derivativeTransaction>",
        "<securityTitle><value>"
        f"Non-Qualified Stock Option, accession {filing['accession']}</value>"
        "</securityTitle>",
        "<conversionOrExercisePrice>"
        f"<value>{float(filing['price']) - 10:.2f}</value></conversionOrExercisePrice>",
        f"<exerciseDate><value>{filing['transaction_date']}</value></exerciseDate>",
        "<expirationDate><value>2031-03-31</value></expirationDate>",
        "<underlyingSecurity>",
        "<underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle>",
        f"<underlyingSecurityShares><value>{filing['shares']}</value></underlyingSecurityShares>",
        "</underlyingSecurity>",
        "<transactionDate><value>"
        f"{(date.fromisoformat(filing['transaction_date']) + timedelta(days=1)).isoformat()}"
        "</value></transactionDate>",
        "<transactionCoding><transactionCode>M</transactionCode></transactionCoding>",
        "<transactionAmounts>",
        f"<transactionShares><value>{filing['shares']}</value></transactionShares>",
        "<transactionPricePerShare><value>0</value></transactionPricePerShare>",
        "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>",
        "</transactionAmounts>",
        "<postTransactionAmounts><sharesOwnedFollowingTransaction>"
        f"<value>{filing['shares_owned_after'] + filing['shares']}</value>"
        "</sharesOwnedFollowingTransaction></postTransactionAmounts>",
        "<ownershipNature>",
        "<directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>",
        "</ownershipNature>",
        "</derivativeTransaction>",
        "</derivativeTable>",
    ]


def _insider_document() -> dict[str, Any]:
    """The 24-leg carrier the preflight grants: directory + three feed pages + ten filing pairs."""
    filings = _filings()
    responses: list[dict[str, Any]] = [
        _seam_json(_TICKER_NEEDLE, _ticker_directory_document()),
        {
            "url_contains": _FEED_NEEDLE,
            "status": 200,
            "content_type": "application/atom+xml",
            "sequence": _feed_pages(filings),
        },
    ]
    for filing in filings:
        prefix = _archive_prefix(filing)
        responses.append(_seam_json(f"{prefix}/index.json", _index_document(filing)))
        responses.append(_seam_text(f"{prefix}/{filing['primary_document']}", _form4_xml(filing)))
    return {"responses": responses}


def _ambiguous_index_tamper() -> dict[str, Any]:
    """Point the newest filing's index at two documents that both claim to be the Form 4 XML."""
    newest = _filings()[0]
    rival = {
        "name": f"form4-{newest['index'] + 1:02d}-amended.xml",
        "type": newest["form"],
        "format": "text/xml",
    }
    return {
        "op": "set",
        # responses[0] is the ticker directory, [1] the feed, [2] the first filing's index.json.
        "path": ["responses", 2, "body", "directory", "item"],
        "value": [*_index_items(newest), rival],
    }


MODELS: Final[dict[str, Any]] = {
    "InsiderTrading": {
        "client_seam": _CLIENT_SEAM,
        "fixture_document": _insider_document,
        "query": {
            "symbol": _SYMBOL,
            "start_date": _WINDOW_START.isoformat(),
            "end_date": _WINDOW_END.isoformat(),
            "limit": 2 * MAX_FORM4_FILINGS,
        },
        "tamper": _ambiguous_index_tamper(),
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "SEC_INSIDER_PRIMARY_XML_AMBIGUOUS",
        "tamper_reason": (
            "the filing index addresses one primary ownership document per filing, and "
            "``_parse_index_xml_name`` counts the ``.xml`` rows typed as the filing's form, so a "
            "directory that publishes a second such row -- the original plus an amended-looking "
            "one -- leaves the Form 4 document without a unique address and the fetch must refuse "
            "instead of returning whichever row the listing happened to put first"
        ),
        "source_files": _INSIDER_FILES,
        "contract_files": _INSIDER_CONTRACT,
    }
}
