"""Offline source-shape, budget, and typed-output tests for FRED SLOOS."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_sloos import (
    FredSeniorLoanOfficerSurvey,
    FredSeniorLoanOfficerSurveyQuery,
    SloosCategory,
    SloosFREDTransform,
    SloosSelectionScope,
    SloosTransform,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.sloos import (
    FredSeniorLoanOfficerSurveyFetcher,
    FredSloosQueryLimitError,
    _fred_transform,
)
from opendata.data.providers.fred.sloos_series import (
    SLOOS_CATEGORY_ORDER,
    SLOOS_SERIES,
    SloosSeriesSpec,
    series_for_category,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    reserve_scoped_attempt,
)

ROOT = Path(__file__).resolve().parents[1]
MAPPING_CSV = ROOT / "docs/evidence/C100/fred-sloos/recommended-candidates.csv"


@pytest.fixture(autouse=True)
def _offline_fred_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the FRED client sees only a noncredential fixture value."""
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")


def _context(
    fetcher: FredSeniorLoanOfficerSurveyFetcher,
    *,
    task_attempts: int = 32,
    source_attempts: int = 32,
) -> tuple[FetchContext, RequestBudget]:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline synthetic SLOOS fixture grant",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=5, request_budget=budget), budget


def _page(
    observations: list[dict[str, object]],
    *,
    offset: int,
    limit: int,
    count: int | None = None,
) -> str:
    return json.dumps(
        {
            "count": len(observations) if count is None else count,
            "offset": offset,
            "limit": limit,
            "observations": observations[offset : offset + limit],
        }
    )


def _install_http_get(
    monkeypatch: pytest.MonkeyPatch,
    responder: Any,
) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def http_get(
        url: str,
        params: dict[str, str],
        timeout: float | None,
        **_kwargs,
    ) -> tuple[int, str]:
        del timeout
        scopes = current_request_scopes()
        reserve_scoped_attempt(scopes, "fred", "api.stlouisfed.org")
        snapshot = dict(params)
        calls.append((url, snapshot))
        return responder(url, snapshot)

    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        http_get,
    )
    return calls


def test_public_query_contract_and_strict_date_and_bound_validation() -> None:
    query = FredSeniorLoanOfficerSurveyQuery.model_validate({})
    assert query.category == "spreads"
    assert query.transform is None
    assert query.max_requests == 32
    assert query.max_records == 10_000
    assert query.market is None
    assert FredSeniorLoanOfficerSurveyQuery(market="us").market == "us"

    for category in SLOOS_CATEGORY_ORDER:
        assert FredSeniorLoanOfficerSurveyQuery(category=category).category == category

    invalid_values = (
        {"category": "heloc"},
        {"transform": "bad"},
        {"start_date": "2026-1-01"},
        {"start_date": "2026-02-30"},
        {"start_date": "2026-02-01", "end_date": "2026-01-31"},
        {"max_requests": 33},
        {"max_requests": True},
        {"max_records": 0},
        {"unexpected": "reject"},
    )
    for values in invalid_values:
        with pytest.raises(ValidationError):
            FredSeniorLoanOfficerSurveyQuery.model_validate(values)


def test_runtime_annotations_and_fred_transform_mapping() -> None:
    series_hints = get_type_hints(SloosSeriesSpec)
    assert series_hints["category"] == SloosCategory
    assert series_hints["selection_scope"] == SloosSelectionScope
    assert series_hints["min_date"] is date

    transform_hints = get_type_hints(_fred_transform)
    assert transform_hints["query_transform"] == SloosTransform | None
    assert transform_hints["return"] == SloosFREDTransform
    assert _fred_transform(None) == "lin"
    for transform in ("chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"):
        assert _fred_transform(transform) == transform


