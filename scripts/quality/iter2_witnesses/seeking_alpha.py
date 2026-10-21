"""Offline witness configuration for the seeking_alpha provider models.

The fixture is the captured envelope, shrunk to what a witness can assert rather than a whole day:
three records on page 1 and one on page 2, byte-for-byte the JSON:API resources Seeking Alpha sent
on 2026-10-19 (``docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/sources/``:
``sa-calendar-second-date.json`` and ``sa-calendar-page2-api-form.json``), with the envelope's own
``meta`` narrowed to match the kept rows (``size`` equal to the row count, ``total_count`` 4,
``total_pages`` 2) so the page cursor
is still exercised. Two properties of the real response are load-bearing and are therefore kept:

* page 1 carries a non-null ``links.next`` while ``total_pages`` is 2 -- the adapter's guard refuses
  a declared further page with no cursor, so dropping it would make the fixture a different route.
* ``links.next`` is published as ``/earnings_calendar/tickers?…``, which is *not* the API address;
  the capture proves it resolves to ``HTTP/2 404`` against the site root, which is why the adapter
  builds ``page[number]`` itself and why the seam here serves page 2 from a sequence rather than
  from a second URL needle (the governed client's params never reach the matcher).

The refusal is the adapter's own text guard, ``SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD``. It is
attributed by code rather than by a typed column because several calendar cells are legitimately
null in this envelope and only a non-text value in one of them is a corrupt body; "no value" and
"not a value" are different failures and only the second may land as data.
"""

from __future__ import annotations

from typing import Any, Final

CALENDAR_DAY = "2026-10-20"
_NEXT_LINK = f"/earnings_calendar/tickers?filter[selected_date]={CALENDAR_DAY}&page[number]=2"


def _record(
    identifier: str, slug: str, name: str, exchange: str, sector: int, cap: float
) -> dict[str, Any]:
    """One JSON:API calendar resource in the envelope's own key spelling."""
    return {
        "id": identifier,
        "type": "earning_calendar_ticker",
        "attributes": {
            "exchange": exchange,
            "marketcap": cap,
            "name": name,
            "release_date": f"{CALENDAR_DAY}T00:00:00.000-04:00",
            "release_time": "Post-Market",
            "sector_id": sector,
            "slug": slug,
        },
    }


def _cboe_calendar_envelope() -> dict[str, Any]:
    """Page 1 then page 2 of one calendar day, as one seam carrier sequences them."""
    page_one = [
        _record(
            "554436", "SPCX", "Space Exploration Technologies Corp.", "NASDAQ", 50, 1.9820392e12
        ),
        _record("118621", "FVCB", "FVCBankcorp, Inc.", "NASDAQ", 40, 315598600.0),
        _record("401020", "GE", "General Electric Co.", "NYSE", 20, 2.0e11),
    ]
    page_two = [
        _record("330001", "LOR:CA", "Loree Ltd.", "TSX", 30, 1.5e8),
    ]
    return {
        "responses": [
            {
                "url_contains": "earnings_calendar/tickers",
                "content_type": "application/json",
                "method": "GET",
                "sequence": [
                    {
                        "data": page_one,
                        "meta": {"size": len(page_one), "total_count": 4, "total_pages": 2},
                        "links": {"next": _NEXT_LINK, "self": "/earnings_calendar/tickers"},
                    },
                    {
                        "data": page_two,
                        "meta": {"size": len(page_two), "total_count": 4, "total_pages": 2},
                        "links": {"next": None, "self": "/earnings_calendar/tickers"},
                    },
                ],
            }
        ]
    }


_SA_ID = "1150"
_SA_EFFECTIVE = "2024-06-19T00:06:03.000-04:00"


def _sa_period(index: int) -> dict[str, object]:
    """The envelope's own ``period`` object for one relative period, as the capture spells it."""
    return {
        "periodtypeid": "quarterly",
        "fiscalquarter": index + 1,
        "fiscalyear": 2027,
        "calendarquarter": index + 1,
        "calendaryear": 2026,
        "periodenddate": f"2026-0{index + 4}-30T00:00:00.000-04:00",
        "advancedate": None,
    }


def _sa_series(items: dict[str, list[str]]) -> dict[str, object]:
    """Per-item series in upstream layout: ``<item>.<relative_period>[{dataitemvalue, ...}]``."""
    series: dict[str, object] = {}
    for name, values in items.items():
        series[name] = {
            str(index): [
                {"effectivedate": _SA_EFFECTIVE, "dataitemvalue": value, "period": _sa_period(index)}
            ]
            for index, value in enumerate(values)
        }
    return series


def _sa_estimates_envelope(items: dict[str, list[str]]) -> dict[str, object]:
    """Two seam responses: the search that resolves the site id, then the estimates envelope."""
    return {
        "responses": [
            {
                "url_contains": "/api/v3/searches",
                "body": {
                    "data": [
                        {"id": _SA_ID, "type": "ticker", "attributes": {"symbol": "AAPL"}}
                    ]
                },
            },
            {
                "url_contains": "/api/v3/symbol_data/estimates",
                "body": {"estimates": {_SA_ID: _sa_series(items)}, "revisions": {}},
            },
        ]
    }


