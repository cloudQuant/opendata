"""Offline witness configuration for the ``tradingeconomics`` provider.

The single inventory row runs through the declaration engine:
``TradingEconomicsEconomicCalendarFetcher`` reuses ``fetch_pages``/``normalize_record`` over the
``ECONOMIC_CALENDAR`` ModelSpec, so its refusals are the engine's own attributed failures
(``missing=(column,)``), not an adapter's bare code. The declared detail below comes from the
contract column the tampered source key feeds, never from a captured run.

Witnesses run exclusively through the offline seam (``http_transport``) served by
``FixedResponseTransport``. No leg touches the network and no ``OPENDATA_ALLOW_LIVE_E2E`` path is
enabled.
"""

from __future__ import annotations

from typing import Any, Final


def _calendar_event(index: int) -> dict[str, Any]:
    """One Trading Economics calendar row in the spellings its schema publishes."""
    day = 2 + index
    return {
        "CalendarId": 40_000 + index,
        "Date": f"2026-01-{day:02d}T14:30:00.000Z",
        "Country": "United States",
        "Currency": "USD",
        "Importance": "3",
        "Source": "Non-Farm Payrolls",
        "Event": f"Fixture Employment Change ({day})",
        "Actual": f"{3.1 + index:.1f}",
        "Forecast": f"{3.0 + index:.1f}",
        "TEForecast": f"{2.9 + index:.1f}",
        "Previous": f"{2.8 + index:.1f}",
        "Revised": None,
        "Unit": "K",
        "Category": "Labor Force",
        "DateSpan": 0,
        "Reference": "Jan",
        "ReferenceDate": f"2025-12-{day:02d}",
        "Symbol": None,
        "Ticker": None,
        "URL": f"https://www.tradingeconomics.com/united-states/non-farm-payrolls-{day}",
        "SourceURL": "https://www.bls.gov/fixtures",
        "LastUpdate": f"2026-01-{day:02d}T14:31:00.000Z",
    }


def _tradingeconomics_economic_calendar_document() -> list[dict[str, Any]]:
    """A two-event answer from the documented country route."""
    return [_calendar_event(0), _calendar_event(1)]


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.tradingeconomics.registration",
    "credential_env": {"TRADINGECONOMICS_API_KEY": "fixture-key-never-sent"},
    "allowed_hosts": ["api.tradingeconomics.com"],
    "models": {
        "EconomicCalendar": {
            "fixture_document": _tradingeconomics_economic_calendar_document,
            "query": {"country": "United States"},
            "tamper": {
                "op": "drop",
                "path": [0, "CalendarId"],
                "column": "calendar_id",
            },
            "expected_missing": ["calendar_id"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TRADINGECONOMICS_SHAPE_INVALID",
            "tamper_reason": (
                "publishing an event without its 'CalendarId' leaves the required, non-nullable "
                "'calendar_id' column unresolvable, so the engine must refuse with "
                "TRADINGECONOMICS_SHAPE_INVALID attributing missing=('calendar_id',) rather than "
                "shipping an event the caller cannot cite back"
            ),
            "source_files": [
                "opendata/data/providers/tradingeconomics/models/economic_calendar.py",
                "opendata/data/providers/tradingeconomics/specs.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/tradingeconomics.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
