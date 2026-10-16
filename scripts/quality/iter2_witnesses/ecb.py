"""Offline witness configuration for the ``ecb`` provider.

ECB speaks SDMX-ML 2.1 GenericData, not JSON, and all three fetchers under witness pass
``self.http_transport`` into their client (:mod:`._series_client` and :mod:`._reference_client`
each take the ``transport is not None`` branch), so each witness installs the repository's
``FixedResponseTransport`` and the seam hands the shipped parser the bytes a real answer would
have carried: the document is the list of its own source lines and ``ecb.models._client.seam_body``
rejoins them exactly once. No leg touches a socket, and the XML envelope, the dimension checks,
the period checks and the row contracts under test are the ones that ship.

The refusals here are ECB's own stable codes (``EcbProviderError``, the engine's attributable
refusal type for this source). They are deliberately code-only: a period that is not a monthly
``YYYY-MM``, an observation outside the requested window, and a quote currency that was never
asked for are all refused before any contract column is addressed, so no cell exists for the
refusal to attribute. ``ECB_BAD_RESPONSE`` carries ``status=200`` because the parser rejects a
response the source did successfully serve; ``ECB_BAD_OBSERVATION`` carries ``status=None``
because that gate is the Fetcher's own slice check, not an HTTP outcome.

What these three witnesses do not cover: the ``transport is None`` production leg, which reaches
``get_shared_http_client()`` instead of the injected seam, is not exercised here -- the engine
seam is what the fetchers honour and what the claim pins. The SDMX ``Header`` fields, the dataset
and group layers, and the attribute layers are published as the source publishes them, but every
number and every series key in these bodies is synthetic; they prove the reading and the gates,
not that the ECB Data Portal holds them.
"""

from __future__ import annotations

from typing import Any, Final

_MESSAGE_NS: Final[str] = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
_COMMON_NS: Final[str] = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_GENERIC_NS: Final[str] = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
_XSI_NS: Final[str] = "http://www.w3.org/2001/XMLSchema-instance"

#: One structure id shared by the Header declaration and every DataSet reference in one body.
_STRUCTURE_ID: Final[str] = "ECB_SYNTHETIC"

_YC_DIMENSIONS: Final[tuple[tuple[str, str], ...]] = (
    ("FREQ", "B"),
    ("REF_AREA", "U2"),
    ("CURRENCY", "EUR"),
    ("PROVIDER_FM", "4F"),
    ("INSTRUMENT_FM", "G_N_A"),
    ("PROVIDER_FM_ID", "SV_C_YM"),
    ("DATA_TYPE_FM", "SR_3M"),
)
_YC_SERIES_KEY: Final[str] = "YC." + ".".join(value for _, value in _YC_DIMENSIONS)

_BPS_DIMENSIONS: Final[tuple[tuple[str, str], ...]] = (
    ("FREQ", "M"),
    ("ADJUSTMENT", "N"),
    ("REF_AREA", "I10"),
    ("COUNTERPART_AREA", "W1"),
    ("REF_SECTOR", "S121"),
    ("COUNTERPART_SECTOR", "S1"),
    ("FLOW_STOCK_ENTRY", "T"),
    ("ACCOUNTING_ENTRY", "A"),
    ("INT_ACC_ITEM", "FA"),
    ("FUNCTIONAL_CAT", "R"),
    ("INSTR_ASSET", "F"),
    ("MATURITY", "_Z"),
    ("UNIT_MEASURE", "EUR"),
    ("CURRENCY_DENOM", "X1"),
    ("VALUATION", "_X"),
    ("COMP_METHOD", "N"),
    ("TYPE_ENTITY", "ALL"),
)
_BPS_SERIES_KEY: Final[str] = "BPS." + ".".join(value for _, value in _BPS_DIMENSIONS)


def _values(pairs: tuple[tuple[str, str], ...], indent: str) -> list[str]:
    """Publish one SDMX value list, one component per source line."""
    return [
        f'{indent}<generic:Value id="{component}" value="{value}"/>' for component, value in pairs
    ]


def _observation(
    period: str, value: str | None, attributes: tuple[tuple[str, str], ...]
) -> list[str]:
    """Publish one GenericData observation with its source period and raw value text."""
    lines = [
        "    <generic:Obs>",
        f'     <generic:ObsDimension value="{period}"/>',
    ]
    if value is not None:
        lines.append(f'     <generic:ObsValue value="{value}"/>')
    if attributes:
        lines.append("     <generic:Attributes>")
        lines.extend(_values(attributes, "      "))
        lines.append("     </generic:Attributes>")
    lines.append("    </generic:Obs>")
    return lines


