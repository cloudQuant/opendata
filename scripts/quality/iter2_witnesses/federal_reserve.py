"""Offline witness configuration for the ``federal_reserve`` provider.

The six frozen inventory rows split by the body their declaration reads. Three of them --
``FederalFundsRate``, ``SOFR`` and ``OvernightBankFundingRate`` -- are the New York Fed
reference-rate JSON documents in :mod:`opendata.data.providers.federal_reserve.specs`: a
generated engine fetcher, ``rows_pointer="refRates"``, and no decoder, so they run through the
engine's ``transport`` seam exactly like the other declaration-engine witnesses and are
witnessed here. Their refusals are attributed by the engine itself -- the required-timestamp
missing face, the numeric guard's bool-for-float face, and the reviewed row model's
str-field-rejects-a-number face -- so each entry declares ``expected_*`` from its own contract
column name, never from a captured run.

The other three frozen rows are deliberately absent from this module, and the reason is the
body rather than the work: ``MoneyMeasures``, ``TreasuryRates`` and ``SvenssonYieldCurve``
declare ``DecoderSpec(kind="csv")`` (the H.6 and H.15 ``Output.aspx`` releases and the FEDS
wide table), and a delimited declaration reads :attr:`HttpResponse.payload`, which the
evidence probe's ``HttpResponse(200, document)`` never populates -- :func:`decode_response`
answers that wiring with ``FEDERAL_RESERVE_PAYLOAD_MISSING``. Witnessing that code would
record the fixture's missing bytes as if it were the contract's refusal, so the three stay
unwitnessed and are reported instead.

Two of the seven bound rows with no frozen inventory contract row -- ``PrimaryDealerFails`` and
``PrimaryDealerPositioning`` -- are witnessed here after this wave's clean-room step. Each bound
fetcher builds the contract its own domain declares -- ``PrimaryDealerFails`` /
``PrimaryDealerPositioning`` in :mod:`opendata.data.models.federal_reserve_clean_nyfed`, whose
``symbol``/``title``/``value`` fields are exactly what :file:`opendata/data/domains.yaml` names
as the natural key -- because the governed service revalidates every row against
``contract_model(domain)`` rather than against the fetcher's own class: a fetcher that returns a
look-alike still passes the direct leg and refuses on the service leg, which is what this
provider's two ``service`` claims were reporting. The New York Fed primary-dealer client gained
the engine's ``HttpGet`` seam. Their refusals are this provider's own sanitized source codes,
which the probe reads as ``ProviderEngineError`` faces because ``FederalReserveSourceError`` now
carries that family beside the ``ValueError`` it has always been; both are code-only, so neither
entry declares attributed detail. The five remaining bound rows (``YieldCurve``,
``CentralBankHoldings``, ``InflationExpectations``, ``FomcDocuments``,
``TotalFactorProductivity``) read workbook/archive bytes or an HTML index, which the single
canned JSON document answers with a decode refusal that belongs to the fixture, not the
contract.

Witnesses run exclusively through the offline seam (the engine's ``transport`` / the fetchers'
``http_transport``) served by ``FixedResponseTransport``. No leg touches the network and no
``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.
"""

from __future__ import annotations

from typing import Any, Final


def _rate_record(**cells: object) -> dict[str, object]:
    """One New York Fed reference-rate record with its published cells spelled as the kernel reads.

    The staged percent columns are published as exact decimal text, because
    :mod:`opendata.data.providers._engine.value_transform` refuses a binary float on a rescaled
    column (that refusal is priced in ``DECLARED_GAPS``), while an absent key and a published
    integer zero stay themselves.
    """
    return dict(cells)


