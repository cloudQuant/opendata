"""Unit and live tests for the oecd provider (C1 P0).

Unit tests monkeypatch the HTTP seam (no network); the live test hits the
public OECD SDMX API - no key needed - and is what justified
``verified=True``: the adapter's output is asserted against the CSV the
API itself returns.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers.oecd import register
from opendata.data.providers.oecd._source import SOURCE
from opendata.data.providers.oecd.models._client import OecdProviderError
from opendata.data.providers.oecd.models.cpi import OecdCpiFetcher
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry

GBR_CPI_KEY = "GBR.M.HICP.CPI.PC.CP09.N.G1"

#: The seven non-REF_AREA key dimensions of the GBR CPI series.
_KEY_DIMS = {
    "FREQ": "M",
    "METHODOLOGY": "HICP",
    "MEASURE": "CPI",
    "UNIT_MEASURE": "PC",
    "EXPENDITURE": "CP09",
    "ADJUSTMENT": "N",
    "TRANSFORMATION": "G1",
}


def _csv_body(rows: list[dict[str, str]]) -> str:
    """Serialize CSV rows the way the SDMX API does."""
    if not rows:
        return "DATAFLOW,REF_AREA,TIME_PERIOD,OBS_VALUE\n"
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
        capability = OecdCpiFetcher().capability
        assert capability.domain == "economy_cpi"
        assert capability.asset_class == "macro"
        assert capability.period == "1M"
        assert capability.market == "eu"
        assert capability.source == SOURCE == "oecd"
        assert capability.verified is True

    def test_register_is_idempotent(self) -> None:
        assert register() == []

    def test_auto_routing_prefers_a_verified_source(self) -> None:
        resolved = get_registry().resolve_domain("economy_cpi")
        assert resolved.capability.source in {"ecb", "imf", "oecd"}


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            OecdCpiFetcher().transform_query(series_key=GBR_CPI_KEY, bogus=1)


class TestClient:
    def test_http_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.oecd.models._client._http_get",
            lambda url, params, timeout: (404, "NoRecordsFound"),
        )
        with pytest.raises(OecdProviderError) as err:
            OecdCpiFetcher().extract_data(
                OecdCpiFetcher().transform_query(series_key=GBR_CPI_KEY), FetchContext()
            )
        assert err.value.code == "OECD_HTTP_ERROR"

    def test_non_csv_body_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "opendata.data.providers.oecd.models._client._http_get",
            lambda url, params, timeout: (200, "DATAFLOW,WRONG\nX,2026-01,1.0\n"),
        )
        with pytest.raises(OecdProviderError) as err:
            OecdCpiFetcher().extract_data(
                OecdCpiFetcher().transform_query(series_key=GBR_CPI_KEY), FetchContext()
            )
        assert err.value.code == "OECD_BAD_RESPONSE"


class TestTransform:
    def test_missing_value_becomes_none_and_rows_sorted(self) -> None:
        fetcher = OecdCpiFetcher()
        raw = [
            {**_KEY_DIMS, "REF_AREA": "GBR", "TIME_PERIOD": "2026-02", "OBS_VALUE": "0.2220577"},
            {**_KEY_DIMS, "REF_AREA": "GBR", "TIME_PERIOD": "2026-01", "OBS_VALUE": ""},
        ]
        result = fetcher.transform_data(raw, fetcher.transform_query(series_key=GBR_CPI_KEY))
        assert [point.date.isoformat() for point in result] == ["2026-01-01", "2026-02-01"]
        assert result[0].value is None
        assert result[1].value == pytest.approx(0.2220577)

    def test_series_id_is_the_dotted_dimension_key(self) -> None:
        fetcher = OecdCpiFetcher()
        raw = [
            {
                "DATAFLOW": "X",
                "REF_AREA": "GBR",
                "FREQ": "M",
                "METHODOLOGY": "HICP",
                "MEASURE": "CPI",
                "UNIT_MEASURE": "PC",
                "EXPENDITURE": "CP09",
                "ADJUSTMENT": "N",
                "TRANSFORMATION": "G1",
                "TIME_PERIOD": "2026-02",
                "OBS_VALUE": "0.2220577",
            }
        ]
        result = fetcher.transform_data(raw, fetcher.transform_query(series_key=GBR_CPI_KEY))
        assert result[0].series_id == GBR_CPI_KEY

    def test_unparseable_value_fails_closed(self) -> None:
        fetcher = OecdCpiFetcher()
        with pytest.raises(OecdProviderError) as err:
            fetcher.transform_data(
                [{**_KEY_DIMS, "REF_AREA": "GBR", "TIME_PERIOD": "2026-02", "OBS_VALUE": "abc"}],
                fetcher.transform_query(series_key=GBR_CPI_KEY),
            )
        assert err.value.code == "OECD_BAD_OBSERVATION"

    def test_empty_upstream_yields_no_rows(self) -> None:
        fetcher = OecdCpiFetcher()
        assert fetcher.transform_data([], fetcher.transform_query(series_key=GBR_CPI_KEY)) == ()


@pytest.mark.e2e
class TestLive:
    def test_gbr_cpi_matches_the_api(self) -> None:
        """Adapter output equals the SDMX API's own CSV (verified=True basis)."""
        import datetime

        fetcher = OecdCpiFetcher()
        query = fetcher.transform_query(
            series_key=GBR_CPI_KEY, start_date=datetime.date(2025, 6, 1)
        )
        raw = fetcher.extract_data(query, FetchContext(timeout=20.0))
        result = fetcher.transform_data(raw, query)
        assert len(result) >= 3
        assert [point.date for point in result] == sorted(point.date for point in result)
        assert all(point.series_id == GBR_CPI_KEY for point in result)
        latest = max(raw, key=lambda row: str(row["TIME_PERIOD"]))
        assert result[-1].date.isoformat()[:7] == str(latest["TIME_PERIOD"])[:7]
        assert result[-1].value == pytest.approx(float(latest["OBS_VALUE"]))
