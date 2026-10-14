"""Offline contract tests for the clean New York Fed fetchers."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.federal_reserve_clean_nyfed import (
    SOFR,
    PrimaryDealerFails,
    PrimaryDealerPositioning,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import ProviderEngineQueryError
from opendata.data.providers.federal_reserve.models import clean_nyfed
from opendata.data.providers.federal_reserve.models.clean_nyfed import (
    NyfedProviderError,
    PrimaryDealerFailsFetcher,
    PrimaryDealerPositioningFetcher,
    SOFRFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class _Response:
    def __init__(self, document: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self._content = json.dumps(document).encode("utf-8")
        self.encoding = "utf-8"
        self.url = "https://markets.newyorkfed.org/api/test"

    def json(self) -> object:
        """Expose the offline document through the requests response API."""
        return json.loads(self._content)


class _Session:
    def __init__(self, documents: list[object]) -> None:
        self.documents = list(documents)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return _Response(self.documents.pop(0))


def _context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="federal_reserve",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:synthetic-response",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=frozenset({"markets.newyorkfed.org"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=5,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
        ),
    )


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    documents: list[object],
) -> _Session:
    session = _Session(documents)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(clean_nyfed, "get_shared_http_client", lambda: client)
    return session


def _dealer_document(*rows: tuple[str, str, str]) -> dict[str, object]:
    return {
        "pd": {
            "timeseries": [
                {"asofdate": day, "keyid": key_id, "value": value} for day, key_id, value in rows
            ]
        }
    }


def _nyfed_capture(filename: str) -> object:
    """Load a byte-preserved official NY Fed response used by offline tests."""
    path = (
        Path(__file__).resolve().parents[1]
        / "docs/evidence/C100/federal-reserve-clean-nyfed/fixtures"
        / filename
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_sofr_joins_two_official_endpoints_and_preserves_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_session(
        monkeypatch,
        [
            {
                "refRates": [
                    {
                        "type": "SOFR",
                        "effectiveDate": "2024-01-02",
                        "percent": 5.31,
                        "percentPercentile1": 5.20,
                        "percentPercentile25": 5.30,
                        "percentPercentile75": 5.32,
                        "percentPercentile99": 5.40,
                        "volumeInBillions": 1_234.5,
                    }
                ]
            },
            {
                "refRates": [
                    {
                        "type": "SOFRAI",
                        "effectiveDate": "2024-01-02",
                        "average30day": 5.29,
                        "average90day": 5.27,
                        "average180day": 5.25,
                        "index": 1.04,
                    }
                ]
            },
        ],
    )

    rows = SOFRFetcher().fetch(
        ctx=_context("SOFR", attempts=2),
        start_date="2024-01-01",
        end_date="2024-01-31",
    )

    assert len(rows) == 1
    assert isinstance(rows[0], SOFR)
    assert rows[0].date == date(2024, 1, 2)
    assert rows[0].rate == 5.31
    assert rows[0].volume == 1_234.5
    assert rows[0].average_30d == 5.29
    assert rows[0].index == 1.04
    assert [call[1] for call in session.calls] == [
        "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json",
        "https://markets.newyorkfed.org/api/rates/secured/sofrai/search.json",
    ]
    assert session.calls[0][2]["params"] == {
        "startDate": "2024-01-01",
        "endDate": "2024-01-31",
    }


def test_sofr_keeps_early_rate_rows_without_published_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            {"refRates": [{"type": "SOFR", "effectiveDate": "2018-04-02", "percent": 1.8}]},
            {"refRates": []},
        ],
    )

    rows = SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    assert len(rows) == 1
    assert rows[0].date == date(2018, 4, 2)
    assert rows[0].average_30d is None
    assert rows[0].average_90d is None
    assert rows[0].average_180d is None
    assert rows[0].index is None


def test_sofr_replays_official_percent_rate_capture_and_maps_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the captured live-service key without changing public values."""
    session = _install_session(
        monkeypatch,
        [_nyfed_capture("sofr-2020-01-02-03.json"), {"refRates": []}],
    )

    rows = SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    assert [(row.date, row.rate) for row in rows] == [
        (date(2020, 1, 2), 1.54),
        (date(2020, 1, 3), 1.55),
    ]
    assert all(row.average_30d is None and row.index is None for row in rows)
    assert len(session.calls) == 2