def _fed_federal_funds_rate_document() -> dict[str, list[dict[str, object]]]:
    """Two ``effr/search.json`` records under the pointer ``FEDERAL_FUNDS_RATE`` follows.

    ``type`` and ``footnoteId`` are published by the endpoint but undeclared by the
    declaration, so they ride the record and never reach the row model. The second record
    carries the published integer zero (the division keeps it at zero) and the ``''`` revision
    spelling the declaration's ``REVISION_NULL_SENTINEL`` nulls before any stage.
    """
    return {
        "refRates": [
            _rate_record(
                effectiveDate="2024-06-25",
                percentRate="4.33",
                targetRateTo="5.50",
                targetRateFrom="5.25",
                percentPercentile1="4.20",
                percentPercentile25="4.30",
                percentPercentile75="4.40",
                percentPercentile99="4.50",
                volumeInBillions="185.0",
                intraDayLow="4.10",
                intraDayHigh="4.60",
                stdDeviation="0.05",
                revisionIndicator="R",
                type="FEDFUNDS",
                footnoteId="F",
            ),
            _rate_record(
                effectiveDate="2024-06-26",
                percentRate=0,
                targetRateTo="5.50",
                targetRateFrom="5.25",
                percentPercentile1="4.21",
                percentPercentile25="4.31",
                percentPercentile75="4.41",
                percentPercentile99="4.51",
                volumeInBillions="172.5",
                intraDayLow="4.11",
                intraDayHigh="4.61",
                stdDeviation="0.06",
                revisionIndicator="",
                type="FEDFUNDS",
                footnoteId=None,
            ),
        ]
    }


def _fed_sofr_document() -> dict[str, list[dict[str, object]]]:
    """Two ``sofr/search.json`` records under the pointer ``SOFR`` follows.

    The four SOFRAI columns (``average30day``/``average90day``/``average180day``/``index``) are
    published by the second endpoint only, so this one-GET body leaves them absent and the
    contract's four optionals read as null -- the reading the declaration's own notes state.
    ``percentRate`` carries no sentinel here: the reviewed contract makes it a required
    non-nullable float.
    """
    return {
        "refRates": [
            _rate_record(
                effectiveDate="2024-06-25",
                percentRate="5.33",
                percentPercentile1="5.10",
                percentPercentile25="5.20",
                percentPercentile75="5.40",
                percentPercentile99="5.50",
                volumeInBillions="2350.0",
                type="SOFR",
                footnoteId="S",
                revisionIndicator="R",
            ),
            _rate_record(
                effectiveDate="2024-06-26",
                percentRate="5.34",
                percentPercentile1="",
                percentPercentile25="5.21",
                percentPercentile75="5.41",
                percentPercentile99="5.51",
                volumeInBillions="2410.5",
                type="SOFR",
                footnoteId=None,
                revisionIndicator="R",
            ),
        ]
    }


def _fed_overnight_bank_funding_rate_document() -> dict[str, list[dict[str, object]]]:
    """Two ``obfr/search.json`` records under the pointer ``OVERNIGHT_BANK_FUNDING_RATE`` follows.

    This row declares NO null sentinel (``normalize_percent`` guards only ``None``), so every
    published cell reaches its stage or its row model verbatim, and ``revisionIndicator`` is
    the nullable text the reviewed contract carries.
    """
    return {
        "refRates": [
            _rate_record(
                effectiveDate="2024-06-25",
                percentRate="2.34",
                percentPercentile1="2.08",
                percentPercentile25="2.25",
                percentPercentile75="2.40",
                percentPercentile99="2.55",
                volumeInBillions="98.0",
                revisionIndicator="R",
                type="OBFR",
                footnoteId="F",
            ),
            _rate_record(
                effectiveDate="2024-06-26",
                percentRate=0,
                percentPercentile1="2.09",
                percentPercentile25="2.26",
                percentPercentile75="2.41",
                percentPercentile99="2.56",
                volumeInBillions="101.5",
                revisionIndicator="P",
                type="OBFR",
                footnoteId=None,
            ),
        ]
    }


def _pd_item(key_id: str, description: str, as_of_date: str, value: str) -> dict[str, str]:
    """One ``pd`` timeseries item carrying both the catalogue and the observation cells.

    The New York Fed primary-dealer route reads ``keyid``/``description`` out of
    ``/api/pd/list/timeseries.json`` and ``keyid``/``asofdate``/``value`` out of
    ``/api/pd/get/<ids>.json``. The offline seam answers every request with one canned body, so
    this fixture publishes the union of the two vocabularies -- the keys each leg actually reads,
    with the official catalogue identifiers and descriptions of
    ``test_primary_dealer_fixtures_match_current_official_catalogue_ids``.
    """
    return {"keyid": key_id, "description": description, "asofdate": as_of_date, "value": value}


