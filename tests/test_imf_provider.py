"""Unit and live tests for the imf provider (C1 P0).

Unit tests monkeypatch the HTTP seam (no network); the live test hits the
public IMF DataMapper - no key needed - and is what justified
``verified=True``: the adapter's output is asserted against the document
the API itself returns.
"""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers.imf import register
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models._client import ImfProviderError
from opendata.data.providers.imf.models.cpi import ImfCpiFetcher
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry


def _document(values: dict[str, float]) -> str:
    return json.dumps({"values": {"PCPIPCH": {"USA": values}}})


@pytest.fixture(autouse=True)
def registry() -> ProviderRegistry:
    """Registered fetchers for every test, isolated per test via the singleton."""
    register()
    return get_registry()


class TestRegistration:
    def test_capability_fields(self) -> None:
        capability = ImfCpiFetcher().capability
        assert capability.domain == "economy_cpi"
        assert capability.asset_class == "macro"
        assert capability.period == "1A"
        assert capability.market == "global"
        assert capability.source == SOURCE == "imf"
        assert capability.verified is True

    def test_register_is_idempotent(self) -> None:
        assert register() == []

    def test_auto_routing_prefers_a_verified_source(self) -> None:
        """ecb/imf are both verified; auto must route to one of them, not fred."""
        resolved = get_registry().resolve_domain("economy_cpi")
        assert resolved.capability.source in {"ecb", "imf"}


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ImfCpiFetcher().transform_query(indicator="PCPIPCH", country="USA", bogus=1)


class TestClient:
    def test_http_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.imf.models._client._http_get",
            lambda url, timeout: (404, "not found"),
        )
        with pytest.raises(ImfProviderError) as err:
            ImfCpiFetcher().extract_data(
                ImfCpiFetcher().transform_query(indicator="PCPIPCH", country="USA"),
                FetchContext(),
            )
        assert err.value.code == "IMF_HTTP_ERROR"

    def test_wrong_shape_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.imf.models._client._http_get",
            lambda url, timeout: (200, json.dumps({"unrelated": {}})),
        )
        with pytest.raises(ImfProviderError) as err:
            ImfCpiFetcher().extract_data(
                ImfCpiFetcher().transform_query(indicator="PCPIPCH", country="USA"),
                FetchContext(),
            )
        assert err.value.code == "IMF_BAD_RESPONSE"


class TestTransform:
    def test_rows_sorted_series_id_and_window_filtering(self) -> None:
        fetcher = ImfCpiFetcher()
        raw = {"2023": 4.1, "2024": 2.9, "2025": 3.0}
        query = fetcher.transform_query(
            indicator="pcpipch",
            country="usa",
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2024, 12, 31),
        )
        result = fetcher.transform_data(raw, query)
        assert [point.date.isoformat() for point in result] == ["2024-01-01"]
        assert result[0].series_id == "PCPIPCH.USA"
        assert result[0].value == pytest.approx(2.9)

    def test_unparseable_year_fails_closed(self) -> None:
        fetcher = ImfCpiFetcher()
        with pytest.raises(ImfProviderError) as err:
            fetcher.transform_data(
                {"20X3": 1.0}, fetcher.transform_query(indicator="PCPIPCH", country="USA")
            )
        assert err.value.code == "IMF_BAD_OBSERVATION"

    def test_empty_upstream_yields_no_rows(self) -> None:
        fetcher = ImfCpiFetcher()
        assert (
            fetcher.transform_data({}, fetcher.transform_query(indicator="PCPIPCH", country="USA"))
            == ()
        )


@pytest.mark.e2e
class TestLive:
    def test_usa_cpi_matches_the_datamapper(self) -> None:
        """Adapter output equals the API's own document (verified=True basis)."""
        fetcher = ImfCpiFetcher()
        query = fetcher.transform_query(
            indicator="PCPIPCH", country="USA", start_date=datetime.date(2023, 1, 1)
        )
        raw = fetcher.extract_data(query, FetchContext(timeout=20.0))
        result = fetcher.transform_data(raw, query)
        assert len(result) >= 2
        assert [point.date for point in result] == sorted(point.date for point in result)
        assert all(point.series_id == "PCPIPCH.USA" for point in result)
        first_year = str(result[0].date.year)
        assert result[0].date.isoformat() == f"{first_year}-01-01"
        assert result[0].value == pytest.approx(raw[first_year])