def test_candidate_map_matches_frozen_csv_and_partial_scope_counts() -> None:
    with MAPPING_CSV.open(newline="", encoding="utf-8") as stream:
        candidate_rows = list(csv.DictReader(stream))

    assert len(candidate_rows) == 170
    assert len(SLOOS_SERIES) == len(candidate_rows)
    assert len({series.fred_series_id for series in SLOOS_SERIES}) == 93
    actual = [
        (
            series.category,
            series.selection,
            series.board_series_id,
            series.fred_series_id,
            series.panel,
            series.bank_size,
            series.loan_type,
            series.measure,
            series.terms,
            series.source_unit_label,
            series.unit_multiplier,
            series.frequency_code,
            series.min_date.isoformat(),
            series.max_date.isoformat(),
            series.description,
        )
        for series in SLOOS_SERIES
    ]
    expected = [
        (
            row["category"],
            row["selection"],
            row["board_series_id"],
            row["fred_series_id"],
            row["panel"],
            row["bank_size"],
            row["loan_type"],
            row["measure"],
            row["terms"],
            row["unit"],
            int(row["unit_multiplier"]),
            row["frequency_code"],
            row["min_date"],
            row["max_date"],
            row["description"],
        )
        for row in candidate_rows
    ]
    assert actual == expected

    counts = {category: len(series_for_category(category)) for category in SLOOS_CATEGORY_ORDER}
    assert counts == {
        "spreads": 6,
        "consumer": 29,
        "auto": 7,
        "credit_card": 7,
        "firms": 32,
        "mortgage": 20,
        "commercial_real_estate": 10,
        "standards": 23,
        "demand": 23,
        "foreign_banks": 13,
    }
    assert all(item.bank_size == "ALL" for item in SLOOS_SERIES)
    assert all(
        item.selection_scope == "all_banks_consumer_excludes_heloc"
        for item in series_for_category("consumer")
    )
    assert all(
        item.selection_scope == "all_banks_home_mortgage_excludes_heloc"
        for item in series_for_category("mortgage")
    )
    foreign = series_for_category("foreign_banks")
    assert len(foreign) == 13
    assert all(
        item.panel == "FGN"
        and item.source_unit_label == "Percentage"
        and item.selection_scope == "foreign_bank_lending_net_percentages_only"
        for item in foreign
    )
    assert any(item.loan_type == "HELOC" for item in series_for_category("standards"))
    assert any(item.loan_type == "HELOC" for item in series_for_category("demand"))

    counterfactual = list(actual)
    counterfactual[0] = (*counterfactual[0][:3], "WRONG_FRED_ID", *counterfactual[0][4:])
    assert counterfactual != expected


def test_fetch_preserves_units_transform_dates_and_typed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(fetcher)
    expected_series = series_for_category("spreads")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        assert url == "https://api.stlouisfed.org/fred/series/observations"
        assert params["series_id"] in {item.fred_series_id for item in expected_series}
        observations: list[dict[str, object]] = []
        if params["series_id"] == expected_series[0].fred_series_id:
            observations = [
                {"date": "1990-06-30", "value": "4.2500"},
                {"date": "1990-09-30", "value": "."},
            ]
        return 200, _page(
            observations,
            offset=0,
            limit=int(params["limit"]),
        )

    calls = _install_http_get(monkeypatch, responder)
    rows = fetcher.fetch(
        ctx=context,
        category="spreads",
        transform=None,
        start_date="1990-06-30",
        end_date="1990-09-30",
    )

    assert len(calls) == len(expected_series) == 6
    assert budget.attempts_used == 6
    assert budget.attempts_used_for("fred") == 6
    assert [params["series_id"] for _, params in calls] == [
        item.fred_series_id for item in expected_series
    ]
    assert all(params["units"] == "lin" for _, params in calls)
    assert all(params["observation_start"] == "1990-06-30" for _, params in calls)
    assert all(params["observation_end"] == "1990-09-30" for _, params in calls)
    assert {url for url, _ in calls} == {"https://api.stlouisfed.org/fred/series/observations"}
    assert len(rows) == 2
    assert all(isinstance(row, FredSeniorLoanOfficerSurvey) for row in rows)
    first = rows[0]
    assert first.date == date(1990, 6, 30)
    assert first.symbol == expected_series[0].fred_series_id
    assert first.value == 4.25
    assert first.title is None
    assert first.board_series_id == expected_series[0].board_series_id
    assert first.source_unit_label == "Percentage"
    assert first.source_unit_multiplier == 1
    assert first.transform == "lin"
    assert first.categories_are_additive is False
    assert rows[1].date == date(1990, 9, 30)
    assert rows[1].value is None
    serialized = json.loads(first.model_dump_json())
    assert serialized["date"] == "1990-06-30"
    assert serialized["value"] == 4.25
    assert serialized["title"] is None