def test_sofr_joins_captured_2025_sofrai_rows_by_effective_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Join captured positive enrichment rows without synthesizing averages."""
    _install_session(
        monkeypatch,
        [
            _nyfed_capture("sofr-2025-06-02-03.json"),
            _nyfed_capture("sofrai-2025-06-02-03.json"),
        ],
    )

    rows = SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    assert [
        (row.date, row.rate, row.average_30d, row.average_90d, row.average_180d, row.index)
        for row in rows
    ] == [
        (date(2025, 6, 2), 4.35, 4.30785, 4.34693, 4.40296, 1.19661783),
        (date(2025, 6, 3), 4.32, 4.30753, 4.34716, 4.40159, 1.19676243),
    ]


def test_sofr_accepts_equal_documented_and_captured_rate_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            {
                "refRates": [
                    {
                        "type": "SOFR",
                        "effectiveDate": "2024-01-02",
                        "percent": 5.0,
                        "percentRate": 5,
                    }
                ]
            },
            {"refRates": []},
        ],
    )

    rows = SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    assert [(row.date, row.rate) for row in rows] == [(date(2024, 1, 2), 5.0)]


@pytest.mark.parametrize(
    "rate_fields",
    (
        {"percent": 5.0, "percentRate": 5.1},
        {"percent": 5.0, "percentRate": "5.0"},
        {"percent": 5.0, "percentRate": True},
        {},
    ),
)
def test_sofr_rejects_conflicting_missing_or_non_numeric_rate_aliases_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
    rate_fields: dict[str, object],
) -> None:
    session = _install_session(
        monkeypatch,
        [
            {"refRates": [{"type": "SOFR", "effectiveDate": "2024-01-02", **rate_fields}]},
            {"refRates": []},
        ],
    )

    with pytest.raises(NyfedProviderError, match="NYFED_BAD_SHAPE"):
        SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    assert len(session.calls) == 1


def test_sofr_rejects_conflicting_duplicate_and_boolean_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            {
                "refRates": [
                    {"type": "SOFR", "effectiveDate": "2024-01-02", "percent": 5.0},
                    {"type": "SOFR", "effectiveDate": "2024-01-02", "percent": 5.1},
                ]
            },
            {"refRates": []},
        ],
    )
    with pytest.raises(NyfedProviderError, match="NYFED_DUPLICATE_DATE_CONFLICT"):
        SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))

    _install_session(
        monkeypatch,
        [
            {"refRates": [{"type": "SOFR", "effectiveDate": "2024-01-02", "percent": True}]},
            {"refRates": []},
        ],
    )
    with pytest.raises(NyfedProviderError, match="NYFED_BAD_SHAPE"):
        SOFRFetcher().fetch(ctx=_context("SOFR", attempts=2))


def test_sofr_query_rejects_fred_only_parameters_and_reversed_dates() -> None:
    fetcher = SOFRFetcher()
    with pytest.raises(ValidationError):
        fetcher.transform_query(frequency="monthly")
    with pytest.raises(ValidationError):
        fetcher.transform_query(start_date="2024-02-01", end_date="2024-01-31")


@pytest.mark.parametrize(
    ("asset_class", "expected_keys"),
    (
        (
            "all",
            (
                "PDFTR-USTET",
                "PDFTR-UST",
                "PDFTR-FGEM",
                "PDFTR-FGM",
                "PDFTR-OM",
                "PDFTR-CS",
                "PDFTD-USTET",
                "PDFTD-UST",
                "PDFTD-FGEM",
                "PDFTD-FGM",
                "PDFTD-OM",
                "PDFTD-CS",
            ),
        ),
        ("treasuries", ("PDFTR-USTET", "PDFTD-USTET")),
        ("tips", ("PDFTR-UST", "PDFTD-UST")),
        ("agency", ("PDFTR-FGEM", "PDFTD-FGEM")),
        ("mbs", ("PDFTR-FGM", "PDFTD-FGM", "PDFTR-OM", "PDFTD-OM")),
        ("corporate", ("PDFTR-CS", "PDFTD-CS")),
    ),
)
def test_fails_uses_exact_asset_key_allowlist_and_keeps_absent_keys_absent(
    monkeypatch: pytest.MonkeyPatch,
    asset_class: str,
    expected_keys: tuple[str, ...],
) -> None:
    session = _install_session(monkeypatch, [_dealer_document()])
    rows = PrimaryDealerFailsFetcher().fetch(
        ctx=_context("PrimaryDealerFails"),
        asset_class=asset_class,
    )

    assert rows == []
    assert session.calls[0][1].endswith(f"/pd/get/{'_'.join(expected_keys)}.json")


def test_fails_preserves_two_mbs_categories_and_source_millions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            _dealer_document(
                ("2024-01-03", "PDFTR-FGM", "10"),
                ("2024-01-03", "PDFTD-OM", "12.5"),
            )
        ],
    )

    rows = PrimaryDealerFailsFetcher().fetch(
        ctx=_context("PrimaryDealerFails"),
        asset_class="mbs",
        unit="value",
    )

    assert [(row.symbol, row.value) for row in rows] == [
        ("PDFTD-OM", 12.5),
        ("PDFTR-FGM", 10),
    ]
    assert all(isinstance(row, PrimaryDealerFails) for row in rows)
    assert "FAILS TO RECEIVE" in rows[1].title
    assert "FAILS TO DELIVER" in rows[0].title


def test_fails_percent_selector_is_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_session(monkeypatch, [])
    with pytest.raises(ProviderEngineQueryError) as exc_info:
        PrimaryDealerFailsFetcher().fetch(
            ctx=_context("PrimaryDealerFails"),
            unit="percent",
        )

    assert exc_info.value.code == "NYFED_UNSUPPORTED_FAILS_UNIT"
    assert session.calls == []


@pytest.mark.parametrize(
    ("category", "expected_keys"),
    (
        ("treasuries", ("PDPOSGST-TOT",)),
        ("bills", ("PDPOSGS-B",)),
        (
            "coupons",
            (
                "PDPOSGSC-L2",
                "PDPOSGSC-G2L3",
                "PDPOSGSC-G3L6",
                "PDPOSGSC-G6L7",
                "PDPOSGSC-G7L11",
                "PDPOSGSC-G11L21",
                "PDPOSGSC-G21",
            ),
        ),
        ("notes", ("PDPOSGS-BFRN",)),
        (
            "tips",
            ("PDPOSTIPS-L2", "PDPOSTIPS-G2", "PDPOSTIPS-G6L11", "PDPOSTIPS-G11"),
        ),
        (
            "mbs",
            (
                "PDPOSMBS-TOT",
                "PDPOSMBSFGS-TBA",
                "PDPOSMBSFGS-ST",
                "PDPOSMBSFGS-OR",
                "PDPOSMBSFGS-C",
                "PDPOSMBSNA-R",
                "PDPOSMBSNA-O",
            ),
        ),
        ("cmbs", ("PDPOSMBSFGS-C", "PDPOSMBSNA-O")),
        (
            "municipal",
            (
                "PDPOSSMGO-TOT",
                "PDPOSSMGO-L13",
                "PDPOSSMGO-G13",
                "PDPOSSMGO-G5L10",
                "PDPOSSMGO-G10",
                "PDPOSSMGO-VRD",
            ),
        ),
        (
            "corporate",
            (
                "PDPOSCS-TOT",
                "PDPOSCSBND-L13",
                "PDPOSCSBND-G13",
                "PDPOSCSBND-G5L10",
                "PDPOSCSBND-G10",
                "PDPOSCSBND-BELL13",
                "PDPOSCSBND-BELG13",
                "PDPOSCSBND-BELG5L10",
                "PDPOSCSBND-BELG10",
                "PDPOSCSCP",
            ),
        ),
        ("commercial_paper", ("PDPOSCSCP",)),
        (
            "corporate_ig",
            (
                "PDPOSCSBND-L13",
                "PDPOSCSBND-G13",
                "PDPOSCSBND-G5L10",
                "PDPOSCSBND-G10",
            ),
        ),
        (
            "corporate_junk",
            (
                "PDPOSCSBND-BELL13",
                "PDPOSCSBND-BELG13",
                "PDPOSCSBND-BELG5L10",
                "PDPOSCSBND-BELG10",
            ),
        ),
        (
            "abs",
            (
                "PDPOSABS-TOT",
                "PDPOSABS-ALB",
                "PDPOSABS-CCB",
                "PDPOSABS-SLB",
                "PDPOSABS-OAB",
            ),
        ),
    ),
)
def test_positioning_category_maps_to_exact_documented_keyids(
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    expected_keys: tuple[str, ...],
) -> None:
    session = _install_session(monkeypatch, [_dealer_document()])
    assert (
        PrimaryDealerPositioningFetcher().fetch(
            ctx=_context("PrimaryDealerPositioning"),
            category=category,
        )
        == []
    )
    assert session.calls[0][1].endswith(f"/pd/get/{'_'.join(expected_keys)}.json")


def test_positioning_keeps_native_integer_and_derives_required_name_from_keyid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [_dealer_document(("2024-01-03", "PDPOSGST-TOT", "-123456"))],
    )

    rows = PrimaryDealerPositioningFetcher().fetch(
        ctx=_context("PrimaryDealerPositioning"),
    )

    assert rows == [
        PrimaryDealerPositioning(
            date=date(2024, 1, 3),
            symbol="PDPOSGST-TOT",
            value=-123456,
            name="PDPOSGST-TOT",
            title=clean_nyfed._POSITIONING_DESCRIPTIONS["PDPOSGST-TOT"],
        )
    ]


def test_dealer_transform_rejects_bad_rows_and_does_not_zero_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [_dealer_document(("2024-01-03", "PDPOSGST-TOT", "NaN"))],
    )
    with pytest.raises(NyfedProviderError, match="NYFED_BAD_NUMERIC_VALUE"):
        PrimaryDealerPositioningFetcher().fetch(ctx=_context("PrimaryDealerPositioning"))

    _install_session(
        monkeypatch,
        [_dealer_document()],
    )
    rows = PrimaryDealerPositioningFetcher().fetch(
        ctx=_context("PrimaryDealerPositioning"),
        category="treasuries",
    )
    assert rows == []

    _install_session(
        monkeypatch,
        [_dealer_document(("2024-01-03", "PDPOSGS-B", "5"))],
    )
    with pytest.raises(NyfedProviderError, match="NYFED_UNEXPECTED_KEYID"):
        PrimaryDealerPositioningFetcher().fetch(
            ctx=_context("PrimaryDealerPositioning"),
            category="treasuries",
        )


@pytest.mark.parametrize(
    ("fetcher", "model", "contract", "documents", "query", "identity"),
    (
        (
            SOFRFetcher(),
            "SOFR",
            SOFR,
            [
                {"refRates": [{"type": "SOFR", "effectiveDate": "2024-01-02", "percent": 5.0}]},
                {"refRates": []},
            ],
            {},
            {"date": "2024-01-02", "rate": 5.0},
        ),
        (
            PrimaryDealerFailsFetcher(),
            "PrimaryDealerFails",
            PrimaryDealerFails,
            [_dealer_document(("2024-01-03", "PDFTR-USTET", "10"))],
            {"asset_class": "treasuries"},
            {"symbol": "PDFTR-USTET", "value": 10},
        ),
        (
            PrimaryDealerPositioningFetcher(),
            "PrimaryDealerPositioning",
            PrimaryDealerPositioning,
            [_dealer_document(("2024-01-03", "PDPOSGST-TOT", "-10"))],
            {},
            {"symbol": "PDPOSGST-TOT", "value": -10},
        ),
    ),
)
def test_provider_model_query_injects_source_market_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Any,
    model: str,
    contract: type[Any],
    documents: list[object],
    query: dict[str, object],
    identity: dict[str, object],
) -> None:
    _install_session(monkeypatch, documents)
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="federal_reserve",
        model=model,
        domain=capability.domain,
        verified=False,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )

    observed: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def capture_transform_query(**kwargs: object) -> object:
        observed.append((str(kwargs["source"]), kwargs["market"]))
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_transform_query)

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="federal_reserve",
            model=model,
            query=query,
            principal=Principal(),
            ctx=_context(model, attempts=2),
        )
    )

    assert observed == [("federal_reserve", "us")]
    assert response["source"] == "federal_reserve"
    assert response["model"] == model
    assert response["domain"] == capability.domain
    assert response["verified"] is False
    serialized_row = response["results"][0]
    assert set(serialized_row) == set(contract.model_fields)
    assert isinstance(serialized_row["date"], str)
    assert serialized_row["date"] == ("2024-01-02" if model == "SOFR" else "2024-01-03")
    assert all(serialized_row[key] == value for key, value in identity.items())
