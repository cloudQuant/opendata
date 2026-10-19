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
    },
}