def test_requested_fred_transform_is_passed_through_without_rescaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)
    expected_series = series_for_category("spreads")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        observations = (
            [{"date": "1990-06-30", "value": "-0.375"}]
            if params["series_id"] == expected_series[0].fred_series_id
            else []
        )
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    calls = _install_http_get(monkeypatch, responder)
    rows = fetcher.fetch(ctx=context, category="spreads", transform="pch")

    assert len(calls) == 6
    assert all(params["units"] == "pch" for _, params in calls)
    assert rows[0].value == -0.375
    assert rows[0].transform == "pch"


@pytest.mark.parametrize(
    ("task_attempts", "source_attempts", "max_requests", "limit_kind"),
    [
        (31, 31, 32, "request_budget"),
        (32, 32, 31, "max_requests"),
    ],
)
def test_max_request_preflight_rejects_whole_category_without_consuming_attempts(
    monkeypatch: pytest.MonkeyPatch,
    task_attempts: int,
    source_attempts: int,
    max_requests: int,
    limit_kind: str,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(
        fetcher,
        task_attempts=task_attempts,
        source_attempts=source_attempts,
    )
    calls = _install_http_get(
        monkeypatch,
        lambda url, params: (200, _page([], offset=0, limit=int(params["limit"]))),
    )

    with pytest.raises(FredSloosQueryLimitError) as error:
        fetcher.fetch(ctx=context, category="firms", max_requests=max_requests)

    assert error.value.code == "FRED_QUERY_LIMIT"
    assert error.value.limit_kind == limit_kind
    assert error.value.requested == 32
    assert error.value.maximum == 31
    assert error.value.partial is False
    assert calls == []
    assert budget.attempts_used == 0
    assert budget.attempts_used_for("fred") == 0


def test_default_fanout_fits_exact_maximum_and_stops_no_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(fetcher)
    firms = series_for_category("firms")
    calls = _install_http_get(
        monkeypatch,
        lambda url, params: (200, _page([], offset=0, limit=int(params["limit"]))),
    )

    rows = fetcher.fetch(ctx=context, category="firms")

    assert rows == ()
    assert len(calls) == len(firms) == 32
    assert [params["series_id"] for _, params in calls] == [item.fred_series_id for item in firms]
    assert budget.attempts_used == budget.attempts_used_for("fred") == 32


def test_total_max_records_fails_closed_without_returning_partial_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(fetcher)
    selected = series_for_category("spreads")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        observations = (
            [
                {"date": "1990-06-30", "value": "1.0"},
                {"date": "1990-09-30", "value": "2.0"},
            ]
            if params["series_id"] == selected[0].fred_series_id
            else []
        )
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    calls = _install_http_get(monkeypatch, responder)
    with pytest.raises(FredSloosQueryLimitError) as error:
        fetcher.fetch(ctx=context, category="spreads", max_records=1)

    assert error.value.code == "FRED_QUERY_LIMIT"
    assert error.value.limit_kind == "max_records"
    assert error.value.partial is False
    assert len(calls) == 1
    assert budget.attempts_used == 1


def test_total_max_records_stops_before_next_series_when_cap_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(fetcher)
    selected = series_for_category("spreads")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        observations = [{"date": "1990-06-30", "value": "1.0"}]
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    calls = _install_http_get(monkeypatch, responder)
    with pytest.raises(FredSloosQueryLimitError) as error:
        fetcher.fetch(ctx=context, category="spreads", max_records=5)

    assert error.value.code == "FRED_QUERY_LIMIT"
    assert error.value.limit_kind == "max_records"
    assert error.value.requested == 6
    assert error.value.maximum == 5
    assert error.value.partial is False
    assert [params["series_id"] for _, params in calls] == [
        series.fred_series_id for series in selected[:5]
    ]
    assert len(calls) == 5
    assert budget.attempts_used == budget.attempts_used_for("fred") == 5


def test_exact_max_records_on_final_series_succeeds_after_empty_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, budget = _context(fetcher)
    selected = series_for_category("spreads")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        if params["series_id"] == selected[0].fred_series_id:
            observations = [{"date": "1990-06-30", "value": "1.0"}]
        elif params["series_id"] == selected[-1].fred_series_id:
            observations = [{"date": "1990-09-30", "value": "2.0"}]
        else:
            observations = []
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    calls = _install_http_get(monkeypatch, responder)
    rows = fetcher.fetch(ctx=context, category="spreads", max_records=2)

    assert len(calls) == len(selected) == 6
    assert [params["series_id"] for _, params in calls] == [
        series.fred_series_id for series in selected
    ]
    assert [params["limit"] for _, params in calls] == ["2", "1", "1", "1", "1", "1"]
    assert len(rows) == 2
    assert [row.symbol for row in rows] == [
        selected[0].fred_series_id,
        selected[-1].fred_series_id,
    ]
    assert [row.value for row in rows] == [1.0, 2.0]
    assert budget.attempts_used == budget.attempts_used_for("fred") == 6


def test_original_percent_unit_label_and_value_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)
    consumer_series = series_for_category("consumer")
    percent_series = next(item for item in consumer_series if item.source_unit_label == "Percent")

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        observations = (
            [{"date": "1966-09-30", "value": "7.25"}]
            if params["series_id"] == percent_series.fred_series_id
            else []
        )
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    calls = _install_http_get(monkeypatch, responder)
    rows = fetcher.fetch(ctx=context, category="consumer")

    assert len(calls) == len(consumer_series) == 29
    assert len(rows) == 1
    assert rows[0].value == 7.25
    assert rows[0].source_unit_label == "Percent"
    assert rows[0].source_unit_multiplier == 1


