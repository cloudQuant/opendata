"""Offline witness configuration for the ``imf`` provider (wave 10).

Neither imf fetcher owns an engine transport: every iter2 fetcher reads
``get_shared_http_client()`` as a module-level name in
``opendata.data.providers/imf/models/_sdmx.py`` (SDMX structure + CSV observation
legs), ``.../models/_client.py`` (DataMapper JSON legs) or
``.../models/portwatch.py`` (ArcGIS legs), so each entry declares a ``client_seam``
over the module its legs actually import. That is measured, not assumed: the
provider's 11 bindings publish no ``http_transport`` attribute at all, which is why
the engine seam path cannot witness them and why this shard exists.

The carriers are what the shipped decoders parse: JSON documents for the SDMX
structure and catalogue legs (``json.loads`` + ``data``/``dataflows``/
``dataStructures``/``codelists`` shape checks in ``_sdmx``) and ``list[str]`` CSV
lines for the observation legs (``csv.DictReader`` over the joined body, header
required to carry ``TIME_PERIOD`` and ``OBS_VALUE``). PortWatch layers are read by
``_fetch_pages``, which asks one endpoint three times -- count, ObjectId-ordered
page, confirming count -- so those envelopes answer a single URL with the
runner's per-request ``sequence`` carrier, whose entries are bodies (a tamper path
addresses ``["sequence", 1, "features", ...]``, not a nested ``body`` key). Parsing,
dimension attribution and the frozen macro_iter2 contracts are the code that ships;
the runner's socket circuit breaker makes "offline" enforceable rather than asserted.

The refusal face is the provider's own: ``ImfSdmxError`` carries ``code`` and
``status``. The tamper refusals here are raised by ``transform_data`` and by the
``_sdmx`` shape checks without an HTTP status, so the declared ``status`` stays
``None`` and no column-attribution face is claimed (attribution lives in the
tamper_reason prose).
"""

from __future__ import annotations

from typing import Any, Final

_SDMX_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.imf.models._sdmx"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/imf/models/_sdmx.py",
    "opendata/data/providers/imf/models/iter2.py",
]
_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/macro_iter2.py",
    "opendata/data/domains.yaml",
]

# The CPI flow's own DSD code lists, as ``fetch_dataflow_structure`` returns them
# inside ``data.codelists``. ``code_labels`` accepts either the ``names`` mapping or
# a flat ``name``, and rejects duplicate codes, so each list below is one entry per
# code with an English label.
_CPI_STRUCTURE: Final[dict[str, Any]] = {
    "data": {
        "codelists": [
            {
                "id": "CL_COUNTRY",
                "codes": [
                    {"id": "USA", "names": {"en": "United States"}},
                    {"id": "JPN", "names": {"en": "Japan"}},
                ],
            },
            {
                "id": "CL_INDEX_TYPE",
                "codes": [
                    {"id": "CPI", "names": {"en": "Consumer Price Index"}},
                    {"id": "HICP", "names": {"en": "Harmonised Index of Consumer Prices"}},
                ],
            },
            {
                "id": "CL_COICOP_1999",
                "codes": [
                    {"id": "_T", "names": {"en": "All Items"}},
                    {"id": "CP01", "names": {"en": "Food and non-alcoholic beverages"}},
                ],
            },
            {
                "id": "CL_CPI_TYPE_OF_TRANSFORMATION",
                "codes": [
                    {"id": "IX", "names": {"en": "Index"}},
                    {"id": "YOY_PCH_PA_PT", "names": {"en": "Y-on-Y, percentage change"}},
                ],
            },
        ]
    }
}