def _series(
    dimensions: tuple[tuple[str, str], ...],
    series_attributes: tuple[tuple[str, str], ...],
    observations: list[list[str]],
) -> list[str]:
    """Publish one series: its complete key, its attribute layer, then its observations."""
    lines = ["   <generic:Series>", "    <generic:SeriesKey>"]
    lines.extend(_values(dimensions, "     "))
    lines.append("    </generic:SeriesKey>")
    if series_attributes:
        lines.append("    <generic:Attributes>")
        lines.extend(_values(series_attributes, "     "))
        lines.append("    </generic:Attributes>")
    for observation_lines in observations:
        lines.extend(observation_lines)
    lines.append("   </generic:Series>")
    return lines


def _document(series_blocks: list[list[str]]) -> list[str]:
    """Wrap series blocks in the SDMX 2.1 GenericData envelope the parsers require."""
    schema_location = f"{_MESSAGE_NS} SDMXMessage.xsd {_GENERIC_NS} SDMXDataGeneric.xsd"
    header = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<message:GenericData xmlns:message="{_MESSAGE_NS}" xmlns:common="{_COMMON_NS}"',
        f' xmlns:generic="{_GENERIC_NS}" xmlns:xsi="{_XSI_NS}"',
        f' xsi:schemaLocation="{schema_location}">',
        " <message:Header>",
        "  <message:ID>SYNTHETIC-ECB-FIXTURE</message:ID>",
        "  <message:Test>true</message:Test>",
        '  <message:Sender id="ECB">',
        "   <common:Name>European Central Bank</common:Name>",
        "  </message:Sender>",
        f'  <message:Structure structureID="{_STRUCTURE_ID}"',
        '   dimensionAtObservation="TIME_PERIOD">',
        "   <common:Structure>",
        '    <Ref agencyID="ECB" id="SYNTHETIC" version="1.0" class="DataStructure"'
        ' package="datastructure"/>',
        "   </common:Structure>",
        "  </message:Structure>",
        " </message:Header>",
        f' <message:DataSet structureRef="{_STRUCTURE_ID}">',
        "  <generic:Attributes>",
        '   <generic:Value id="LAST_UPDATE" value="2026-01-09T00:00:00"/>',
        "  </generic:Attributes>",
    ]
    for block in series_blocks:
        header.extend(block)
    header.append(" </message:DataSet>")
    header.append("</message:GenericData>")
    return header


def _line_index(lines: list[str], needle: str) -> int:
    """Resolve one counterfact target to its fixture line, refusing an ambiguous needle."""
    hits = [index for index, line in enumerate(lines) if needle in line]
    if len(hits) != 1:
        raise AssertionError(
            f"counterfact target {needle!r} matches {len(hits)} fixture line(s), not exactly one"
        )
    return hits[0]


#: One euro-area three-month rate over three business days, as the FM interest-rate product
#: publishes it: dataset, group, series and observation attribute layers all stay separate.
_YC_LINES: Final[list[str]] = _document(
    [
        [
            '   <generic:Group type="SOURCE_ATTACHMENT">',
            "    <generic:GroupKey>",
            '     <generic:Value id="FREQ" value="B"/>',
            "    </generic:GroupKey>",
            "    <generic:Attributes>",
            '     <generic:Value id="SOURCEattachment_NOTE" value="synthetic"/>',
            "    </generic:Attributes>",
            "   </generic:Group>",
        ]
    ]
    + [
        _series(
            _YC_DIMENSIONS,
            (("UNIT", "PER"), ("DECIMALS", "3")),
            [
                _observation("2026-01-05", "2.418", (("FREQ_PRICING", "B"),)),
                _observation("2026-01-06", "2.423", (("FREQ_PRICING", "B"),)),
                _observation("2026-01-07", "2.431", (("FREQ_PRICING", "B"),)),
            ],
        )
    ]
)

#: One monthly euro-area balance-of-payments flow over three months, keyed with all seventeen
#: official dimensions, and one quarterly sibling series in the same dataset.
_BPS_LINES: Final[list[str]] = _document(
    [
        _series(
            _BPS_DIMENSIONS,
            (("UNIT", "EUR"), ("DECIMALS", "0")),
            [
                _observation("2025-01", "341000", (("ACT_FLAG", "A"),)),
                _observation("2025-02", "352000", (("ACT_FLAG", "A"),)),
                _observation("2025-03", "-18000", (("ACT_FLAG", "E"),)),
            ],
        )
    ]
)

#: Two daily EUR-reference fixings, one per requested quote currency, in one dataset.
_EXR_LINES: Final[list[str]] = _document(
    [
        _series(
            (
                ("FREQ", "D"),
                ("CURRENCY", "USD"),
                ("CURRENCY_DENOM", "EUR"),
                ("EXR_TYPE", "SP00"),
                ("EXR_SUFFIX", "A"),
            ),
            (("UNIT", "USD"), ("DECIMALS", "4")),
            [
                _observation("2026-01-05", "1.0842", (("OBS_STATUS", "A"),)),
                _observation("2026-01-06", "1.0851", (("OBS_STATUS", "A"),)),
            ],
        ),
        _series(
            (
                ("FREQ", "D"),
                ("CURRENCY", "JPY"),
                ("CURRENCY_DENOM", "EUR"),
                ("EXR_TYPE", "SP00"),
                ("EXR_SUFFIX", "A"),
            ),
            (("UNIT", "JPY"), ("DECIMALS", "3")),
            [
                _observation("2026-01-05", "167.412", (("OBS_STATUS", "A"),)),
                _observation("2026-01-06", "167.905", (("OBS_STATUS", "A"),)),
            ],
        ),
    ]
)


