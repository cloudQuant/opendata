"""Offline witness configuration for the ``econdb`` provider models.

econdb's five iter2 rows are hand-written adapters that already speak the repository's own
offline seam (``http_transport`` on ``EconDBAPIModelFetcher``, served by ``FixedResponseTransport``)
and already refuse with the engine's typed ``ProviderEngineError``. Their refusals are code-only:
the adapter validates the whole series envelope before it builds any row, so the typed refusal
carries the stable code and no cell attribution.

Three of econdb's eight census rows deliberately have no entry here, and the reason is measured
rather than editorial: ``AvailableIndicators`` is a ``csv`` decoder whose declaration refuses a
transport that returns no raw body, and ``PortVolume`` and ``YieldCurve`` fan out to two or three
*differently shaped* documents per fetch, which a single canned response cannot answer. The
witness harness pins one body per model, so those rows stay un-witnessed this wave.
"""

from __future__ import annotations

from typing import Any, Final

_SERIES_URL_SYMBOL: Final[str] = "NAAM01USM144N"


def _econdb_series_document(*, description: str) -> dict[str, Any]:
    """One v1 ``/series/{id}/`` envelope in exactly the keys ``models/iter2.py`` reads."""
    return {
        "ticker": _SERIES_URL_SYMBOL,
        "description": description,
        "geography": "United States",
        "frequency": "Quarterly",
        "dataset": "NAAM",
        "units": "USD",
        "additional_metadata": {"source_name": "Fixture Statistical Office"},
        "data": {
            "dates": ["2024-01-01", "2024-04-01", "2025-01-01", "2025-04-01"],
            "values": [100.0, 110.0, 120.0, 130.0],
        },
    }


def _econdb_economic_indicator_document() -> dict[str, Any]:
    return _econdb_series_document(description="Consumer Price Index, United States")


def _econdb_gdp_nominal_document() -> dict[str, Any]:
    return _econdb_series_document(
        description="Gross Domestic Product (GDP), current prices, United States"
    )


def _econdb_gdp_real_document() -> dict[str, Any]:
    return _econdb_series_document(
        description="Gross Domestic Product (GDP), chained volume measures, United States"
    )


def _econdb_export_destinations_document() -> dict[str, Any]:
    """A two-destination answer from the public widget table."""
    return {
        "plots": [
            {
                "data": [
                    {"Country": "Mexico", "Value million USD": 123.4},
                    {"Country": "Canada", "Value million USD": 98.7},
                ],
                "series": [{"code": "Value million USD"}],
                "title": "Exports from United States",
                "footnote": "Public widget fixture.",
            }
        ]
    }


def _econdb_country_profile_document() -> list[dict[str, Any]]:
    """Thirteen context series for the United States, in the live response shape."""
    entries: list[tuple[str, str, list[float]]] = [
        ("GDPUS~TUSD", "Millions", [8_000_000, 8_000_000, 8_000_000, 8_000_000]),
        ("RGDPUS~TPOP", "Percent", [0.55]),
        ("RGDPUS~TOYA", "Percent", [2.2]),
        ("POPUS", "Thousands", [343008]),
        ("CPIUS~TOYA", "Percent", [3.7]),
        ("COREUS~TOYA", "Percent", [2.5]),
        ("RETAUS~TOYA", "Percent", [6.2]),
        ("IPUS~TOYA", "Percent", [2.2]),
        ("POLIRUS", "Percent", [3.75]),
        ("Y10YDUS", "Percent", [4.99]),
        ("GDEBTUS~TUSD", "Millions", [39_000_000]),
        ("CAUS~TPGP", "Percent", [-0.84]),
        ("URATEUS", "Percent", [4.2]),
    ]
    document = []
    for ticker, scale, values in entries:
        document.append(
            {
                "id": ticker,
                "td": {"scale": scale, "units": "Percent" if scale == "Percent" else "USD"},
                "geography": {"name": "United States"},
                "dataarray": [
                    {"date": f"2025-{index + 1:02d}-01", ticker: value}
                    for index, value in enumerate(values)
                ],
            }
        )
    return document


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.econdb.registration",
    "credential_env": {"ECONDB_API_KEY": "fixture-token-never-sent"},
    "allowed_hosts": ["api.econdb.com"],
    "models": {
        "CountryProfile": {
            "fixture_document": _econdb_country_profile_document,
            "query": {"country": "US"},
            "tamper": {"op": "drop", "path": [10], "column": "govt_debt_gdp"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "ECONDB_SHAPE_INVALID",
            "tamper_reason": (
                "dropping the GDEBT context series leaves the government-debt ratio with no "
                "numerator, so the adapter must refuse with ECONDB_SHAPE_INVALID rather than "
                "publish a profile with a silently missing ratio"
            ),
            "source_files": [
                "opendata/data/providers/econdb/models/profiles.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/econdb.py",
                "opendata/data/domains.yaml",
            ],
        },
        "ExportDestinations": {
            "fixture_document": _econdb_export_destinations_document,
            "query": {"country": "US"},
            "tamper": {
                "op": "drop",
                "path": ["plots", 0, "data", 0, "Country"],
                "column": "destination_country",
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "ECONDB_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a destination row without its 'Country' leaves the required "
                "'destination_country' field unresolvable, so the adapter must refuse with "
                "ECONDB_SHAPE_INVALID rather than ship an unlabelled export value"
            ),
            "source_files": [
                "opendata/data/providers/econdb/models/profiles.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/econdb.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EconomicIndicators": {
            "fixture_document": _econdb_economic_indicator_document,
            "query": {"symbol": _SERIES_URL_SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["data", "dates", 0],
                "value": "January 2024",
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "ECONDB_SHAPE_INVALID",
            "tamper_reason": (
                "publishing an observation date the documented ISO calendar contract cannot parse "
                "makes the series envelope refuse before any row is built; the adapter must "
                "surface ECONDB_SHAPE_INVALID as a ProviderEngineError carrying "
                "code/status/rejected/missing/invalid"
            ),
            "source_files": [
                "opendata/data/providers/econdb/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/econdb.py",
                "opendata/data/domains.yaml",
            ],
        },
        "GdpNominal": {
            "fixture_document": _econdb_gdp_nominal_document,
            "query": {"symbol": _SERIES_URL_SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["data", "values", 3],
                "value": True,
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "ECONDB_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a boolean where the v1 series contract publishes a JSON number is the "
                "numeric guard's own refusal: pydantic strict mode and the adapter both reject a "
                "bool-as-quantity, so the adapter must surface ECONDB_SHAPE_INVALID as a "
                "ProviderEngineError with the typed refusal shape"
            ),
            "source_files": [
                "opendata/data/providers/econdb/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/econdb.py",
                "opendata/data/domains.yaml",
            ],
        },
        "GdpReal": {
            "fixture_document": _econdb_gdp_real_document,
            "query": {"symbol": _SERIES_URL_SYMBOL},
            "tamper": {
                "op": "drop",
                "path": ["geography"],
                "column": "country",
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "ECONDB_SHAPE_INVALID",
            "tamper_reason": (
                "removing the required 'geography' metadata makes the reporting economy the "
                "'country' column carries unresolvable, so the adapter must surface "
                "ECONDB_SHAPE_INVALID as a ProviderEngineError rather than publish a GDP row "
                "without the economy it belongs to"
            ),
            "source_files": [
                "opendata/data/providers/econdb/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/econdb.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