def _cpi_envelope() -> dict[str, Any]:
    """Two legs: the CPI DSD metadata document, then its annual index CSV."""
    return {
        "responses": [
            {
                "url_contains": "/structure/dataflow/IMF.STA/CPI/5.0.0",
                "status": 200,
                "content_type": "application/json",
                "body": _CPI_STRUCTURE,
            },
            {
                "url_contains": "/data/dataflow/IMF.STA/CPI/5.0.0/USA.CPI._T.IX.A",
                "status": 200,
                "content_type": "text/plain",
                "body": [
                    "COUNTRY,INDEX_TYPE,COICOP_1999,TYPE_OF_TRANSFORMATION,FREQUENCY,"
                    "TIME_PERIOD,OBS_VALUE",
                    "USA,CPI,_T,IX,A,2022,124.3",
                    "USA,CPI,_T,IX,A,2023,131.5",
                    "USA,CPI,_T,IX,A,2024,136.2",
                ],
            },
        ]
    }


# The catalogue leg returns ``data.dataflows``; the structure leg carries the DSD
# dimension list plus the indicator and frequency code lists that
# ``dimension_labels`` resolves through ``CL_INDICATOR``/``CL_FREQ``.
_CATALOG: Final[dict[str, Any]] = {
    "data": {
        "dataflows": [
            {
                "agencyID": "IMF.STA",
                "id": "CPI",
                "version": "5.0.0",
                "name": "Consumer Price Index",
            }
        ]
    }
}
_INDICATOR_STRUCTURE: Final[dict[str, Any]] = {
    "data": {
        "dataStructures": [
            {
                "id": "DSD_CPI",
                "dataStructureComponents": {
                    "dimensionList": {
                        "dimensions": [
                            {
                                "id": "COUNTRY",
                                "position": 1,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).COUNTRY"
                                ),
                            },
                            {
                                "id": "FREQ",
                                "position": 2,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).FREQ"
                                ),
                            },
                            {
                                "id": "INDICATOR",
                                "position": 3,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).INDICATOR"
                                ),
                            },
                        ]
                    }
                },
            }
        ],
        "codelists": [
            {
                "id": "CL_COUNTRY",
                "codes": [{"id": "USA", "names": {"en": "United States"}}],
            },
            {
                "id": "CL_FREQ",
                "codes": [
                    {"id": "A", "names": {"en": "Annual"}},
                    {"id": "M", "names": {"en": "Monthly"}},
                ],
            },
            {
                "id": "CL_INDICATOR",
                "codes": [
                    {
                        "id": "PCPIPCH",
                        "names": {"en": "Inflation, end of period"},
                        "descriptions": {
                            "en": "Consumer price index, percent change, end of period"
                        },
                    },
                    {
                        "id": "PCPIPCHPA",
                        "names": {"en": "Inflation, average consumer prices"},
                        "descriptions": {
                            "en": "Consumer price index, percent change, average period"
                        },
                    },
                ],
            },
        ],
    }
}


def _available_envelope() -> dict[str, Any]:
    """Two legs: the dataflow catalogue, then the requested flow's DSD document."""
    return {
        "responses": [
            {
                "url_contains": "/structure/dataflow/all/all",
                "status": 200,
                "content_type": "application/json",
                "body": _CATALOG,
            },
            {
                "url_contains": "/structure/dataflow/IMF.STA/CPI/5.0.0",
                "status": 200,
                "content_type": "application/json",
                "body": _INDICATOR_STRUCTURE,
            },
        ]
    }


def _dot_envelope() -> dict[str, Any]:
    """Two legs: the IMTS DSD code lists, then the reporter-counterpart balance CSV."""
    return {
        "responses": [
            {
                "url_contains": "/structure/dataflow/IMF.STA/IMTS/1.0.0",
                "status": 200,
                "content_type": "application/json",
                "body": _IMTS_STRUCTURE,
            },
            {
                "url_contains": "/data/dataflow/IMF.STA/IMTS/1.0.0/USA.TBG_USD.JPN.A",
                "status": 200,
                "content_type": "text/plain",
                "body": [
                    "COUNTRY,COUNTERPART_COUNTRY,INDICATOR,FREQUENCY,SCALE,TIME_PERIOD,OBS_VALUE",
                    "USA,JPN,TBG_USD,A,1,2022,-62085.0",
                    "USA,JPN,TBG_USD,A,1,2023,-47711.0",
                ],
            },
        ]
    }


