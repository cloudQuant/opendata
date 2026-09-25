"""Unit tests for the fred provider (C1 P0).

No network and no API key are needed: the HTTP layer is monkeypatched and
the keyless path is asserted directly. Live verification against official
series values is deferred per R2 (no FRED_API_KEY configured yet).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext
from opendata.data.providers import register_providers
from opendata.data.providers.fred import register
from opendata.data.providers.fred._source import SOURCE
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.cpi import FredCpiFetcher
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry


def _document(observations: list[dict[str, str]]) -> str:
    return json.dumps({"observations": observations})


@pytest.fixture(autouse=True)
def registry() -> ProviderRegistry:
    """The process-wide singleton with **every** provider registered.

    ``register_providers()`` is idempotent, and calling it here is what
    makes the routing assertions below order-independent: with only this
    provider's legs in the pool ``economy_cpi`` would have one market, the
    cross-market guard could never fire, and "green" would still mean
    "whichever module happened to import first" - the failure mode C22
    set out to remove.
    """
    register_providers()
    return get_registry()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured key for every test unless a case clears it on purpose."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")


class TestRegistration:
    def test_capability_fields(self) -> None:
        capability = FredCpiFetcher().capability
        assert capability.domain == "economy_cpi"
        assert capability.asset_class == "macro"
        assert capability.period == "1M"
        assert capability.market == "us"
        assert capability.source == SOURCE == "fred"
        assert capability.verified is False

    def test_register_is_idempotent(self) -> None:
        assert register() == []

    def test_unverified_capability_is_not_auto_routed(self) -> None:
        """fred is the only ``1M``/``us`` CPI leg and it is unverified: auto must refuse.

        The old form of this check asked auto without naming a market, so
        its outcome depended on which other macro providers had already
        registered into the shared singleton in this process.
        """
        with pytest.raises(LookupError, match="no verified capability"):
            get_registry().resolve_domain("economy_cpi", period="1M", market="us")

    def test_explicit_source_reaches_the_unverified_leg(self) -> None:
        """Naming fred bypasses verification: the caller owns that choice."""
        resolved = get_registry().resolve_domain("economy_cpi", market="us", source="fred")
        assert isinstance(resolved, FredCpiFetcher)


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            FredCpiFetcher().transform_query(series_id="CPIAUCSL", bogus=1)


class TestClient:
    def test_missing_key_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FRED_API_KEY")
        monkeypatch.setattr(
            "opendata.core.config.get_settings",
            lambda: type("S", (), {"fred_api_key": None, "fred_api_base_url": None})(),
        )
        with pytest.raises(FredProviderError) as err:
            FredCpiFetcher().extract_data(
                FredCpiFetcher().transform_query(series_id="CPIAUCSL"), FetchContext()
            )
        assert err.value.code == "FRED_API_KEY_MISSING"
        # Nothing was requested, so there is nothing to attribute.
        assert str(err.value) == "FRED_API_KEY_MISSING"

    def test_http_error_names_the_request_but_never_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested: list[tuple[str, dict[str, str]]] = []

        def _get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
            requested.append((url, params))
            return 429, "Too Many Requests"

        monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", _get)
        with pytest.raises(FredProviderError) as err:
            FredCpiFetcher().extract_data(
                FredCpiFetcher().transform_query(series_id="CPIAUCSL"), FetchContext()
            )
        url, params = requested[0]
        assert params["api_key"], "the key must travel as a query parameter"
        assert str(err.value) == f"FRED_HTTP_ERROR status=429 url={url}"
        assert params["api_key"] not in str(err.value)

    def test_http_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.fred.models._client._http_get",
            lambda url, params, timeout: (429, "Too Many Requests"),
        )
        with pytest.raises(FredProviderError) as err:
            FredCpiFetcher().extract_data(
                FredCpiFetcher().transform_query(series_id="CPIAUCSL"), FetchContext()
            )
        assert err.value.code == "FRED_HTTP_ERROR"

    def test_malformed_body_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.fred.models._client._http_get",
            lambda url, params, timeout: (200, "<html>blocked</html>"),
        )
        with pytest.raises(FredProviderError) as err:
            FredCpiFetcher().extract_data(
                FredCpiFetcher().transform_query(series_id="CPIAUCSL"), FetchContext()
            )
        assert err.value.code == "FRED_BAD_RESPONSE"


class TestTransform:
    def test_missing_sentinel_becomes_none_and_rows_sorted(self) -> None:
        fetcher = FredCpiFetcher()
        raw = [
            {"date": "2026-08-01", "value": "322.5"},
            {"date": "2026-07-01", "value": "."},
            {"date": "2026-06-01", "value": "320.1"},
        ]
        result = fetcher.transform_data(raw, fetcher.transform_query(series_id="cpiaucsl"))
        assert [point.date.isoformat() for point in result] == [
            "2026-06-01",
            "2026-07-01",
            "2026-08-01",
        ]
        assert all(point.series_id == "CPIAUCSL" for point in result)
        assert result[1].value is None
        assert result[2].value == pytest.approx(322.5)

    def test_unparseable_observation_fails_closed(self) -> None:
        fetcher = FredCpiFetcher()
        with pytest.raises(FredProviderError) as err:
            fetcher.transform_data(
                [{"date": "2026-08-01", "value": "abc"}],
                fetcher.transform_query(series_id="CPIAUCSL"),
            )
        assert err.value.code == "FRED_BAD_OBSERVATION"

    def test_empty_upstream_yields_no_rows(self) -> None:
        fetcher = FredCpiFetcher()
        assert fetcher.transform_data([], fetcher.transform_query(series_id="CPIAUCSL")) == ()


class TestMacroSeriesContract:
    def test_value_is_nullable(self) -> None:
        import datetime

        point = MacroSeries(series_id="CPIAUCSL", date=datetime.date(2026, 8, 1))
        assert point.value is None

    def test_nan_is_rejected(self) -> None:
        import datetime

        with pytest.raises(ValidationError):
            MacroSeries(
                series_id="CPIAUCSL",
                date=datetime.date(2026, 8, 1),
                value=float("nan"),
            )