def _fed_primary_dealer_fails_document() -> dict[str, dict[str, list[dict[str, str]]]]:
    """Two weekly TIPS financing-fails observations in published USD millions."""
    return {
        "pd": {
            "timeseries": [
                _pd_item(
                    "PDFTD-UST",
                    "TREASURY INFLATION-PROTECTED SECURITIES (TIPS): DEALER FINANCING "
                    "FAILS TO DELIVER",
                    "2024-06-19",
                    "11980.5",
                ),
                _pd_item(
                    "PDFTD-UST",
                    "TREASURY INFLATION-PROTECTED SECURITIES (TIPS): DEALER FINANCING "
                    "FAILS TO DELIVER",
                    "2024-06-26",
                    "12215.0",
                ),
            ]
        }
    }


def _fed_primary_dealer_positioning_document() -> dict[str, dict[str, list[dict[str, str]]]]:
    """Two weekly Treasury dealer-position observations, published as whole USD millions.

    The reviewed positioning contract declares a whole ``int`` value, and its reference reader
    refuses a fractional cell rather than truncating it, so these source texts stay integral.
    """
    return {
        "pd": {
            "timeseries": [
                _pd_item(
                    "PDPOSGST-TOT",
                    "TOTAL U.S. TREASURY SECURITIES (EXCLUDING TIPS) DEALER POSITION LONG - SHORT",
                    "2024-06-19",
                    "-25480.0",
                ),
                _pd_item(
                    "PDPOSGST-TOT",
                    "TOTAL U.S. TREASURY SECURITIES (EXCLUDING TIPS) DEALER POSITION LONG - SHORT",
                    "2024-06-26",
                    "31750.0",
                ),
            ]
        }
    }


_SPECS: Final[str] = "opendata/data/providers/federal_reserve/specs.py"
_RATE_MODEL_DIR: Final[str] = "opendata/data/providers/federal_reserve/models"
_SOURCE: Final[str] = "opendata/data/providers/federal_reserve/_source.py"
_ENGINE: Final[str] = "opendata/data/providers/_engine/http_json.py"
_CLEAN_RATES: Final[str] = "opendata/data/models/federal_reserve_clean_rates.py"
_CLEAN_NYFED: Final[str] = "opendata/data/models/federal_reserve_clean_nyfed.py"
_PD_HELPER: Final[str] = "opendata/data/providers/federal_reserve/models/_primary_dealer.py"
_ITER2_CLIENT: Final[str] = "opendata/data/providers/federal_reserve/_iter2_client.py"
_DOMAINS: Final[str] = "opendata/data/domains.yaml"