_IMTS_STRUCTURE: Final[dict[str, Any]] = {
    "data": {
        "codelists": [
            {
                "id": "CL_IMTS_COUNTRY",
                "codes": [
                    {"id": "USA", "names": {"en": "United States"}},
                    {"id": "JPN", "names": {"en": "Japan"}},
                ],
            },
            {
                "id": "CL_IMTS_INDICATOR",
                "codes": [
                    {"id": "TBG_USD", "names": {"en": "Trade in Goods, Balance: Total (USD)"}},
                    {"id": "XG_FOB_USD", "names": {"en": "Trade in Goods, Exports: FOB (USD)"}},
                ],
            },
        ]
    }
}

# EconomicIndicators walks three legs: the catalogue (to resolve the flow id inside
# the symbol), the flow's DSD, then the observations keyed by that DSD's dimension
# order. ``PCPI::PCPIPCH`` therefore becomes the key ``USA.PCPIPCH.A``.
_PCPI_CATALOG: Final[dict[str, Any]] = {
    "data": {
        "dataflows": [
            {
                "agencyID": "IMF.STA",
                "id": "PCPI",
                "version": "1.0.0",
                "name": "Price Index - Consumer Price Indexes",
            }
        ]
    }
}
_PCPI_STRUCTURE: Final[dict[str, Any]] = {
    "data": {
        "dataStructures": [
            {
                "id": "DSD_PCPI",
                "dataStructureComponents": {
                    "dimensionList": {
                        "dimensions": [
                            {
                                "id": "COUNTRY",
                                "position": 1,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).COUNTRY"
                                ),
                            },
                            {
                                "id": "INDICATOR",
                                "position": 2,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).INDICATOR"
                                ),
                            },
                            {
                                "id": "FREQ",
                                "position": 3,
                                "conceptIdentity": (
                                    "urn:sdmx:org.sdmx.infomodel.codelist.Codelist"
                                    "=IMF:CL_MASTER(1.0).FREQ"
                                ),
                            },
                        ]
                    }
                },
            }
        ],
        "codelists": [
            {
                "id": "CL_COUNTRY",
                "codes": [{"id": "USA", "names": {"en": "United States"}}],
            },
            {
                "id": "CL_INDICATOR",
                "codes": [
                    {"id": "PCPIPCH", "names": {"en": "Inflation, Consumer Prices (annual %)"}},
                    {"id": "PCPI", "names": {"en": "Consumer Price Index, Period, Index"}},
                ],
            },
            {
                "id": "CL_FREQ",
                "codes": [
                    {"id": "A", "names": {"en": "Annual"}},
                    {"id": "M", "names": {"en": "Monthly"}},
                ],
            },
        ],
    }
}


def _economic_envelope() -> dict[str, Any]:
    return {
        "responses": [
            {
                "url_contains": "/structure/dataflow/all/all",
                "status": 200,
                "content_type": "application/json",
                "body": _PCPI_CATALOG,
            },
            {
                "url_contains": "/structure/dataflow/IMF.STA/PCPI/1.0.0",
                "status": 200,
                "content_type": "application/json",
                "body": _PCPI_STRUCTURE,
            },
            {
                "url_contains": "/data/dataflow/IMF.STA/PCPI/1.0.0/USA.PCPIPCH.A",
                "status": 200,
                "content_type": "text/plain",
                "body": [
                    "COUNTRY,INDICATOR,FREQ,UNIT,TIME_PERIOD,OBS_VALUE",
                    "USA,PCPIPCH,A,IX,2022,8.0",
                    "USA,PCPIPCH,A,IX,2023,4.1",
                    "USA,PCPIPCH,A,IX,2024,2.9",
                ],
            },
        ]
    }


