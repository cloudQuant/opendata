"""Wave 8 shard: offline witnesses for econdb's three bindable unwitnessed rows.

Preconditions, checked before any fixture was written (both must hold or the row is a GAP):

1. ``row_model`` is declared by the fetcher itself --
   ``opendata/data/providers/econdb/models/available_indicators.py:11`` (the generated
   ``EcondbAvailableIndicatorsFetcher`` publishes its reviewed contract through
   ``build_row_model``), ``opendata/data/providers/econdb/models/iter2.py:681``
   (``EconDBPortVolumeFetcher.row_model``), ``opendata/data/providers/econdb/models/iter2.py:756``
   (``EconDBYieldCurveFetcher.row_model``).
2. each declared class IS the class ``contract_model('<domain>')`` resolves to, printed with the
   brief's one-liner (``python -c "from opendata.data.domains import contract_model; print(...)"``)
   on 2026-10-15, gate python py313 -- no look-alike is declared here::

    econdb_available_indicators -> opendata.data.models.econdb.EconDBAvailableIndicators
    econdb_port_volume          -> opendata.data.models.econdb.EconDBPortVolume
    econdb_yield_curve          -> opendata.data.models.econdb.EconDBYieldCurve

   The other econdb inventory rows printed the same way, and the same check was already true for
   the three green rows (``opendata/data/providers/econdb/models/iter2.py:386,552,568``).

Branch and seam. All three rows refuse through the engine's typed ``ProviderEngineError`` with an
ECONDB code, so no provider-owned error class is declared. What is new here is how the body reaches
the fetcher. The base module left these rows un-witnessed because ``FixedResponseTransport`` answers
*every* request with one canned document: ``PortVolume`` reads three differently shaped documents
(roster / container-traffic / dwell-times, ``iter2.py:692-707``) and ``YieldCurve`` reads one
document per mapped series id (``iter2.py:767-772``), while ``AvailableIndicators`` declares a
``csv`` decoder whose record list is the raw body, which ``HttpResponse(200, document)`` never
supplies. The wave-8 ``client_seam`` answers all three: no fetcher here sets ``http_transport``, so
``EconDBAPIModelFetcher._get`` (``iter2.py:351``) and the engine's ``_production_transport``
(``opendata/data/providers/_engine/http_json.py:862-884``) both fall through to
``http_json._http_get_json``, which imports ``get_shared_http_client`` *inside the call* (``:825``)
and therefore resolves it from ``opendata.data.http_client`` at request time -- exactly the late
resolution the brief requires, with no provider-local edit and no production behaviour touched. The
declared seam module is that owner module; the seam then matches per request by ``url_contains``,
which is what lets one fixture carry three shapes. Sockets stay blocked by the runner.

Refusals are derived from the contract each row claims to enforce, never copied from a capture:
``AvailableIndicators`` breaks the declared ``multiplier`` int column, which the engine attributes
as ``rejected=('multiplier',)`` (``http_json.py:792-796``); ``PortVolume`` and ``YieldCurve`` break
a numeric cell that their own ``_number`` guard refuses as "not a JSON number" (``iter2.py:172-180``)
before any row is built, so those refusals stay code-only with empty attribution faces, which is the
econdb house pattern (``iter2_witnesses/econdb.py:61-132``).

Two of econdb's five blocked inventory rows are deliberately absent and reported as GAPs:
``CountryProfile`` and ``ExportDestinations`` are catalog-unbound -- econdb's descriptor binds six
fetchers and neither identity is among them (``opendata/data/providers/econdb/provider.py:15-55``),
and the package itself records them as undeclared for lack of a documented semantic equivalent
(``opendata/data/providers/econdb/specs.py:192-206``). Witnessing them would mean editing the
frozen ``opendata/data/catalog.py`` / ``domains.yaml`` / ``models/``, so no fixture is authored.
"""

from __future__ import annotations

from typing import Any, Final

#: The shared offline seam: the module that owns ``get_shared_http_client``, which the engine's
#: ``_http_get_json`` re-imports on every call, so patching it here is what the fetch actually reads.
_ECONDB_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.http_client"],
    "attr": "get_shared_http_client",
}

#: The one request path of the roster model, and the port/series ids the fixtures are keyed on.
_PORT_CODE: Final[str] = "US LAX"
_YIELD_SERIES_3M: Final[str] = "NAAM01USM199N"
_YIELD_SERIES_10Y: Final[str] = "NAAM01USM200N"


