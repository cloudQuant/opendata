"""Offline source-shape checks for Federal Reserve iteration-2 typed adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook

from opendata.data.domains import DomainSpec, contract_model
from opendata.data.models.federal_reserve_clean_documents import (
    FederalReserveYieldCurveObservation,
)
from opendata.data.models.federal_reserve_clean_nyfed import (
    PrimaryDealerFails,
    PrimaryDealerPositioning,
)
from opendata.data.models.federal_reserve_macro import (
    FederalReserveCentralBankHolding,
    FederalReserveFomcDocument,
    FederalReserveInflationExpectations,
    FederalReserveTotalFactorProductivity,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.federal_reserve._iter2_client import FederalReserveSourceError
from opendata.data.providers.federal_reserve.models import (
    _primary_dealer,
    central_bank_holdings,
    fomc_documents,
    inflation_expectations,
    total_factor_productivity,
    yield_curve,
)
from opendata.data.providers.federal_reserve.provider import PROVIDER
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_H15_COLUMNS = (
    "Time Period",
    "RIFLGFCM01_N.B",
    "RIFLGFCM03_N.B",
    "RIFLGFCM06_N.B",
    "RIFLGFCY01_N.B",
    "RIFLGFCY02_N.B",
    "RIFLGFCY03_N.B",
    "RIFLGFCY05_N.B",
    "RIFLGFCY07_N.B",
    "RIFLGFCY10_N.B",
    "RIFLGFCY20_N.B",
    "RIFLGFCY30_N.B",
)
_TFP_COLUMNS = (
    "date",
    "dY_prod",
    "dY_inc",
    "dY",
    "dhours",
    "dLP",
    "dk",
    "dLQ_BLS_interpolated",
    "dLQ_Aaronson_Sullivan",
    "dLQ",
    "alpha",
    "dtfp",
    "dutil",
    "dtfp_util",
    "relativePrice",
    "invShare",
    "dtfp_I",
    "dtfp_C",
    "du_invest",
    "du_consumption",
    "dtfp_I_util",
    "dtfp_C_util",
)
_TFP_SOURCE_VINTAGE = (
    r"Produced on October 01, 2026  2:43 PM by John Fernald/Leela Khatri--"
    r"fernaldjg@gmail.com (Directory: out\QuarterlyTFP_2026.10.01)"
)


def _excel_bytes(build: Any) -> bytes:
    workbook = Workbook()
    build(workbook)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _inflation_book(*, malformed_header: bool = False) -> bytes:
    def build(workbook: Workbook) -> None:
        initial = workbook.active
        workbook.remove(initial)
        expected = workbook.create_sheet("INFLATION")
        expected_headers = ["YEAR", "QUARTER", "INFPGDP1YR", "INFCPI1YR", "INFCPI10YR"]
        if malformed_header:
            expected_headers[2] = "INFPGDP2YR"
        expected.append(expected_headers)
        expected.append([1970, 1, "#N/A", "#N/A", "#N/A"])
        expected.append([1970, 2, 2.9851, "#N/A", "#N/A"])
        expected.append([2024, 2, 2.4, 2.5, 2.2])

    return _excel_bytes(build)


def _tfp_book(
    *,
    malformed_header: bool = False,
    vintage_line: str = "Produced on October 01 2026",
) -> bytes:
    def build(workbook: Workbook) -> None:
        readme = workbook.active
        readme.title = "readme"
        readme.append([vintage_line])
        readme.append(["All variables are percent change at an annual rate"])
        quarterly = workbook.create_sheet("quarterly")
        quarterly.append(["All variables are percent change at an annual rate"])
        headers = list(_TFP_COLUMNS)
        if malformed_header:
            headers[1] = "dY_wrong"
        quarterly.append(headers)
        quarterly.append(["2024:Q1", *[float(index) for index in range(1, 22)]])
        quarterly.append(["2024:Q2", *[float(index) for index in range(2, 23)]])

    return _excel_bytes(build)


def _h15_csv(*, malformed_header: bool = False) -> bytes:
    header = list(_H15_COLUMNS)
    if malformed_header:
        header[1] = "RIFLGFCM02_N.B"
    rows = [
        "Series Description,Market yield on U.S. Treasury securities",
        "Unit:,Percent:_Per_Year",
        "Multiplier:,1",
        "Currency:,NA",
        "Unique Identifier:,H15/H15/RIFLGFCM01_N.B",
        ",".join(header),
        "2024-06-24,5.40,5.45,5.35,5.12,4.60,4.42,4.20,4.18,4.20,4.40,4.30",
        "2024-06-25,5.43,5.49,5.37,5.10,4.65,4.45,4.25,4.23,4.23,4.46,4.36",
    ]
    return ("\r\n".join(rows) + "\r\n").encode()


def _domain_spec(domain: str, model_type: type[Any]) -> DomainSpec:
    model_name = {
        FederalReserveYieldCurveObservation: "YieldCurve",
        PrimaryDealerFails: "PrimaryDealerFails",
        PrimaryDealerPositioning: "PrimaryDealerPositioning",
        FederalReserveCentralBankHolding: "CentralBankHoldings",
        FederalReserveInflationExpectations: "InflationExpectations",
        FederalReserveTotalFactorProductivity: "TotalFactorProductivity",
        FederalReserveFomcDocument: "FomcDocuments",
    }[model_type]
    time_field, natural_key = {
        "YieldCurve": ("date", ("date", "maturity")),
        "PrimaryDealerFails": ("date", ("date", "symbol")),
        "PrimaryDealerPositioning": ("date", ("date", "symbol")),
        "CentralBankHoldings": ("date", ("date", "security_type", "cusip")),
        "InflationExpectations": ("date", ("date",)),
        "TotalFactorProductivity": ("period", ("period", "vintage_date")),
        "FomcDocuments": ("document_date", ("url",)),
    }[model_name]
    temporal = "document" if model_type is FederalReserveFomcDocument else "series"
    return DomainSpec(
        display_name=model_type.__name__,
        rest_path=f"federal-reserve/{domain.removeprefix('federal_reserve_').replace('_', '-')}",
        contract=model_type.__name__,
        temporal_kind=temporal,
        time_field=time_field,
        natural_key=natural_key,
        filter_dims=(),
        storage_mode="upsert",
        permissions=("query",),
    )


def _install_service_mocks(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    if model == "YieldCurve":
        monkeypatch.setattr(yield_curve, "get_bytes", lambda *_args: _h15_csv())
    elif model == "PrimaryDealerFails":
        monkeypatch.setattr(
            _primary_dealer, "get_json", _primary_dealer_responses("FAILS TO DELIVER")
        )
    elif model == "PrimaryDealerPositioning":
        monkeypatch.setattr(
            _primary_dealer, "get_json", _primary_dealer_responses("DEALER POSITION")
        )
    elif model == "CentralBankHoldings":
        monkeypatch.setattr(
            central_bank_holdings, "get_json", lambda *_args, **_kwargs: _soma_document()
        )
    elif model == "InflationExpectations":
        monkeypatch.setattr(inflation_expectations, "get_bytes", lambda *_args: _inflation_book())
    elif model == "TotalFactorProductivity":
        monkeypatch.setattr(
            total_factor_productivity,
            "get_bytes",
            lambda *_args: _tfp_book(vintage_line=_TFP_SOURCE_VINTAGE),
        )
    elif model == "FomcDocuments":
        monkeypatch.setattr(fomc_documents, "get_text", lambda *_args: _fomc_html())
    else:
        raise AssertionError(model)


_PD_FAILS_DESCRIPTION = (
    "TREASURY INFLATION-PROTECTED SECURITIES (TIPS): DEALER FINANCING FAILS TO DELIVER"
)
_PD_POSITION_DESCRIPTION = (
    "TOTAL U.S. TREASURY SECURITIES (EXCLUDING TIPS) DEALER POSITION LONG - SHORT"
)


def _primary_dealer_responses(description: str) -> Any:
    if "POSITION" in description:
        key_id = "PDPOSGST-TOT"
        source_description = _PD_POSITION_DESCRIPTION
        # The reviewed positioning contract publishes a whole USD-million position.
        value = "12500.00"
    else:
        key_id = "PDFTD-UST"
        source_description = _PD_FAILS_DESCRIPTION
        value = "12.50"

    def get_json(url: str, _timeout: float | None = None, **_kwargs) -> dict[str, object]:
        if url.endswith("/list/timeseries.json"):
            return {"pd": {"timeseries": [{"keyid": key_id, "description": source_description}]}}
        return {"pd": {"timeseries": [{"asofdate": "2024-06-26", "keyid": key_id, "value": value}]}}

    return get_json


def _soma_document() -> dict[str, object]:
    return {
        "soma": {
            "summary": [
                {
                    "asOfDate": "2024-06-26",
                    "mbs": "7000000000000.25",
                    "cmbs": "100000000000.00",
                    "tips": "300000000000.00",
                    "frn": "200000000000.00",
                    "tipsInflationCompensation": "10000000000.00",
                    "notesbonds": "4000000000000.00",
                    "bills": "200000000000.00",
                    "agencies": "100000000000.00",
                    "total": "12000000000000.25",
                }
            ]
        }
    }


def _soma_detail_document() -> dict[str, object]:
    return {
        "soma": {
            "holdings": [
                {
                    "asOfDate": "2024-06-26",
                    "cusip": "91282CHT3",
                    "maturityDate": "2024-09-26",
                    "issuer": "U.S. Treasury",
                    "spread": "",
                    "coupon": "5.250",
                    "parValue": "20489792900",
                    "inflationCompensation": "",
                    "percentOutstanding": "12.5",
                    "changeFromPriorWeek": "-100000",
                    "changeFromPriorYear": "250000",
                    "securityType": "Bills",
                }
            ]
        }
    }


def _soma_mortgage_document(*, cmbs: bool = False) -> dict[str, object]:
    row: dict[str, object] = {
        "asOfDate": "2024-06-26",
        "cusip": "31418DAB5",
        "securityDescription": "UMBS 30YR 2024-06",
        "term": "30-Year",
        "currentFaceValue": "62027851989.83",
        "securityType": "CMBS" if cmbs else "MBS",
    }
    if not cmbs:
        row["isAggregated"] = "Y"
    return {"soma": {"holdings": [row]}}


def _soma_wam_document(*, agency: bool = False) -> dict[str, object]:
    return {
        "soma": {
            "asOfDate": "2024-06-26",
            "wam": "3.70" if agency else "8.20",
            "securityTypes": ["Agency Debts"]
            if agency
            else ["Bills", "FRNs", "NotesBonds", "TIPS"],
        }
    }


def _fomc_html() -> str:
    return """<html><h5>June 11-12 Meeting - 2024</h5>
      <a href="/monetarypolicy/files/FOMC20240612statement.pdf">Statement</a></html>"""


_MODELS = {
    "YieldCurve": (FederalReserveYieldCurveObservation, "www.federalreserve.gov"),
    "PrimaryDealerFails": (
        PrimaryDealerFails,
        "markets.newyorkfed.org",
    ),
    "PrimaryDealerPositioning": (
        PrimaryDealerPositioning,
        "markets.newyorkfed.org",
    ),
    "CentralBankHoldings": (
        FederalReserveCentralBankHolding,
        "markets.newyorkfed.org",
    ),
    "InflationExpectations": (
        FederalReserveInflationExpectations,
        "www.philadelphiafed.org",
    ),
    "TotalFactorProductivity": (
        FederalReserveTotalFactorProductivity,
        "www.frbsf.org",
    ),
    "FomcDocuments": (FederalReserveFomcDocument, "www.federalreserve.gov"),
}


def test_integration_handoff_semantics_reference_typed_contract_fields() -> None:
    handoff_path = (
        Path(__file__).resolve().parents[1] / "docs/evidence/C100/federal-reserve/integration.json"
    )
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    contracts_by_name = {model.__name__: model for model, _host in _MODELS.values()}
    query_only_fields = {
        "end_date",
        "end_period",
        "key_ids",
        "maturities",
        "start_date",
        "start_period",
        "year",
    }

    assert set(handoff["domains"]) == {
        fetcher.capability.domain
        for fetcher in PROVIDER.fetchers
        if fetcher.canonical_model in _MODELS
    }
    for domain, entry in handoff["domains"].items():
        spec = DomainSpec.model_validate(entry)
        name = spec.contract
        if name not in contracts_by_name:
            # The C100 handoff names the class the leg emitted when the handoff was written; wave
            # 28 retargeted the H.15 leg to the class domains.yaml declares. Resolve a stale name
            # through the shipped registry so a real drift still reddens the field check below.
            shipped = contract_model(domain)
            assert shipped.__name__ in contracts_by_name, domain
            name = shipped.__name__
        contract = contracts_by_name[name]
        contract_fields = set(contract.model_fields)
        semantic_fields = (
            (() if spec.time_field is None else (spec.time_field,))
            + spec.natural_key
            + spec.filter_dims
        )
        assert set(semantic_fields) <= contract_fields
        assert not (set(spec.filter_dims) & query_only_fields)


def test_declared_row_models_are_their_domain_contracts() -> None:
    """Guard the face the governed service actually revalidates rows against.

    ``provider_model_query._serialize_contract_row`` compares every row with
    ``contract_model(domain)`` rather than with the fetcher's own class, so a bound fetcher that
    builds a look-alike contract passes its direct leg and refuses on the service leg. The
    primary-dealer rows did exactly that, and the fake ``DomainSpec`` injected by the sibling
    test below could not see it.
    """
    declaring = [
        fetcher
        for fetcher in PROVIDER.fetchers
        if getattr(type(fetcher), "row_model", None) is not None
    ]
    assert declaring, "no bound federal_reserve fetcher declares a row model"
    for fetcher in declaring:
        contract_type = contract_model(fetcher.capability.domain)
        assert type(fetcher).row_model is contract_type, (
            f"{fetcher.canonical_model} builds "
            f"{type(fetcher).row_model.__module__}.{type(fetcher).row_model.__name__} while "
            f"{fetcher.capability.domain} declares {contract_type.__name__}"
        )


@pytest.mark.parametrize("model", tuple(_MODELS))
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, host = _MODELS[model]
    registry = ProviderRegistry()
    register_provider("federal_reserve", registry)
    domain = next(
        fetcher.capability.domain
        for fetcher in PROVIDER.fetchers
        if fetcher.canonical_model == model
    )
    semantics = _domain_spec(domain, contract)
    monkeypatch.setattr("opendata.data.domains.require_domain_semantics", lambda _domain: semantics)
    monkeypatch.setattr("opendata.data.domains.contract_model", lambda _domain: contract)
    _install_service_mocks(monkeypatch, model)
    fetcher = registry.resolve_model("federal_reserve", model)
    transform_query = fetcher.transform_query
    injected_identity: list[tuple[str, str]] = []

    def record_injected_identity(**kwargs: object) -> object:
        parsed = transform_query(**kwargs)
        injected_identity.append((parsed.source, parsed.market))
        return parsed

    monkeypatch.setattr(fetcher, "transform_query", record_injected_identity)

    grant = RequestGrant(
        source="federal_reserve",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="official public source fixture",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=(host,),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    context = FetchContext(
        timeout=5,
        request_budget=RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,)),
        operation=RequestOperation.QUERY,
    )
    queries: dict[str, dict[str, object]] = {
        "YieldCurve": {"start_date": "2024-06-25", "maturities": ["10Y"]},
        "PrimaryDealerFails": {"key_ids": ["PDFTD-UST"], "start_date": "2024-06-26"},
        "PrimaryDealerPositioning": {
            "key_ids": ["PDPOSGST-TOT"],
            "start_date": "2024-06-26",
        },
        "CentralBankHoldings": {"date": "2024-06-26", "summary": True},
        "InflationExpectations": {
            "start_date": "2024-04-01",
            "end_date": "2024-06-30",
        },
        "TotalFactorProductivity": {"start_period": "2024:Q1", "end_period": "2024:Q1"},
        "FomcDocuments": {"year": 2024},
    }
    principal = SimpleNamespace(allows_domain=lambda _domain: True)
    result = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="federal_reserve",
            model=model,
            query=queries[model],
            principal=principal,
            ctx=context,
        )
    )

    assert result["model"] == model
    assert injected_identity == [("federal_reserve", "us")]
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert json.loads(json.dumps(row)) == row
    assert "source" not in row and "market" not in row
    if model == "YieldCurve":
        assert row == {"date": "2024-06-25", "maturity": "10Y", "rate": 0.0423}
    elif model == "PrimaryDealerFails":
        assert row == {
            "date": "2024-06-26",
            "symbol": "PDFTD-UST",
            "title": _PD_FAILS_DESCRIPTION,
            "value": 12.5,
        }
    elif model == "PrimaryDealerPositioning":
        assert row == {
            "date": "2024-06-26",
            "symbol": "PDPOSGST-TOT",
            "name": "PDPOSGST-TOT",
            "title": _PD_POSITION_DESCRIPTION,
            "value": 12500,
        }
    elif model == "CentralBankHoldings":
        assert row["date"] == "2024-06-26" and row["total"] == "12000000000.00025"
    elif model == "InflationExpectations":
        assert row == {
            "date": "2024-04-01",
            "infpgdp1yr": 2.4,
            "infcpi1yr": 2.5,
            "infcpi10yr": 2.2,
        }
    elif model == "TotalFactorProductivity":
        assert row["period"] == "2024:Q1" and row["vintage_date"] == "2026-10-01"
    else:
        assert row["document_type"] == "statement" and row["format"] == "pdf"


def test_tfp_source_vintage_survives_service_identity_injection_and_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "TotalFactorProductivity"
    contract = FederalReserveTotalFactorProductivity
    fetcher = total_factor_productivity.FederalReserveTotalFactorProductivityFetcher()
    registry = ProviderRegistry()
    registry.register_provider_models("federal_reserve", (model,), fetcher)
    semantics = _domain_spec(fetcher.capability.domain, contract)
    monkeypatch.setattr("opendata.data.domains.require_domain_semantics", lambda _domain: semantics)
    monkeypatch.setattr("opendata.data.domains.contract_model", lambda _domain: contract)
    _install_service_mocks(monkeypatch, model)
    transform_query = fetcher.transform_query
    injected_identity: list[tuple[str, str]] = []

    def record_injected_identity(**kwargs: object) -> object:
        parsed = transform_query(**kwargs)
        injected_identity.append((parsed.source, parsed.market))
        return parsed

    monkeypatch.setattr(fetcher, "transform_query", record_injected_identity)
    grant = RequestGrant(
        source="federal_reserve",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="official SF Fed workbook shape fixture",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=("www.frbsf.org",),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    context = FetchContext(
        timeout=5,
        request_budget=RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,)),
        operation=RequestOperation.QUERY,
    )
    principal = SimpleNamespace(allows_domain=lambda _domain: True)

    result = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="federal_reserve",
            model=model,
            query={"start_period": "2024:Q1", "end_period": "2024:Q1"},
            principal=principal,
            ctx=context,
        )
    )

    assert injected_identity == [("federal_reserve", "us")]
    row = result["results"][0]
    assert json.loads(json.dumps(row)) == row
    assert row["period"] == "2024:Q1" and row["vintage_date"] == "2026-10-01"
    assert "source" not in row and "market" not in row


@pytest.mark.parametrize("parser", ["yield", "inflation", "tfp"])
def test_workbook_and_csv_contracts_refuse_official_shape_drift(parser: str) -> None:
    if parser == "yield":
        fetcher = yield_curve.FederalReserveYieldCurveFetcher()
        query = fetcher.transform_query()
        payload = _h15_csv(malformed_header=True)
    elif parser == "inflation":
        fetcher = inflation_expectations.FederalReserveInflationExpectationsFetcher()
        query = fetcher.transform_query()
        payload = _inflation_book(malformed_header=True)
    else:
        fetcher = total_factor_productivity.FederalReserveTotalFactorProductivityFetcher()
        query = fetcher.transform_query()
        payload = _tfp_book(malformed_header=True)
    with pytest.raises(ValueError):
        fetcher.transform_data(payload, query)


def test_spf_inflation_preserves_quarter_boundary_and_published_na_cells() -> None:
    fetcher = inflation_expectations.FederalReserveInflationExpectationsFetcher()
    query = fetcher.transform_query(start_date="1970-04-01", end_date="1970-04-01")
    rows = fetcher.transform_data(_inflation_book(), query)
    assert len(rows) == 1
    assert rows[0].model_dump(mode="json") == {
        "date": "1970-04-01",
        "infpgdp1yr": 2.9851,
        "infcpi1yr": None,
        "infcpi10yr": None,
    }


@pytest.mark.parametrize(
    "vintage_line",
    ("Produced on October 01 2026", _TFP_SOURCE_VINTAGE),
    ids=("date-only-compatibility", "official-date-time-author-directory-shape"),
)
def test_tfp_vintage_parses_supported_date_forms(vintage_line: str) -> None:
    fetcher = total_factor_productivity.FederalReserveTotalFactorProductivityFetcher()
    query = fetcher.transform_query(start_period="2024:Q1", end_period="2024:Q1")
    rows = fetcher.transform_data(_tfp_book(vintage_line=vintage_line), query)

    assert len(rows) == 1
    assert rows[0].period == "2024:Q1"
    assert rows[0].vintage_date.isoformat() == "2026-10-01"


@pytest.mark.parametrize(
    "vintage_line",
    (
        "Produced on February 30, 2026  2:43 PM by John Fernald (Directory: out\\tfp)",
        "Produced on October 01, 2026  13:43 PM by John Fernald (Directory: out\\tfp)",
        "Produced on October 01, 2026  2:43 PM by John Fernald",
        "Produced on October 01, 2026  2:43 PM by John Fernald (Directory:   )",
    ),
    ids=("invalid-date", "invalid-12-hour-time", "missing-directory", "empty-directory"),
)
def test_tfp_vintage_rejects_invalid_date_time_or_line_grammar(vintage_line: str) -> None:
    fetcher = total_factor_productivity.FederalReserveTotalFactorProductivityFetcher()
    query = fetcher.transform_query()

    with pytest.raises(FederalReserveSourceError) as exc_info:
        fetcher.transform_data(_tfp_book(vintage_line=vintage_line), query)

    assert exc_info.value.code == "FEDERAL_RESERVE_BAD_TFP_VINTAGE"


@pytest.mark.parametrize(
    "model",
    (
        "PrimaryDealerFails",
        "PrimaryDealerPositioning",
        "CentralBankHoldings",
        "FomcDocuments",
    ),
)
def test_json_and_html_models_refuse_malformed_official_shape(model: str) -> None:
    if model in {"PrimaryDealerFails", "PrimaryDealerPositioning"}:
        query = _primary_dealer.PrimaryDealerQuery(
            key_ids=("PDFTD-UST",), source="federal_reserve", market="us"
        )
        with pytest.raises(ValueError):
            _primary_dealer.normalize_primary_dealer_records(
                ("not a typed source record",),
                query,
                category="fails" if model.endswith("Fails") else "position",
            )  # type: ignore[arg-type]
    elif model == "CentralBankHoldings":
        fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
        query = fetcher.transform_query()
        with pytest.raises(ValueError):
            fetcher.transform_data({"soma": {"summary": [{"asOfDate": "2024-06-26"}]}}, query)
    else:
        fetcher = fomc_documents.FederalReserveFomcDocumentsFetcher()
        query = fetcher.transform_query(year=2024)
        with pytest.raises(ValueError):
            fetcher.transform_data(
                '<a href="/monetarypolicy/files/FOMC20240612statement.pdf"></a>', query
            )


def test_soma_detail_selector_preserves_security_fields_and_thousands_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    query = fetcher.transform_query(date="2024-06-26", holding_type="bills")
    requested: list[str] = []

    def get_json(url: str, _timeout: float | None = None, **_kwargs) -> dict[str, object]:
        requested.append(url)
        return _soma_detail_document()

    monkeypatch.setattr(central_bank_holdings, "get_json", get_json)
    raw = fetcher.extract_data(query, FetchContext(timeout=5))
    rows = fetcher.transform_data(raw, query)

    assert requested == [
        "https://markets.newyorkfed.org/api/soma/tsy/get/bills/asof/2024-06-26.json"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.date.isoformat() == "2024-06-26"
    assert row.security_type == "Bills" and row.cusip == "91282CHT3"
    assert row.par_value == Decimal("20489792.9")
    assert row.change_prior_week == Decimal("-100")
    assert row.coupon == Decimal("5.250")
    assert row.percent_outstanding == Decimal("12.5")


@pytest.mark.parametrize(
    ("holding_type", "cmbs", "aggregated"),
    [("mbs", False, "Y"), ("cmbs", True, None)],
)
def test_soma_mortgage_detail_shape_preserves_optional_source_fields(
    holding_type: str, cmbs: bool, aggregated: str | None
) -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    query = fetcher.transform_query(date="2024-06-26", holding_type=holding_type)
    rows = fetcher.transform_data(_soma_mortgage_document(cmbs=cmbs), query)
    assert len(rows) == 1
    row = rows[0]
    assert row.security_type == ("CMBS" if cmbs else "MBS")
    assert row.description == "UMBS 30YR 2024-06"
    assert row.term == "30-Year"
    assert row.face_value == Decimal("62027851.98983")
    assert row.is_aggregated == aggregated


def test_soma_wam_selector_is_grouped_and_emits_documented_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    query = fetcher.transform_query(
        date="2024-06-26",
        holding_type="agency_debts",
        cusip="91282CHT3",
        monthly=True,
        wam=True,
    )
    requested: list[str] = []

    def get_json(url: str, _timeout: float | None = None, **_kwargs) -> dict[str, object]:
        requested.append(url)
        return _soma_wam_document(
            agency=url.startswith("https://markets.newyorkfed.org/api/soma/agency/")
        )

    monkeypatch.setattr(central_bank_holdings, "get_json", get_json)
    raw = fetcher.extract_data(query, FetchContext(timeout=5))
    rows = fetcher.transform_data(raw, query)

    assert requested == [
        "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2024-06-26.json",
        "https://markets.newyorkfed.org/api/soma/agency/wam/agency%20debts/asof/2024-06-26.json",
    ]
    assert [row.model_dump(mode="json", exclude_none=True) for row in rows] == [
        {
            "date": "2024-06-26",
            "security_type": "Agency",
            "wam": 3.7,
            "security_types": ["Agency Debts"],
        },
        {
            "date": "2024-06-26",
            "security_type": "Treasury",
            "wam": 8.2,
            "security_types": ["Bills", "FRNs", "NotesBonds", "TIPS"],
        },
    ]


def test_soma_wam_parser_refuses_wrong_source_security_group() -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    query = fetcher.transform_query(date="2024-06-26", holding_type="agency_debts", wam=True)
    malformed = {
        "soma": {
            "asOfDate": "2024-06-26",
            "wam": "3.70",
            "securityTypes": ["MBS"],
        }
    }
    with pytest.raises(ValueError, match="FEDERAL_RESERVE_BAD_WAM_ROW"):
        fetcher.transform_data((_soma_wam_document(), malformed), query)


@pytest.mark.parametrize(
    ("query", "expected_url"),
    [
        (
            {"date": "2024-06-26", "summary": True, "holding_type": "mbs"},
            "https://markets.newyorkfed.org/api/soma/summary.json",
        ),
        (
            {"date": "2024-06-26", "holding_type": "mbs"},
            "https://markets.newyorkfed.org/api/soma/agency/get/mbs/asof/2024-06-26.json",
        ),
        (
            {"date": "2024-06-26", "cusip": "91282CHT3"},
            "https://markets.newyorkfed.org/api/soma/tsy/get/cusip/91282CHT3.json",
        ),
        (
            {"monthly": True},
            "https://markets.newyorkfed.org/api/soma/tsy/get/monthly.json",
        ),
    ],
)
def test_soma_selector_routes_follow_documented_priority(
    query: dict[str, object], expected_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    parsed = fetcher.transform_query(**query)
    requested: list[str] = []
    monkeypatch.setattr(
        central_bank_holdings,
        "get_json",
        lambda url, _timeout=None, **_kwargs: requested.append(url) or {},
    )
    fetcher.extract_data(parsed, FetchContext(timeout=5))
    assert requested == [expected_url]


def test_soma_monthly_selector_rejects_non_treasury_category() -> None:
    fetcher = central_bank_holdings.FederalReserveCentralBankHoldingsFetcher()
    with pytest.raises(ValueError, match="monthly is only supported"):
        fetcher.transform_query(holding_type="mbs", monthly=True)


def test_primary_dealer_fixtures_match_current_official_catalogue_ids() -> None:
    list_url = "https://markets.newyorkfed.org/api/pd/list/timeseries.json"
    assert _primary_dealer_responses("DEALER POSITION")(list_url, 5)["pd"]["timeseries"] == [
        {
            "keyid": "PDPOSGST-TOT",
            "description": (
                "TOTAL U.S. TREASURY SECURITIES (EXCLUDING TIPS) DEALER POSITION LONG - SHORT"
            ),
        }
    ]
    assert _primary_dealer_responses("FAILS TO DELIVER")(list_url, 5)["pd"]["timeseries"] == [
        {
            "keyid": "PDFTD-UST",
            "description": (
                "TREASURY INFLATION-PROTECTED SECURITIES (TIPS): DEALER FINANCING FAILS TO DELIVER"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("category", "key_id"),
    [("fails", "PDFTD-UST"), ("position", "PDPOSGST-TOT")],
)
def test_primary_dealer_api_refuses_malformed_catalog_shape(
    category: str, key_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _primary_dealer,
        "get_json",
        lambda *_args, **_kwargs: {"pd": {"timeseries": {"wrong": "container"}}},
    )
    with pytest.raises(ValueError, match="FEDERAL_RESERVE_BAD_PD_DEFINITIONS"):
        _primary_dealer.fetch_primary_dealer_series(
            (key_id,),
            category=category,
            timeout=5,  # type: ignore[arg-type]
        )