# PortWatch's four layers are read through ``_fetch_pages``, which for one service
# asks the source three times: a count, the ObjectId-ordered page, then a second
# count that must match the first (that is what lets it report a complete table
# rather than a truncated one). The seam's per-response ``sequence`` carrier is
# therefore what a PortWatch row needs -- one body per request, consumed in order.
_PORTWATCH_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.imf.models.portwatch"],
    "attr": "get_shared_http_client",
}
_PORTWATCH_SOURCE_FILES: Final[list[str]] = ["opendata/data/providers/imf/models/portwatch.py"]


def _feature(object_id: int, attributes: dict[str, Any]) -> dict[str, Any]:
    """One ArcGIS feature, with the ObjectId ``_fetch_pages`` orders and dedupes on."""
    return {"attributes": {"ObjectId": object_id, **attributes}}


def _arcgis_envelope(service: str, features: list[dict[str, Any]]) -> dict[str, Any]:
    """Count, page, confirming count for one PortWatch FeatureServer layer."""
    return {
        "responses": [
            {
                "url_contains": f"{service}/FeatureServer/0/query",
                "status": 200,
                "content_type": "application/json",
                "sequence": [
                    {"count": len(features)},
                    {"features": features, "exceededTransferLimit": False},
                    {"count": len(features)},
                ],
            }
        ]
    }