def _yc_document() -> list[str]:
    """The YC GenericData body the offline seam answers the series request with."""
    return list(_YC_LINES)


def _bps_document() -> list[str]:
    """The BPS GenericData body the offline seam answers the series request with."""
    return list(_BPS_LINES)


def _exr_document() -> list[str]:
    """The EXR GenericData body the offline seam answers the reference-rate request with."""
    return list(_EXR_LINES)


_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/ecb/models/_client.py",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.ecb.registration",
    "allowed_hosts": ["data-api.ecb.europa.eu"],
    "models": {
        "YieldCurve": {
            "fixture_document": _yc_document,
            "query": {
                "series_key": _YC_SERIES_KEY,
                "start_date": "2026-01-05",
                "end_date": "2026-01-07",
                "max_records": 5,
            },
            "tamper": {
                "op": "set",
                "path": [_line_index(_YC_LINES, '<generic:ObsDimension value="2026-01-06"/>')],
                "value": '     <generic:ObsDimension value="2026-02-13"/>',
            },
            "refusal_class": "EcbProviderError",
            "refusal_code": "ECB_BAD_OBSERVATION",
            "tamper_reason": (
                "moving one observation to 2026-02-13 keeps the body well-formed and the value "
                "numeric, but puts it outside the 2026-01-05..2026-01-07 window the query asked "
                "for, so EcbYieldCurveFetcher.transform_data must refuse the row rather than "
                "silently widen the caller's curve slice"
            ),
            "source_files": [
                "opendata/data/providers/ecb/models/yield_curve.py",
                "opendata/data/providers/ecb/models/_series_client.py",
                "opendata/data/providers/ecb/models/_series_query.py",
                "opendata/data/providers/ecb/models/_series_sdmx.py",
                *_SOURCE_FILES,
            ],
            "contract_files": [
                "opendata/data/models/ecb_series.py",
                "opendata/data/models/currency.py",
                "opendata/data/domains.yaml",
            ],
        },
        "BalanceOfPayments": {
            "fixture_document": _bps_document,
            "query": {
                "series_key": _BPS_SERIES_KEY,
                "start_period": "2025-01",
                "end_period": "2025-03",
                "max_records": 5,
            },
            "tamper": {
                "op": "set",
                "path": [_line_index(_BPS_LINES, '<generic:ObsDimension value="2025-02"/>')],
                "value": '     <generic:ObsDimension value="2025-02-28"/>',
            },
            "refusal_class": "EcbProviderError",
            "refusal_code": "ECB_BAD_RESPONSE",
            "expected_status": 200,
            "tamper_reason": (
                "publishing a daily 2025-02-28 axis on a monthly FREQ=M series breaks the source "
                "period grammar the BPS contract is built on, so the parser must refuse the whole "
                "body with ECB_BAD_RESPONSE and status=200 instead of guessing which month was "
                "meant or trimming the period to 2025-02"
            ),
            "source_files": [
                "opendata/data/providers/ecb/models/balance_of_payments.py",
                "opendata/data/providers/ecb/models/_series_client.py",
                "opendata/data/providers/ecb/models/_series_query.py",
                "opendata/data/providers/ecb/models/_series_sdmx.py",
                *_SOURCE_FILES,
            ],
            "contract_files": [
                "opendata/data/models/ecb_series.py",
                "opendata/data/models/currency.py",
                "opendata/data/domains.yaml",
            ],
        },
        "CurrencyReferenceRates": {
            "fixture_document": _exr_document,
            "query": {
                "quote_currencies": ["USD", "JPY"],
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
                "max_records": 10,
            },
            "tamper": {
                "op": "set",
                "path": [_line_index(_EXR_LINES, '<generic:Value id="CURRENCY" value="JPY"/>')],
                "value": '     <generic:Value id="CURRENCY" value="CHF"/>',
            },
            "refusal_class": "EcbProviderError",
            "refusal_code": "ECB_BAD_OBSERVATION",
            "tamper_reason": (
                "renaming the second series' quote currency to CHF keeps a valid EUR-denominated "
                "SP00/A fixing but answers a currency the caller never asked for, so "
                "EcbCurrencyReferenceRatesFetcher.transform_data must refuse the row instead of "
                "letting an unrequested quote currency into the reference-rate frame"
            ),
            "source_files": [
                "opendata/data/providers/ecb/models/reference_rates.py",
                "opendata/data/providers/ecb/models/_reference_client.py",
                "opendata/data/providers/ecb/models/_reference_rates.py",
                *_SOURCE_FILES,
            ],
            "contract_files": [
                "opendata/data/models/currency.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
