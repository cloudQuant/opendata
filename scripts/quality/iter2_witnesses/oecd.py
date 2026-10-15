"""Offline witness configuration for the ``oecd`` provider (wave 7).

Every OECD iter2 fetcher owns its transport: every leg goes through
``opendata.data.providers.oecd.models._client.fetch_observations``, which reads
``get_shared_http_client()`` as a module-level name, so each entry declares a
``client_seam`` over that one module. The shipped decoder is a strict CSV reader
(``format=csvfile`` SDMX payloads), so each fixture envelope carries its provider
body as CSV lines -- exactly the carrier ``fetch_observations`` parses -- and the
parsing, dimension validation and frozen macro_iter2 contracts that ship are the
code the five phases exercise, with the runner's socket circuit breaker making
"offline" enforceable rather than asserted.

The refusal face is the provider's own: ``OecdProviderError`` carries ``code``
and ``status``; the tamper refusals here (``OECD_BAD_RESPONSE`` /
``OECD_BAD_OBSERVATION``) are raised by ``_record_rows`` without an HTTP status,
so the declared ``status`` stays ``None`` and no column-attribution face is
claimed (attribution lives in the tamper_reason prose).
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.oecd.models._client"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/oecd/models/_client.py",
    "opendata/data/providers/oecd/models/iter2.py",
]
_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/macro_iter2.py",
    "opendata/data/domains.yaml",
]


def _envelope(url_contains: str, lines: list[str]) -> dict[str, Any]:
    """One CSV body served through the shared-client seam, matched by URL."""
    return {
        "responses": [
            {
                "url_contains": url_contains,
                "status": 200,
                "content_type": "text/plain",
                "body": lines,
            }
        ]
    }


def _cli_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.STES,DSD_STES@DF_CLI,4.1/USA.M.LI",
        [
            "REF_AREA,FREQ,MEASURE,ADJUSTMENT,UNIT_MEASURE,TRANSFORMATION,"
            "ACTIVITY,TIME_HORIZ,METHODOLOGY,TIME_PERIOD,OBS_VALUE",
            "USA,M,LI,AA,IX,IX,_Z,_Z,H,2024-01,100.4",
            "USA,M,LI,AA,IX,IX,_Z,_Z,H,2024-02,100.6",
            "USA,M,LI,AA,IX,IX,_Z,_Z,H,2024-03,100.5",
        ],
    )


def _cpi_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/USA.M.N.CPI.PA._T.N.GY",
        [
            "REF_AREA,FREQ,METHODOLOGY,MEASURE,UNIT_MEASURE,EXPENDITURE,"
            "ADJUSTMENT,TRANSFORMATION,TIME_PERIOD,OBS_VALUE",
            "USA,M,N,CPI,PA,_T,N,GY,2024-01,3.1",
            "USA,M,N,CPI,PA,_T,N,GY,2024-02,3.2",
            "USA,M,N,CPI,PA,_T,N,GY,2024-03,3.5",
        ],
    )


def _rate_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.STES,DSD_KEI@DF_KEI,4.0/USA.M.IR3TIB.PA",
        [
            "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE",
            "USA,M,IR3TIB,PA,2024-04,5.25",
            "USA,M,IR3TIB,PA,2024-05,5.25",
            "USA,M,IR3TIB,PA,2024-06,5.25",
        ],
    )


def _forecast_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.ECO.MAD,DSD_EO@DF_EO,1.5/USA.GDPV_USD.A",
        [
            "REF_AREA,MEASURE,FREQ,UNIT_MEASURE,UNIT_MULT,TIME_PERIOD,OBS_VALUE",
            "USA,GDPV_USD,A,USD_EXC,0,2022,25500.5",
            "USA,GDPV_USD,A,USD_EXC,0,2023,26100.3",
            "USA,GDPV_USD,A,USD_EXC,0,2024,26900.1",
        ],
    )


def _gdp_nominal_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_USD,1.1/"
        "Q.Y.USA.S1.S1.B1GQ._Z._Z._Z.USD_PPP.V.LA.T0102",
        [
            "FREQ,ADJUSTMENT,REF_AREA,SECTOR,COUNTERPART_SECTOR,TRANSACTION,"
            "INSTR_ASSET,ACTIVITY,EXPENDITURE,UNIT_MEASURE,PRICE_BASE,"
            "TRANSFORMATION,TABLE_IDENTIFIER,UNIT_MULT,TIME_PERIOD,OBS_VALUE",
            "Q,Y,USA,S1,S1,B1GQ,_Z,_Z,_Z,USD_PPP,V,LA,T0102,0,2024-Q1,6750000.5",
            "Q,Y,USA,S1,S1,B1GQ,_Z,_Z,_Z,USD_PPP,V,LA,T0102,0,2024-Q2,6820000.2",
        ],
    )


def _gdp_real_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_USD,1.1/"
        "Q.Y.USA.S1.S1.B1GQ._Z._Z._Z.USD_PPP.LR.LA.T0102",
        [
            "FREQ,ADJUSTMENT,REF_AREA,SECTOR,COUNTERPART_SECTOR,TRANSACTION,"
            "INSTR_ASSET,ACTIVITY,EXPENDITURE,UNIT_MEASURE,PRICE_BASE,"
            "TRANSFORMATION,TABLE_IDENTIFIER,UNIT_MULT,TIME_PERIOD,OBS_VALUE",
            "Q,Y,USA,S1,S1,B1GQ,_Z,_Z,_Z,USD_PPP,LR,LA,T0102,0,2024-Q1,4900.5",
            "Q,Y,USA,S1,S1,B1GQ,_Z,_Z,_Z,USD_PPP,LR,LA,T0102,0,2024-Q2,4950.2",
        ],
    )


def _house_price_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.ECO.MPD,DSD_AN_HOUSE_PRICES@DF_HOUSE_PRICES,1.0/USA.Q.RHP.IX",
        [
            "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,ADJUSTMENT,BASE_PER,UNIT_MULT,"
            "TIME_PERIOD,OBS_VALUE",
            "USA,Q,RHP,IX,S,2015,0,2024-Q1,110.2",
            "USA,Q,RHP,IX,S,2015,0,2024-Q2,111.4",
        ],
    )


def _share_price_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/USA.M.SHARE.IX",
        [
            "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE",
            "USA,M,SHARE,IX,2024-01,120.5",
            "USA,M,SHARE,IX,2024-02,121.3",
            "USA,M,SHARE,IX,2024-03,119.8",
        ],
    )


def _unemployment_envelope() -> dict[str, Any]:
    return _envelope(
        "OECD.SDD.TPS,DSD_LFS@DF_IALFS_INDIC,1.0/USA.UNE_LF_M",
        [
            "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TRANSFORMATION,ADJUSTMENT,"
            "SEX,AGE,ACTIVITY,TIME_PERIOD,OBS_VALUE",
            "USA,M,UNE_LF_M,PT_LF_SUB,_Z,N,_T,Y_GE15,_Z,2024-01,3.7",
            "USA,M,UNE_LF_M,PT_LF_SUB,_Z,N,_T,Y_GE15,_Z,2024-02,3.8",
            "USA,M,UNE_LF_M,PT_LF_SUB,_Z,N,_T,Y_GE15,_Z,2024-03,3.5",
        ],
    )


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.oecd.registration",
    "credential_env": {},  # the OECD SDMX endpoint is public; no token exists to fake
    "allowed_hosts": ["sdmx.oecd.org"],
    "models": {
        "CompositeLeadingIndicator": {
            "client_seam": _SEAM,
            "fixture_document": _cli_envelope,
            "query": {"country": "united_states"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 2],
                "value": "USA,M,LI,N,IX,IX,_Z,_Z,H,2024-02,100.6",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "the amplitude-adjusted CLI series is keyed and contracted with ADJUSTMENT=AA, "
                "so an observation row published with ADJUSTMENT=N contradicts the requested "
                "series identity and transform_data's expected_dimensions check must refuse "
                "the payload as OECD_BAD_RESPONSE instead of mixing an unadjusted reading in"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "ConsumerPriceIndex": {
            "client_seam": _SEAM,
            "fixture_document": _cpi_envelope,
            "query": {"country": "united_states"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 2],
                "value": "USA,M,N,CPI,PA,_T,N,GY,2024-02,not-a-number",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_OBSERVATION",
            "tamper_reason": (
                "OBS_VALUE is the contract's numeric CPI reading, so an observation cell "
                "that is not a parseable finite number must be refused by _record_rows as "
                "OECD_BAD_OBSERVATION instead of being coerced or dropped"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "CountryInterestRates": {
            "client_seam": _SEAM,
            "fixture_document": _rate_envelope,
            "query": {"country": "united_states", "duration": "short"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "USA,M,IRLT,PA,2024-04,5.25",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "the short-rate query is contracted to MEASURE=IR3TIB, so a payload row "
                "published under MEASURE=IRLT (the long rate) is a different series than the "
                "one requested and transform_data's expected_dimensions check must refuse it "
                "as OECD_BAD_RESPONSE"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "GdpForecast": {
            "client_seam": _SEAM,
            "fixture_document": _forecast_envelope,
            "query": {"country": "united_states", "frequency": "annual", "units": "volume"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "USA,GDPV_USD,A,USD_EXC,30,2022,25500.5",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_OBSERVATION",
            "tamper_reason": (
                "forecast values are rescaled by UNIT_MULT before entering the contract, so a "
                "UNIT_MULT exponent outside the supported -12..12 range must be refused by "
                "_record_rows as OECD_BAD_OBSERVATION instead of silently mis-scaling GDP"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "GdpNominal": {
            "client_seam": _SEAM,
            "fixture_document": _gdp_nominal_envelope,
            "query": {
                "country": "united_states",
                "frequency": "quarter",
                "units": "level",
                "price_base": "current_prices",
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 2],
                "value": "Q,Y,USA,S15,S1,B1GQ,_Z,_Z,_Z,USD_PPP,V,LA,T0102,0,2024-Q2,6820000.2",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "GDP level is contracted to the whole-economy SECTOR=S1 position, so an "
                "observation row published with SECTOR=S15 is a different national-accounts "
                "series and transform_data's expected_dimensions check must refuse it as "
                "OECD_BAD_RESPONSE"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "GdpReal": {
            "client_seam": _SEAM,
            "fixture_document": _gdp_real_envelope,
            "query": {"country": "united_states", "frequency": "quarter"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "Q,Y,USA,S1,S1,B1GQ,_Z,_Z,_Z,USD_PPP,LR,LA,T0102,0,2024-Q1,..",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_OBSERVATION",
            "tamper_reason": (
                "OecdGdpReal.value is a required non-null float, so an observation cell "
                "carrying OECD's missing marker '..' must be refused by _record_rows as "
                "OECD_BAD_OBSERVATION instead of producing a null real-GDP row"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "HousePriceIndex": {
            "client_seam": _SEAM,
            "fixture_document": _house_price_envelope,
            "query": {
                "country": "united_states",
                "frequency": "quarter",
                "transform": "index",
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "USA,Q,RHP,IX,S,2020,0,2024-Q1,110.2",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "the contracted house-price series is the 2015-base seasonal-adjusted index, "
                "so a row published with BASE_PER=2020 is a different base period than the "
                "one requested and transform_data's expected_dimensions check must refuse it "
                "as OECD_BAD_RESPONSE"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "SharePriceIndex": {
            "client_seam": _SEAM,
            "fixture_document": _share_price_envelope,
            "query": {"country": "united_states", "frequency": "monthly"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "GBR,M,SHARE,IX,2024-01,120.5",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "the query requested the United States share-price index, so an observation "
                "row published with REF_AREA=GBR belongs to an area the caller never asked "
                "for and _record_rows must refuse the payload as OECD_BAD_RESPONSE instead "
                "of emitting a UK row under a USA query"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
        "Unemployment": {
            "client_seam": _SEAM,
            "fixture_document": _unemployment_envelope,
            "query": {
                "country": "united_states",
                "frequency": "monthly",
                "sex": "total",
                "age": "total",
                "seasonal_adjustment": False,
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 2],
                "value": "USA,M,UNE_LF_M,PT_LF_SUB,_Z,N,M,Y_GE15,_Z,2024-02,3.8",
            },
            "refusal_class": "OecdProviderError",
            "refusal_code": "OECD_BAD_RESPONSE",
            "tamper_reason": (
                "the query is contracted to the total-population SEX=_T breakdown, so a "
                "monthly observation row published with SEX=M mixes a male-only rate into "
                "the total series and transform_data's expected_dimensions check must refuse "
                "the payload as OECD_BAD_RESPONSE"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES,
        },
    },
}