_CHOKEPOINT_METADATA: Final[list[dict[str, Any]]] = [
    _feature(
        11,
        {
            "portid": "SUEZ",
            "portname": "Suez Canal",
            "lat": 29.986,
            "lon": 32.550,
            "vessel_count_container": 100,
            "vessel_count_dry_bulk": 200,
            "vessel_count_general_cargo": 50,
            "vessel_count_RoRo": 10,
            "vessel_count_tanker": 300,
            "vessel_count_total": 660,
            "industry_top1": "Container",
            "industry_top2": "Dry bulk",
            "industry_top3": "Tanker",
        },
    ),
    _feature(
        12,
        {
            "portid": "PANAM",
            "portname": "Panama Canal",
            "lat": 9.200,
            "lon": -79.500,
            "vessel_count_container": 90,
            "vessel_count_dry_bulk": 150,
            "vessel_count_general_cargo": 40,
            "vessel_count_RoRo": 5,
            "vessel_count_tanker": 210,
            "vessel_count_total": 495,
            "industry_top1": "Tanker",
            "industry_top2": None,
            "industry_top3": None,
        },
    ),
]
_CHOKEPOINT_DAILY: Final[list[dict[str, Any]]] = [
    _feature(
        21,
        {
            "date": "2024-05-01",
            "portid": "SUEZ",
            "portname": "Suez Canal",
            "n_container": 10,
            "n_dry_bulk": 20,
            "n_general_cargo": 5,
            "n_roro": 1,
            "n_tanker": 30,
            "n_cargo": 36,
            "n_total": 66,
            "capacity_container": 1000000,
            "capacity_dry_bulk": 2000000,
            "capacity_general_cargo": 500000,
            "capacity_roro": 100000,
            "capacity_tanker": 3000000,
            "capacity_cargo": 3600000,
            "capacity": 6600000,
        },
    ),
    _feature(
        22,
        {
            "date": "2024-05-02",
            "portid": "SUEZ",
            "portname": "Suez Canal",
            "n_container": 8,
            "n_dry_bulk": 18,
            "n_general_cargo": 4,
            "n_roro": 2,
            "n_tanker": 28,
            "n_cargo": 32,
            "n_total": 60,
            "capacity_container": 900000,
            "capacity_dry_bulk": 1900000,
            "capacity_general_cargo": 400000,
            "capacity_roro": 120000,
            "capacity_tanker": 2800000,
            "capacity_cargo": 3220000,
            "capacity": 6020000,
        },
    ),
]
_PORT_INFO: Final[list[dict[str, Any]]] = [
    _feature(
        31,
        {
            "portid": "USLGB",
            "continent": "North America",
            "country": "United States",
            "ISO3": "USA",
            "portname": "Port of Long Beach",
            "fullname": "Port of Long Beach, California",
            "lat": 33.750,
            "lon": -118.200,
            "vessel_count_container": 400,
            "vessel_count_dry_bulk": 80,
            "vessel_count_general_cargo": 150,
            "vessel_count_RoRo": 10,
            "vessel_count_tanker": 160,
            "vessel_count_total": 800,
            "industry_top1": "Container",
            "industry_top2": None,
            "industry_top3": None,
            "share_country_maritime_import": 8.1,
            "share_country_maritime_export": 6.2,
        },
    ),
    _feature(
        32,
        {
            "portid": "USLAX",
            "continent": "North America",
            "country": "United States",
            "ISO3": "USA",
            "portname": "Port of Los Angeles",
            "fullname": "Port of Los Angeles, California",
            "lat": 33.730,
            "lon": -118.270,
            "vessel_count_container": 500,
            "vessel_count_dry_bulk": 100,
            "vessel_count_general_cargo": 200,
            "vessel_count_RoRo": 20,
            "vessel_count_tanker": 180,
            "vessel_count_total": 1000,
            "industry_top1": "Container",
            "industry_top2": "Tanker",
            "industry_top3": "Dry bulk",
            "share_country_maritime_import": 12.5,
            "share_country_maritime_export": 9.4,
        },
    ),
]
_PORT_VOLUME: Final[list[dict[str, Any]]] = [
    _feature(
        41,
        {
            "date": "2024-05-01",
            "portid": "USLAX",
            "portname": "Port of Los Angeles",
            "country": "United States",
            "ISO3": "USA",
            "portcalls_container": 500,
            "portcalls_dry_bulk": 100,
            "portcalls_general_cargo": 200,
            "portcalls_roro": 20,
            "portcalls_tanker": 180,
            "portcalls_cargo": 820,
            "portcalls": 1000,
            "import_container": 9000000,
            "import_dry_bulk": 4000000,
            "import_general_cargo": 1500000,
            "import_roro": 800000,
            "import_tanker": 12000000,
            "import_cargo": 15300000,
            "import": 27300000,
            "export_container": 6000000,
            "export_dry_bulk": 2500000,
            "export_general_cargo": 900000,
            "export_roro": 500000,
            "export_tanker": 7000000,
            "export_cargo": 9900000,
            "export": 16900000,
        },
    ),
    _feature(
        42,
        {
            "date": "2024-05-02",
            "portid": "USLAX",
            "portname": "Port of Los Angeles",
            "country": "United States",
            "ISO3": "USA",
            "portcalls_container": 480,
            "portcalls_dry_bulk": 95,
            "portcalls_general_cargo": 190,
            "portcalls_roro": 18,
            "portcalls_tanker": 170,
            "portcalls_cargo": 783,
            "portcalls": 953,
            "import_container": 8800000,
            "import_dry_bulk": 3900000,
            "import_general_cargo": 1400000,
            "import_roro": 780000,
            "import_tanker": 11700000,
            "import_cargo": 14880000,
            "import": 26580000,
            "export_container": 5800000,
            "export_dry_bulk": 2400000,
            "export_general_cargo": 880000,
            "export_roro": 470000,
            "export_tanker": 6700000,
            "export_cargo": 9550000,
            "export": 16250000,
        },
    ),
]


def _chokepoint_info_envelope() -> dict[str, Any]:
    return _arcgis_envelope("PortWatch_chokepoints_database", _CHOKEPOINT_METADATA)


def _chokepoint_volume_envelope() -> dict[str, Any]:
    """Two layers, six requests: resolve the chokepoint id, then read its daily rows."""
    metadata = _arcgis_envelope("PortWatch_chokepoints_database", _CHOKEPOINT_METADATA)["responses"]
    daily = _arcgis_envelope("Daily_Chokepoints_Data", _CHOKEPOINT_DAILY)["responses"]
    return {"responses": [*metadata, *daily]}


