"""Unit and live tests for the ecb provider (C1 P0).

Unit tests monkeypatch the HTTP seam (no network); the live test hits the
public ECB Data Portal - no key needed - and is what justified
``verified=True``: the adapter's output was compared against the CSV the
portal itself returns.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers.ecb import register
from opendata.data.providers.ecb._source import SOURCE
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models.cpi import EcbCpiFetcher
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry

EURO_AREA_ANR_KEY = "ICP/M.U2.N.000000.4.ANR"


def _csv_body(rows: list[dict[str, str]]) -> str:
    """Serialize CSV rows the way the Data Portal does."""
    if not rows:
        return "KEY,TIME_PERIOD,OBS_VALUE\n"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def registry() -> ProviderRegistry:
    """Registered fetchers for every test, isolated per test via the singleton."""
    register()
    return get_registry()


class TestRegistration:
    def test_capability_fields(self) -> None:
        capability = EcbCpiFetcher().capability
        assert capability.domain == "economy_cpi"
        assert capability.asset_class == "macro"
        assert capability.period == "1M"
        assert capability.market == "eu"
        assert capability.source == SOURCE == "ecb"
        assert capability.verified is True

    def test_register_is_idempotent(self) -> None:
        assert register() == []

    def test_verified_capability_is_auto_routed(self) -> None:
        fetcher = get_registry().resolve_domain("economy_cpi")
        assert isinstance(fetcher, EcbCpiFetcher)


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            EcbCpiFetcher().transform_query(series_id=EURO_AREA_ANR_KEY, bogus=1)


class TestClient:
    def test_http_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.ecb.models._client._http_get",
            lambda url, params, timeout: (404, "no such series"),
        )
        with pytest.raises(EcbProviderError) as err:
            EcbCpiFetcher().extract_data(
                EcbCpiFetcher().transform_query(series_id=EURO_AREA_ANR_KEY), FetchContext()
            )
        assert err.value.code == "ECB_HTTP_ERROR"

    def test_non_csv_body_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.ecb.models._client._http_get",
            lambda url, params, timeout: (200, "KEY,WRONG\nICP.M,2026-01,1.0\n"),
        )
        with pytest.raises(EcbProviderError) as err:
            EcbCpiFetcher().extract_data(
                EcbCpiFetcher().transform_query(series_id=EURO_AREA_ANR_KEY), FetchContext()
            )
        assert err.value.code == "ECB_BAD_RESPONSE"


class TestTransform:
    def test_missing_value_becomes_none_and_rows_sorted(self) -> None:
        fetcher = EcbCpiFetcher()
        raw = [
            {"KEY": "ICP.M.U2.N.000000.4.ANR", "TIME_PERIOD": "2025-12", "OBS_VALUE": "1.9"},
            {"KEY": "ICP.M.U2.N.000000.4.ANR", "TIME_PERIOD": "2025-11", "OBS_VALUE": ""},
        ]
        result = fetcher.transform_data(raw, fetcher.transform_query(series_id=EURO_AREA_ANR_KEY))
        assert [point.date.isoformat() for point in result] == ["2025-11-01", "2025-12-01"]
        assert all(point.series_id == "ICP.M.U2.N.000000.4.ANR" for point in result)
        assert result[0].value is None
        assert result[1].value == pytest.approx(1.9)

    def test_unparseable_value_fails_closed(self) -> None:
        fetcher = EcbCpiFetcher()
        with pytest.raises(EcbProviderError) as err:
            fetcher.transform_data(
                [{"KEY": "ICP.M.U2", "TIME_PERIOD": "2025-12", "OBS_VALUE": "abc"}],
                fetcher.transform_query(series_id=EURO_AREA_ANR_KEY),
            )
        assert err.value.code == "ECB_BAD_OBSERVATION"

    def test_empty_upstream_yields_no_rows(self) -> None:
        fetcher = EcbCpiFetcher()
        assert (
            fetcher.transform_data([], fetcher.transform_query(series_id=EURO_AREA_ANR_KEY)) == ()
        )


@pytest.mark.e2e
class TestLive:
    def test_euro_area_anr_matches_the_portal(self) -> None:
        """Adapter output equals the portal's own CSV (verified=True basis).

        Asserts the adapter against the raw CSV it just fetched (same
        source, self-checking the parse) instead of pinning a date that
        the portal will move past.
        """
        import datetime

        fetcher = EcbCpiFetcher()
        query = fetcher.transform_query(
            series_id=EURO_AREA_ANR_KEY, start_date=datetime.date(2025, 1, 1)
        )
        raw = fetcher.extract_data(query, FetchContext(timeout=20.0))
        result = fetcher.transform_data(raw, query)
        assert len(result) >= 12
        assert [point.date for point in result] == sorted(point.date for point in result)
        assert all(point.series_id == "ICP.M.U2.N.000000.4.ANR" for point in result)
        last_raw = raw[-1]
        assert result[-1].date.isoformat()[:7] == str(last_raw["TIME_PERIOD"])[:7]
        assert result[-1].value == pytest.approx(float(last_raw["OBS_VALUE"]))