def test_observations_outside_requested_dates_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        observations = (
            [{"date": "1990-06-29", "value": "1.0"}]
            if params["series_id"] == series_for_category("spreads")[0].fred_series_id
            else []
        )
        return 200, _page(observations, offset=0, limit=int(params["limit"]))

    _install_http_get(monkeypatch, responder)
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(
            ctx=context,
            category="spreads",
            start_date="1990-06-30",
            end_date="1990-09-30",
        )
    assert error.value.code == "FRED_BAD_OBSERVATION"


@pytest.mark.parametrize(
    "observations",
    [
        [{"date": "1990-06-30", "value": "NaN"}],
        [{"date": "1990-06-31", "value": "1"}],
        [
            {"date": "1990-09-30", "value": "1"},
            {"date": "1990-06-30", "value": "2"},
        ],
        [
            {
                "date": "1990-06-30",
                "value": "1",
                "realtime_start": "1990-06-30",
                "realtime_end": "1990-06-30",
            },
            {
                "date": "1990-06-30",
                "value": "2",
                "realtime_start": "1990-07-01",
                "realtime_end": "9999-12-31",
            },
        ],
        [{"date": "1990-06-30", "value": "1", "series_id": "WRONG"}],
    ],
)
def test_malformed_values_dates_order_and_echoed_identity_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    observations: list[dict[str, object]],
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)
    first_series = series_for_category("spreads")[0]

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        page_rows = observations if params["series_id"] == first_series.fred_series_id else []
        return 200, _page(page_rows, offset=0, limit=int(params["limit"]))

    _install_http_get(monkeypatch, responder)
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=context, category="spreads")
    assert error.value.code == "FRED_BAD_OBSERVATION"


@pytest.mark.parametrize(
    "response_case",
    (
        "missing_count",
        "short_page",
        "wrong_limit",
        "wrong_offset",
        "missing_observations",
    ),
)
def test_missing_or_inconsistent_page_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    response_case: str,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)

    def responder(url: str, params: dict[str, str]) -> tuple[int, str]:
        del url
        limit = int(params["limit"])
        cases = {
            "missing_count": {"offset": 0, "limit": limit, "observations": []},
            "short_page": {"count": 1, "offset": 0, "limit": limit, "observations": []},
            "wrong_limit": {"count": 0, "offset": 0, "limit": limit + 1, "observations": []},
            "wrong_offset": {"count": 0, "offset": 1, "limit": limit, "observations": []},
            "missing_observations": {"count": 0, "offset": 0, "limit": limit},
        }
        return 200, json.dumps(cases[response_case])

    _install_http_get(monkeypatch, responder)
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=context, category="spreads")
    assert error.value.code == "FRED_PAGINATION_ERROR"


def test_http_failure_keeps_existing_fred_error_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredSeniorLoanOfficerSurveyFetcher()
    context, _ = _context(fetcher)
    _install_http_get(monkeypatch, lambda url, params: (429, "rate limited"))

    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=context, category="spreads")
    assert error.value.code == "FRED_HTTP_ERROR"
    assert error.value.status == 429