def _econdb_available_indicators_envelope() -> dict[str, Any]:
    """``main_tickers.csv`` as the published roster actually is: a header plus ticker lines.

    The header carries the declaration's own source keys (``short_ticker`` -> ``symbol``,
    ``entity`` -> ``country``) plus ``iso``, which is not an output column but the key the
    ``not_equals`` row filter is judged on (``opendata/data/providers/econdb/specs.py:120-127``).
    The third line is the obsolete OECD block the filter exists to drop; the two survivors each
    keep a real ``last_date`` so the descending sort is a measured order, not a sentinel artifact.
    """
    return {
        "responses": [
            {
                "url_contains": "static/help/main_tickers.csv",
                "content_type": "text/csv",
                "body": [
                    "iso,short_ticker,entity,transformation,currency,scale,multiplier,source,"
                    "first_date,last_date,last_insert_timestamp",
                    "US,NAAM01USM144N,United States,,USD,3,,FRED,2024-01-01,2025-04-01,"
                    "2025-04-05T00:00:00",
                    "DE,GGDC10DEA183S,Germany,,EUR,0,,EUROSTAT,2024-01-01,2025-03-31,"
                    "2025-04-02T00:00:00",
                    "OECD,NAAM01OECDXXX,OECD,,USD,0,,LEGACY,2024-01-01,2023-12-31,"
                    "2024-01-05T00:00:00",
                ],
            }
        ]
    }


def _port_traffic_document() -> dict[str, Any]:
    """One ``/v1/ports/{locode}/container-traffic/`` timeseries in TEU.

    ``_port_points`` (``iter2.py:612-665``) pins the title to the documented metric units and
    refuses a body that does not publish the three documented series codes.
    """
    return {
        "type": "timeseries",
        "title": "Container volume (TEU)",
        "series": [
            {"code": "Imports", "name": "Imports", "axis": 1},
            {"code": "Exports", "name": "Exports", "axis": 1},
            {"code": "Transshipment", "name": "Transshipment", "axis": 1},
        ],
        "data": [
            {"Date": "2025-01-01", "Imports": 3400.0, "Exports": 1200.0, "Transshipment": 500.5},
            {"Date": "2025-01-02", "Imports": 2600.0, "Exports": 1500.0, "Transshipment": 0.0},
        ],
    }


def _port_dwell_document() -> dict[str, Any]:
    """One ``/v1/ports/{locode}/dwell-times/`` timeseries in days, on the same two dates."""
    return {
        "type": "timeseries",
        "title": "Average dwell time (day)",
        "series": [
            {"code": "Imports", "name": "Imports", "axis": 1},
            {"code": "Exports", "name": "Exports", "axis": 1},
            {"code": "Transshipments", "name": "Transshipments", "axis": 1},
            {"code": "Rail imports", "name": "Rail imports", "axis": 2},
        ],
        "data": [
            {
                "Date": "2025-01-01",
                "Imports": 4.25,
                "Exports": 3.5,
                "Transshipments": 2.0,
                "Rail imports": 1.5,
            },
            {
                "Date": "2025-01-02",
                "Imports": 4.0,
                "Exports": 3.0,
                "Transshipments": 2.5,
                "Rail imports": 1.0,
            },
        ],
    }


def _port_roster_document() -> list[dict[str, Any]]:
    """Two entries of the ``/v1/ports/`` roster; ``_port_metadata`` demands exactly one match."""
    return [
        {"locode": "US LAX", "name": "Los Angeles", "iso2": "US"},
        {"locode": "US NYC", "name": "New York", "iso2": "US"},
    ]


def _econdb_port_volume_envelope() -> dict[str, Any]:
    """The three documents one PortVolume fetch requests, matched by their own route.

    Order is load-bearing: the seam answers with the FIRST entry whose ``url_contains`` matches,
    and ``v1/ports/`` is a substring of all three URLs, so the two metric routes are declared ahead
    of the roster.
    """
    return {
        "responses": [
            {"url_contains": "container-traffic", "body": _port_traffic_document()},
            {"url_contains": "dwell-times", "body": _port_dwell_document()},
            {"url_contains": "v1/ports/", "body": _port_roster_document()},
        ]
    }