def _engine_source_files(model_module: str) -> list[str]:
    """The files the implementation legs of one declaration-engine witness pin."""
    return [f"{_RATE_MODEL_DIR}/{model_module}", _SPECS, _SOURCE, _ENGINE]


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.federal_reserve.registration",
    "credential_env": {},
    "allowed_hosts": ["markets.newyorkfed.org"],
    "models": {
        "FederalFundsRate": {
            "fixture_document": _fed_federal_funds_rate_document,
            "query": {"start_date": "2024-06-01", "end_date": "2024-06-30"},
            "tamper": {
                "op": "drop",
                "path": ["refRates", 0, "effectiveDate"],
                "column": "date",
            },
            "expected_missing": ["date"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
            "tamper_reason": (
                "the declaration marks effectiveDate required and non-nullable and the reviewed "
                "FederalReserveFederalFundsRate date field is a bare datetime.date, so a record "
                "that stops publishing it must refuse as FEDERAL_RESERVE_SHAPE_INVALID naming "
                "the 'date' column rather than a rate row with no day"
            ),
            "source_files": _engine_source_files("federal_funds_rate.py"),
            "contract_files": [_CLEAN_RATES, _DOMAINS],
        },
        "SOFR": {
            "fixture_document": _fed_sofr_document,
            "query": {"start_date": "2024-06-01", "end_date": "2024-06-30"},
            "tamper": {
                "op": "set",
                "path": ["refRates", 0, "percentRate"],
                "value": True,
                "column": "rate",
            },
            "expected_rejected": ["rate"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
            "tamper_reason": (
                "SOFR declares percentRate as a float kind and the reviewed contract SOFR.rate "
                "is a required non-nullable float, so the engine's numeric guard must refuse a "
                "published flag as FEDERAL_RESERVE_SHAPE_INVALID rejected=('rate',) instead of "
                "letting True become a 1.0 rate"
            ),
            "source_files": _engine_source_files("sofr.py"),
            "contract_files": [_CLEAN_NYFED, _DOMAINS],
        },
        "OvernightBankFundingRate": {
            "fixture_document": _fed_overnight_bank_funding_rate_document,
            "query": {"start_date": "2024-06-01", "end_date": "2024-06-30"},
            "tamper": {
                "op": "set",
                "path": ["refRates", 0, "revisionIndicator"],
                "value": 123,
                "column": "revision_indicator",
            },
            "expected_rejected": ["revision_indicator"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
            "tamper_reason": (
                "this row declares no sentinel and no stage for revisionIndicator, so the cell "
                "reaches the reviewed contract's str | None field verbatim; publishing a JSON "
                "number there is outside the documented shape and must surface as "
                "FEDERAL_RESERVE_SHAPE_INVALID rejected=('revision_indicator',)"
            ),
            "source_files": _engine_source_files("overnight_bank_funding_rate.py"),
            "contract_files": [_CLEAN_RATES, _DOMAINS],
        },
        "PrimaryDealerFails": {
            "fixture_document": _fed_primary_dealer_fails_document,
            "query": {
                "key_ids": ["PDFTD-UST"],
                "start_date": "2024-06-01",
                "end_date": "2024-06-30",
            },
            "tamper": {"op": "drop", "path": ["pd", "timeseries", 0, "asofdate"]},
            "refusal_class": "FederalReserveSourceError",
            "refusal_code": "FEDERAL_RESERVE_BAD_PD_DATA_ROW",
            "tamper_reason": (
                "every observation the primary-dealer route reads must publish its as-of date: "
                "the reviewed PrimaryDealerFails date field is a required "
                "datetime.date, so a timeseries item without 'asofdate' has to refuse rather "
                "than land as a fails observation on no week"
            ),
            "source_files": [
                f"{_RATE_MODEL_DIR}/primary_dealer_fails.py",
                _PD_HELPER,
                _ITER2_CLIENT,
            ],
            "contract_files": [_CLEAN_NYFED, _DOMAINS],
        },
        "PrimaryDealerPositioning": {
            "fixture_document": _fed_primary_dealer_positioning_document,
            "query": {
                "key_ids": ["PDPOSGST-TOT"],
                "start_date": "2024-06-01",
                "end_date": "2024-06-30",
            },
            "tamper": {
                "op": "set",
                "path": ["pd", "timeseries", 0, "asofdate"],
                "value": "26-06-2024",
            },
            "refusal_class": "FederalReserveSourceError",
            "refusal_code": "FEDERAL_RESERVE_BAD_DATE",
            "tamper_reason": (
                "the contract's date field is a datetime.date and the reader requires the "
                "canonical YYYY-MM-DD spelling the endpoint publishes, so a day-month-ordered "
                "cell must refuse as FEDERAL_RESERVE_BAD_DATE instead of being reinterpreted "
                "into the wrong week's position"
            ),
            "source_files": [
                f"{_RATE_MODEL_DIR}/primary_dealer_positioning.py",
                _PD_HELPER,
                _ITER2_CLIENT,
            ],
            "contract_files": [_CLEAN_NYFED, _DOMAINS],
        },
    },
}
