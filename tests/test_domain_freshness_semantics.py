"""Domain-declared date semantics for day-level freshness and query windows."""

from datetime import date, datetime
from types import SimpleNamespace
from typing import Annotated

import pytest

from opendata.data.domains import DomainSpec, require_domain
from opendata.data.models import SeriesCatalogItem
from opendata.pipeline import freshness
from opendata.pipeline.query import resolve_time_field


def _declared_spec(*, time_field: str | None, contract: str = "SeriesCatalogItem") -> DomainSpec:
    return DomainSpec(
        display_name="Synthetic provider domain",
        rest_path="test/synthetic-provider",
        contract=contract,
        temporal_kind="snapshot" if time_field is None else "series",
        time_field=time_field,
        natural_key=("series_id",),
        filter_dims=(),
        storage_mode="transient",
        permissions=("query",),
    )


def _field(annotation: object) -> SimpleNamespace:
    return SimpleNamespace(annotation=annotation)


def _install_synthetic_domain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: DomainSpec,
    model_fields: dict[str, SimpleNamespace],
) -> None:
    model = SimpleNamespace(model_fields=model_fields)
    monkeypatch.setattr(freshness, "require_domain", lambda _domain: spec)
    monkeypatch.setattr(freshness, "contract_model", lambda _domain: model)


def test_fred_catalog_uses_declared_date_field_not_first_catalog_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _declared_spec(time_field="observation_end")
    monkeypatch.setattr(freshness, "require_domain", lambda _domain: spec)
    monkeypatch.setattr(freshness, "contract_model", lambda _domain: SeriesCatalogItem)

    assert freshness.freshness_field("fred_search_synthetic") == "observation_end"
    assert resolve_time_field("fred_search_synthetic") == "observation_end"
    field_order = tuple(SeriesCatalogItem.model_fields)
    assert field_order.index("observation_start") < field_order.index("observation_end")
    assert SeriesCatalogItem.model_fields["observation_start"].annotation is date
    assert SeriesCatalogItem.model_fields["observation_end"].annotation is date


def test_declared_null_time_field_rejects_fallback_to_other_date_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _declared_spec(time_field=None)
    _install_synthetic_domain(
        monkeypatch,
        spec=spec,
        model_fields={"observation_start": _field(date), "observation_end": _field(date)},
    )

    with pytest.raises(ValueError, match="declares no time_field"):
        freshness.freshness_field("synthetic_snapshot")
    with pytest.raises(ValueError, match="declares no time_field"):
        resolve_time_field("synthetic_snapshot")


@pytest.mark.parametrize(
    ("time_field", "annotation"),
    [("year", int), ("captured_at", datetime)],
)
def test_declared_non_date_time_field_rejects_fallback_to_date_field(
    monkeypatch: pytest.MonkeyPatch,
    time_field: str,
    annotation: object,
) -> None:
    spec = _declared_spec(time_field=time_field, contract="BlsObservation")
    _install_synthetic_domain(
        monkeypatch,
        spec=spec,
        model_fields={
            "unrelated_date": _field(date),
            time_field: _field(annotation),
        },
    )

    with pytest.raises(ValueError, match="not a date field"):
        freshness.freshness_field("synthetic_bls_series")
    with pytest.raises(ValueError, match="not a date field"):
        resolve_time_field("synthetic_bls_series")


@pytest.mark.parametrize(
    "annotation",
    [
        date,
        date | None,
        Annotated[date, "source day"],
        Annotated[date | None, "nullable source day"],
    ],
)
def test_declared_optional_or_annotated_date_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    annotation: object,
) -> None:
    spec = _declared_spec(time_field="selected_day")
    _install_synthetic_domain(
        monkeypatch,
        spec=spec,
        model_fields={
            "unrelated_date": _field(date),
            "selected_day": _field(annotation),
        },
    )

    assert freshness.freshness_field("synthetic_series") == "selected_day"
    assert resolve_time_field("synthetic_series") == "selected_day"


@pytest.mark.parametrize(
    ("domain", "expected_time_field", "supports_daily_freshness"),
    [
        ("fred_search", "last_updated", False),
        ("fred_series", "date", True),
        ("bls_search", "catalog_as_of", True),
        ("bls_series", "year", False),
        ("equity_historical", "date", True),
        ("equity_quote", None, False),
    ],
)
def test_new_provider_domains_follow_their_declared_day_semantics(
    domain: str,
    expected_time_field: str | None,
    supports_daily_freshness: bool,
) -> None:
    spec = require_domain(domain)
    assert spec.semantics_declared is True
    assert spec.time_field == expected_time_field

    if supports_daily_freshness:
        assert spec.time_field is not None
        assert freshness.freshness_field(domain) == spec.time_field
        assert resolve_time_field(domain) == spec.time_field
    else:
        with pytest.raises(ValueError):
            freshness.freshness_field(domain)
        with pytest.raises(ValueError):
            resolve_time_field(domain)


def test_legacy_stock_trade_and_action_dates_keep_first_date_behavior() -> None:
    assert require_domain("stock_daily").semantics_declared is False
    assert require_domain("stock_action").semantics_declared is False
    assert freshness.freshness_field("stock_daily") == "trade_date"
    assert freshness.freshness_field("stock_action") == "ex_date"
    assert resolve_time_field("stock_daily") == "trade_date"
    assert resolve_time_field("stock_action") == "ex_date"