def _yield_series_document(ticker: str, description: str, rates: list[float]) -> dict[str, Any]:
    """One v1 series envelope carrying the yield semantics ``transform_data`` gates on."""
    return {
        "ticker": ticker,
        "description": description,
        "geography": "United States",
        "frequency": "Daily",
        "dataset": "DGS",
        "units": "%",
        "additional_metadata": {"source_name": "Fixture Statistical Office"},
        "data": {"dates": ["2025-01-02", "2025-01-03"], "values": rates},
    }


def _econdb_yield_curve_envelope() -> dict[str, Any]:
    """Two mapped maturity series, each served on its own ``/v1/series/{id}/`` URL.

    Both publish the same two observation days, so the curve has a shared date to be read at
    (``iter2.py:793-801`` refuses ``ECONDB_NO_COMMON_OBSERVATION`` when it has none).
    """
    return {
        "responses": [
            {
                "url_contains": _YIELD_SERIES_3M,
                "body": _yield_series_document(
                    _YIELD_SERIES_3M, "US Treasury Bill yield, 3 month", [4.25, 4.3]
                ),
            },
            {
                "url_contains": _YIELD_SERIES_10Y,
                "body": _yield_series_document(
                    _YIELD_SERIES_10Y, "US Treasury Note yield, 10 year", [4.1, 4.15]
                ),
            },
        ]
    }


_ENGINE_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/econdb.py",
    "opendata/data/domains.yaml",
]

MODELS: Final[dict[str, dict[str, Any]]] = {
    "AvailableIndicators": {
        "client_seam": _ECONDB_SEAM,
        "fixture_document": _econdb_available_indicators_envelope,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 2],
            "value": (
                "DE,GGDC10DEA183S,Germany,,EUR,0,hundred,EUROSTAT,2024-01-01,2025-03-31,"
                "2025-04-02T00:00:00"
            ),
            "column": "multiplier",
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "ECONDB_SHAPE_INVALID",
        "expected_rejected": ["multiplier"],
        "expected_missing": [],
        "expected_invalid": [],
        "tamper_reason": (
            "the declaration types 'multiplier' as an int column (specs.py:111) and the reviewed "
            "EconDBAvailableIndicators field is 'int | None', so a roster line that spells the "
            "multiplier as the text 'hundred' is not a castable published value: the engine must "
            "attribute the failure to the multiplier cell rather than null it away or widen it to "
            "a made-up power of ten"
        ),
        "source_files": [
            "opendata/data/providers/econdb/models/available_indicators.py",
            "opendata/data/providers/econdb/specs.py",
        ],
        "contract_files": list(_ENGINE_CONTRACT_FILES),
    },
    "PortVolume": {
        "client_seam": _ECONDB_SEAM,
        "fixture_document": _econdb_port_volume_envelope,
        "query": {"port_code": _PORT_CODE},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", 0, "Imports"],
            "value": True,
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "ECONDB_SHAPE_INVALID",
        "tamper_reason": (
            "the container-traffic route publishes import volume as a TEU number and "
            "EconDBPortVolume.import_teu is a StrictInt, so a boolean on the observation date is "
            "the adapter's own numeric guard refusing (iter2.py:_number rejects a bool-as-quantity "
            "before any row is built) rather than a zero-or-one container count"
        ),
        "source_files": [
            "opendata/data/providers/econdb/models/iter2.py",
        ],
        "contract_files": list(_ENGINE_CONTRACT_FILES),
    },
    "YieldCurve": {
        "client_seam": _ECONDB_SEAM,
        "fixture_document": _econdb_yield_curve_envelope,
        "query": {
            "country": "United States",
            "series_ids": [_YIELD_SERIES_3M, _YIELD_SERIES_10Y],
            "maturities": ["3M", "10Y"],
        },
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", "data", "values", 1],
            "value": True,
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "ECONDB_SHAPE_INVALID",
        "tamper_reason": (
            "EconDBYieldCurve.value is a StrictFloat rate and the mapped 10-year series publishes "
            "a JSON number per observation, so publishing a boolean on the curve's latest shared "
            "date is the series validator's numeric refusal (iter2.py:_number) -- a curve point "
            "cannot be a flag, and the adapter must refuse the whole snapshot"
        ),
        "source_files": [
            "opendata/data/providers/econdb/models/iter2.py",
        ],
        "contract_files": list(_ENGINE_CONTRACT_FILES),
    },
}