def _port_info_envelope() -> dict[str, Any]:
    return _arcgis_envelope("PortWatch_ports_database", _PORT_INFO)


def _port_volume_envelope() -> dict[str, Any]:
    return _arcgis_envelope("Daily_Ports_Data", _PORT_VOLUME)


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.imf.registration",
    "credential_env": {},  # the IMF SDMX and DataMapper endpoints are public
    "allowed_hosts": ["api.imf.org", "www.imf.org", "services9.arcgis.com"],
    "models": {
        "ConsumerPriceIndex": {
            "client_seam": _SDMX_SEAM,
            "fixture_document": _cpi_envelope,
            "query": {
                "country": "united states",
                "transform": "index",
                "frequency": "annual",
                "expenditure": "total",
                "harmonized": False,
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 1, "body", 1],
                "value": "USA,CPI,_T,POP_PCH_PA_PT,A,2022,124.3",
            },
            "refusal_class": "ImfSdmxError",
            "refusal_code": "IMF_BAD_RESPONSE",
            "tamper_reason": (
                "the requested series is keyed with TYPE_OF_TRANSFORMATION=IX (the "
                "transform='index' selector), so an observation row published under "
                "POP_PCH_PA_PT is a different transformation than the one contracted and "
                "transform_data's per-row dimension check must refuse the payload as "
                "IMF_BAD_RESPONSE instead of labelling a percent-change reading as an index"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "AvailableIndicators": {
            "client_seam": _SDMX_SEAM,
            "fixture_document": _available_envelope,
            "query": {"dataflows": "CPI"},
            "tamper": {
                "op": "drop",
                "path": [
                    "responses",
                    1,
                    "body",
                    "data",
                    "dataStructures",
                    0,
                    "dataStructureComponents",
                    "dimensionList",
                    "dimensions",
                    0,
                    "position",
                ],
            },
            "refusal_class": "ImfSdmxError",
            "refusal_code": "IMF_BAD_RESPONSE",
            "tamper_reason": (
                "an SDMX dimension list is only usable when every dimension declares its "
                "position, because that is what fixes key order, so a DSD whose first "
                "dimension omits 'position' must be refused by data_dimensions as "
                "IMF_BAD_RESPONSE rather than cataloguing indicators from an unorderable "
                "structure"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "DirectionOfTrade": {
            "client_seam": _SDMX_SEAM,
            "fixture_document": _dot_envelope,
            "query": {
                "country": "united states",
                "counterpart": "japan",
                "direction": "balance",
                "frequency": "annual",
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 1, "body", 1],
                "value": "USA,JPN,XG_FOB_USD,A,1,2022,-62085.0",
            },
            "refusal_class": "ImfSdmxError",
            "refusal_code": "IMF_BAD_RESPONSE",
            "tamper_reason": (
                "direction='balance' keys the request on INDICATOR=TBG_USD, so an "
                "observation row published under XG_FOB_USD is the exports series the "
                "caller never asked for and transform_data's per-row indicator check must "
                "refuse the payload as IMF_BAD_RESPONSE rather than emitting an export "
                "value as a trade balance"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "EconomicIndicators": {
            "client_seam": _SDMX_SEAM,
            "fixture_document": _economic_envelope,
            "query": {"symbol": "PCPI::PCPIPCH", "country": "united states", "frequency": "annual"},
            "tamper": {
                "op": "set",
                "path": ["responses", 2, "body", 2],
                "value": "USA,PCPI,A,IX,2023,4.1",
            },
            "refusal_class": "ImfSdmxError",
            "refusal_code": "IMF_BAD_RESPONSE",
            "tamper_reason": (
                "the symbol selects INDICATOR=PCPIPCH within the PCPI flow, so an "
                "observation row published under the level indicator PCPI is a different "
                "series than the one requested and transform_data's per-row indicator "
                "check must refuse the payload as IMF_BAD_RESPONSE instead of blending an "
                "index level into an inflation-rate series"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "MaritimeChokePointInfo": {
            "client_seam": _PORTWATCH_SEAM,
            "fixture_document": _chokepoint_info_envelope,
            "query": {"max_requests": 8},
            "tamper": {
                "op": "set",
                "path": [
                    "responses",
                    0,
                    "sequence",
                    1,
                    "features",
                    0,
                    "attributes",
                    "vessel_count_total",
                ],
                "value": 999,
            },
            "refusal_class": "ImfPortWatchError",
            "refusal_code": "IMF_PORTWATCH_BAD_OBSERVATION",
            "tamper_reason": (
                "the layer documents vessel_count_total as the sum of the container, dry-bulk, "
                "general-cargo, RoRo and tanker columns, so a metadata row whose total (999) "
                "contradicts its own components (660) must be refused by _require_integer_sum "
                "as IMF_PORTWATCH_BAD_OBSERVATION rather than published as an arithmetic "
                "impossible chokepoint count"
            ),
            "source_files": _PORTWATCH_SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "MaritimeChokePointVolume": {
            "client_seam": _PORTWATCH_SEAM,
            "fixture_document": _chokepoint_volume_envelope,
            "query": {"chokepoint": "suez_canal", "max_requests": 8},
            "tamper": {
                "op": "set",
                "path": [
                    "responses",
                    1,
                    "sequence",
                    1,
                    "features",
                    0,
                    "attributes",
                    "date",
                ],
                "value": 1714521600000,
            },
            "refusal_class": "ImfPortWatchError",
            "refusal_code": "IMF_PORTWATCH_BAD_OBSERVATION",
            "tamper_reason": (
                "the daily layer publishes DateOnly as an ISO text field, so a row carrying an "
                "ArcGIS epoch-millisecond integer instead must be refused by _source_date as "
                "IMF_PORTWATCH_BAD_OBSERVATION rather than coerced into a wrong transit date"
            ),
            "source_files": _PORTWATCH_SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "PortInfo": {
            "client_seam": _PORTWATCH_SEAM,
            "fixture_document": _port_info_envelope,
            "query": {"country": "USA", "continent": "north_america", "limit": 1},
            "tamper": {
                "op": "set",
                "path": [
                    "responses",
                    0,
                    "sequence",
                    1,
                    "features",
                    0,
                    "attributes",
                    "ISO3",
                ],
                "value": "US",
            },
            "refusal_class": "ImfPortWatchError",
            "refusal_code": "IMF_PORTWATCH_BAD_OBSERVATION",
            "tamper_reason": (
                "ImfPortInfo.country_code is the layer's three-letter ISO identity, so a port "
                "row published with ISO3='US' fails the [A-Z]{3} shape that _required_iso3 "
                "enforces and must be refused as IMF_PORTWATCH_BAD_OBSERVATION instead of "
                "entering the catalogue with a two-letter country code"
            ),
            "source_files": _PORTWATCH_SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "PortVolume": {
            "client_seam": _PORTWATCH_SEAM,
            "fixture_document": _port_volume_envelope,
            "query": {"port_code": "USLAX"},
            "tamper": {
                "op": "set",
                "path": [
                    "responses",
                    0,
                    "sequence",
                    1,
                    "features",
                    0,
                    "attributes",
                    "export_tanker",
                ],
                "value": -5,
            },
            "refusal_class": "ImfPortWatchError",
            "refusal_code": "IMF_PORTWATCH_BAD_OBSERVATION",
            "tamper_reason": (
                "export_tanker is a metric-ton estimate, so a negative cell cannot be a traded "
                "quantity and _metric_ton_integer's non-negative clause must refuse the row as "
                "IMF_PORTWATCH_BAD_OBSERVATION rather than let -5 tons flow into the daily "
                "export total and its consistency diagnostics"
            ),
            "source_files": _PORTWATCH_SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
    },
}