def _sa_eps_envelope() -> dict[str, object]:
    """The nine ``eps_*`` items the inventory records, two relative periods each."""
    return _sa_estimates_envelope(
        {
            "eps_gaap_actual": ["1.05", "1.08"],
            "eps_gaap_consensus_low": ["0.88", "0.92"],
            "eps_gaap_consensus_mean": ["0.97", "1.02"],
            "eps_gaap_consensus_high": ["1.12", "1.18"],
            "eps_normalized_actual": ["1.15", "1.19"],
            "eps_normalized_consensus_low": ["0.90", "0.95"],
            "eps_normalized_consensus_mean": ["1.01", "1.06"],
            "eps_normalized_consensus_high": ["1.20", "1.25"],
            "eps_normalized_num_of_estimates": ["8.0", "7.0"],
        }
    )


def _sa_sales_envelope() -> dict[str, object]:
    """The five ``revenue_*`` items the inventory records, two relative periods each."""
    return _sa_estimates_envelope(
        {
            "revenue_actual": ["90753000000.0", "95_340".replace("_", "") + "00000.0"],
            "revenue_consensus_low": ["88100000000.0", "91000000000.0"],
            "revenue_consensus_mean": ["92400000000.0", "97100000000.0"],
            "revenue_consensus_high": ["96800000000.0", "103400000000.0"],
            "revenue_num_of_estimates": ["9.0", "8.0"],
        }
    )



CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.seeking_alpha.registration",
    "credential_env": {},
    "allowed_hosts": ["seekingalpha.com"],
    "models": {
        "CalendarEarnings": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": [
                    "opendata.data.providers.seeking_alpha.models.calendar_earnings_public"
                ],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _cboe_calendar_envelope,
            "query": {"start_date": CALENDAR_DAY, "end_date": CALENDAR_DAY},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "sequence", 0, "data", 0, "attributes", "slug"],
                "value": True,
                "column": "symbol",
            },
            "refusal_class": "SeekingAlphaCalendarEarningsError",
            "refusal_code": "SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD",
            "tamper_reason": (
                "the calendar publishes slug as the ticker text and the contract's 'symbol' is "
                "a required StrictStr; a boolean in that cell is a corrupt body, and because "
                "pydantic would widen True to the literal text 'True', the adapter must refuse the "
                "page with SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD rather than publish a schedule "
                "row for a ticker that the source never named"
            ),
            "source_files": [
                "opendata/data/providers/seeking_alpha/models/calendar_earnings_public.py",
            ],
            "contract_files": [
                "opendata/data/models/seeking_alpha.py",
                "opendata/data/domains.yaml",
            ],
        },
        "ForwardEpsEstimates": {
            "client_seam": {
                "kind": "shared_http_client",
                "attr": "get_shared_http_client",
                "modules": [
                    "opendata.data.providers.seeking_alpha.models.estimates_public",
                ],
            },
            "fixture_document": _sa_eps_envelope,
            "query": {"symbol": "AAPL", "period": "quarter", "relative_periods": 2},
            "tamper": {
                "op": "set",
                "path": [
                    "responses", 1, "body", "estimates", _SA_ID,
                    "eps_gaap_actual", "0", 0, "dataitemvalue",
                ],
                "value": True,
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "SEEKING_ALPHA_ESTIMATES_BAD_NUMERIC",
            "tamper_reason": (
                "dataitemvalue is the one field of the estimate record that becomes a contract "
                "number; the source sends it as a numeric string, and a boolean in that cell is a "
                "corrupt body rather than a missing estimate -- _number refuses it so the row "
                "cannot publish an EPS the source never returned, since pydantic would widen True "
                "to 1.0"
            ),
            "source_files": [
                "opendata/data/providers/seeking_alpha/models/estimates_public.py",
            ],
            "contract_files": [
                "opendata/data/models/seeking_alpha_estimates.py",
                "opendata/data/domains.yaml",
            ],
        },
        "ForwardSalesEstimates": {
            "client_seam": {
                "kind": "shared_http_client",
                "attr": "get_shared_http_client",
                "modules": [
                    "opendata.data.providers.seeking_alpha.models.estimates_public",
                ],
            },
            "fixture_document": _sa_sales_envelope,
            "query": {"symbol": "AAPL", "period": "quarter", "relative_periods": 2},
            "tamper": {
                "op": "set",
                "path": [
                    "responses", 1, "body", "estimates", _SA_ID,
                    "revenue_actual", "0", 0, "dataitemvalue",
                ],
                "value": True,
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "SEEKING_ALPHA_ESTIMATES_BAD_NUMERIC",
            "tamper_reason": (
                "revenue_actual is read through the same numeric guard as the EPS row: the source "
                "publishes it as a numeric string, a boolean there is a corrupt record and must "
                "land as SEEKING_ALPHA_ESTIMATES_BAD_NUMERIC instead of a revenue figure the "
                "estimates envelope never carried"
            ),
            "source_files": [
                "opendata/data/providers/seeking_alpha/models/estimates_public.py",
            ],
            "contract_files": [
                "opendata/data/models/seeking_alpha_estimates.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
