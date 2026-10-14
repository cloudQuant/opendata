"""Manual PortWatch contract fixtures; these tests never contact IMF."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.models.macro_iter2 import (
    ImfMaritimeChokePointInfo,
    ImfMaritimeChokePointVolume,
    ImfPortInfo,
    ImfPortVolume,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.imf.models import portwatch
from opendata.data.providers.imf.models.portwatch import (
    ImfChokePointInfoFetcher,
    ImfChokePointVolumeFetcher,
    ImfPortInfoFetcher,
    ImfPortVolumeFetcher,
    ImfPortWatchError,
)
from opendata.data.providers.imf.provider import PROVIDER as IMF_PROVIDER
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_FEATURE_OBJECT_IDS = count(1)


def _feature(attributes: dict[str, object]) -> dict[str, object]:
    """Build one small fixture in the documented ArcGIS query response shape."""
    source_attributes = dict(attributes)
    source_attributes.setdefault("ObjectId", next(_FEATURE_OBJECT_IDS))
    return {"attributes": source_attributes}


def _page(*features: dict[str, object], exceeded: bool = False) -> str:
    """Serialize a hand-authored ArcGIS REST response fixture."""
    return json.dumps({"features": list(features), "exceededTransferLimit": exceeded})


def _install_pages(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[tuple[int, str]],
    *,
    counts: list[int] | None = None,
) -> list[tuple[str, dict[str, str]]]:
    requests: list[tuple[str, dict[str, str]]] = []
    count_overrides = list(counts or [])
    generated_counts: dict[tuple[str, str], int] = {}

    def _get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        if params.get("returnCountOnly") == "true":
            key = (url, params.get("where", ""))
            if count_overrides:
                value = count_overrides.pop(0)
            elif key in generated_counts:
                value = generated_counts.pop(key)
            else:
                value = 0
                for _, body in pages:
                    document = json.loads(body)
                    features = document.get("features", [])
                    value += len(features)
                    if document.get("exceededTransferLimit") is not True:
                        break
                generated_counts[key] = value
            return 200, json.dumps({"count": value})
        if not pages:
            raise AssertionError("unexpected extra request")
        status, body = pages.pop(0)
        requested = params.get("resultRecordCount")
        if status == 200 and requested is not None:
            document = json.loads(body)
            features = document.get("features")
            if isinstance(features, list) and len(features) > int(requested):
                document["features"] = features[: int(requested)]
                document["exceededTransferLimit"] = True
                body = json.dumps(document)
        return status, body

    monkeypatch.setattr(portwatch, "_http_get", _get)
    return requests


def _page_requests(requests: list[tuple[str, dict[str, str]]]) -> list[tuple[str, dict[str, str]]]:
    """Drop count-only consistency checks from page-query assertions."""
    return [(url, params) for url, params in requests if "outFields" in params]


def _fixture_context(model: str) -> FetchContext:
    """Authorize a bounded fake-transport request for one exact IMF model."""
    grant = RequestGrant(
        source="imf",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=10,
        source_attempts=10,
        allowed_hosts=frozenset({"services9.arcgis.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        timeout=2.0,
        request_budget=RequestBudget(
            task_attempts=10,
            source_attempts=10,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        ),
    )


def _fetch(fetcher: object, **query: object) -> object:
    """Run one offline fetch under its exact, fixture-only model grant."""
    canonical_model = fetcher.canonical_model  # type: ignore[attr-defined]
    return fetcher.fetch(ctx=_fixture_context(canonical_model), **query)  # type: ignore[attr-defined]


def _descriptor(fetcher: object, model: str) -> SimpleNamespace:
    capability = fetcher.capability  # type: ignore[attr-defined]
    return SimpleNamespace(
        source=capability.source,
        model=model,
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )


def _chokepoint_info() -> dict[str, object]:
    return {
        "portid": "chokepoint1",
        "portname": "Suez Canal",
        "lat": 30.5,
        "lon": 32.3,
        "vessel_count_total": 19787,
        "vessel_count_tanker": 6418,
        "vessel_count_container": 5520,
        "vessel_count_general_cargo": 900,
        "vessel_count_dry_bulk": 6000,
        "vessel_count_RoRo": 949,
        "industry_top1": "Mineral products",
        "industry_top2": None,
        "industry_top3": "Food products",
    }


def _port_info() -> dict[str, object]:
    return {
        "portid": "port1201",
        "continent": "Asia & Pacific",
        "country": "China",
        "ISO3": "CHN",
        "portname": "Shanghai",
        "fullname": "Port of Shanghai",
        "lat": 31.23,
        "lon": 121.47,
        "vessel_count_total": 21000,
        "vessel_count_tanker": 1550,
        "vessel_count_container": 10400,
        "vessel_count_general_cargo": 2300,
        "vessel_count_dry_bulk": 5800,
        "vessel_count_RoRo": 950,
        "industry_top1": "Machinery",
        "industry_top2": "Mineral products",
        "industry_top3": None,
        "share_country_maritime_import": 0.12,
        "share_country_maritime_export": 0.18,
    }


def _chokepoint_volume() -> dict[str, object]:
    return {
        "date": "2024-03-04",
        "portid": "chokepoint1",
        "portname": "Suez Canal",
        "n_container": 14,
        "n_dry_bulk": 12,
        "n_general_cargo": 3,
        "n_roro": 1,
        "n_tanker": 8,
        "n_cargo": 30,
        "n_total": 38,
        "capacity_container": 12000,
        "capacity_dry_bulk": 22000,
        "capacity_general_cargo": 800,
        "capacity_roro": 50,
        "capacity_tanker": 100000,
        "capacity_cargo": 34850,
        "capacity": 134850,
    }


def _official_suez_volume_2021_03_11() -> dict[str, object]:
    """Reproduce the captured official ObjectId=1 row without altering source values."""
    return {
        "ObjectId": 1,
        "date": "2021-03-11",
        "portid": "chokepoint1",
        "portname": "Suez Canal",
        "n_container": 16,
        "n_dry_bulk": 12,
        "n_general_cargo": 3,
        "n_roro": 2,
        "n_tanker": 23,
        "n_cargo": 33,
        "n_total": 56,
        "capacity_container": 964354,
        "capacity_dry_bulk": 351621,
        "capacity_general_cargo": 20733,
        "capacity_roro": 10326,
        "capacity_tanker": 1021116,
        "capacity_cargo": 1347035,
        "capacity": 2368151,
    }


def _port_volume() -> dict[str, object]:
    return {
        "date": "2024-03-04",
        "portid": "port1201",
        "portname": "Shanghai",
        "country": "China",
        "ISO3": "CHN",
        "portcalls_container": 40,
        "portcalls_dry_bulk": 18,
        "portcalls_general_cargo": 9,
        "portcalls_roro": 2,
        "portcalls_tanker": 11,
        "portcalls_cargo": 69,
        "portcalls": 80,
        "import_container": 1000,
        "import_dry_bulk": 2000,
        "import_general_cargo": 300,
        "import_roro": 20,
        "import_tanker": 5000,
        "import_cargo": 3320,
        "import": 8320,
        "export_container": 1100,
        "export_dry_bulk": 2100,
        "export_general_cargo": 310,
        "export_roro": 30,
        "export_tanker": 5100,
        "export_cargo": 3540,
        "export": 8640,
    }


def _official_port_volume_2019_01_03() -> dict[str, object]:
    """Reproduce the captured official Daily_Ports_Data ObjectId=3 source row."""
    return {
        "ObjectId": 3,
        "date": "2019-01-03",
        "portid": "port1325",
        "portname": "Tsuruga",
        "country": "Japan",
        "ISO3": "JPN",
        "portcalls_container": 1,
        "portcalls_dry_bulk": 0,
        "portcalls_general_cargo": 0,
        "portcalls_roro": 0,
        "portcalls_tanker": 0,
        "portcalls_cargo": 1,
        "portcalls": 1,
        "import_container": 439,
        "import_dry_bulk": 0,
        "import_general_cargo": 0,
        "import_roro": 0,
        "import_tanker": 0,
        "import_cargo": 439,
        "import": 439,
        "export_container": 0,
        "export_dry_bulk": 0,
        "export_general_cargo": 0,
        "export_roro": 0,
        "export_tanker": 0,
        "export_cargo": 0,
        "export": 0,
    }


class TestQueryValidation:
    def test_bad_country_is_rejected_before_source_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(portwatch, "_http_get", lambda *args: calls.append(args))
        with pytest.raises(ValidationError):
            ImfPortInfoFetcher().fetch(country="USA' OR 1=1 --")
        assert calls == []

    def test_reverse_date_window_is_rejected_before_source_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(portwatch, "_http_get", lambda *args: calls.append(args))
        with pytest.raises(ValidationError):
            ImfPortVolumeFetcher().fetch(
                port_code="port1201", start_date="2024-03-05", end_date="2024-03-04"
            )
        assert calls == []

    def test_unbounded_port_volume_query_is_rejected_before_source_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(portwatch, "_http_get", lambda *args: calls.append(args))
        with pytest.raises(ValidationError):
            ImfPortVolumeFetcher().fetch()
        assert calls == []

    def test_port_volume_request_budget_cannot_exceed_governed_source_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(portwatch, "_http_get", lambda *args: calls.append(args))
        with pytest.raises(ValidationError):
            ImfPortVolumeFetcher().fetch(port_code="port1201", max_requests=65)
        assert calls == []

    def test_selected_chokepoint_needs_two_request_budget_before_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(portwatch, "_http_get", lambda *args: calls.append(args))
        with pytest.raises(ValidationError):
            ImfChokePointVolumeFetcher().fetch(chokepoint="suez_canal", max_requests=1)
        assert calls == []


class TestTypedPortWatchFetchers:
    def test_chokepoint_metadata_is_typed_and_source_counts_are_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _install_pages(
            monkeypatch,
            [(200, _page(_feature(_chokepoint_info())))],
        )
        rows = _fetch(ImfChokePointInfoFetcher())
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, ImfMaritimeChokePointInfo)
        assert row.chokepoint_code == "chokepoint1"
        assert row.vessel_count_total == 19787
        assert row.industry_top2 is None
        assert _page_requests(requests)[0][1]["outFields"].startswith("portid,portname,lat,lon")

    def test_chokepoint_daily_query_resolves_name_and_preserves_metric_tons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _install_pages(
            monkeypatch,
            [
                (200, _page(_feature({"portid": "chokepoint1", "portname": "Suez Canal"}))),
                (200, _page(_feature(_chokepoint_volume()))),
            ],
        )
        rows = _fetch(
            ImfChokePointVolumeFetcher(),
            chokepoint="suez_canal",
            start_date="2024-03-01",
            end_date="2024-03-31",
        )
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, ImfMaritimeChokePointVolume)
        assert row.date.isoformat() == "2024-03-04"
        assert row.vessels_total == 38
        assert row.capacity_total == 134850.0
        pages = _page_requests(requests)
        assert "portid = 'chokepoint1'" in pages[1][1]["where"]
        assert "date >= DATE '2024-03-01'" in pages[1][1]["where"]
        assert "date <= DATE '2024-03-31'" in pages[1][1]["where"]
        assert all("resultOffset" not in params for _, params in pages)

    @pytest.mark.parametrize(
        ("name", "source_name", "source_id"),
        (
            ("balabac_strait", "Balabac Strait", "chokepoint25"),
            ("bering_strait", "Bering Strait", "chokepoint26"),
            ("mindoro_strait", "Mindoro Strait", "chokepoint27"),
            ("kerch_strait", "Kerch Strait", "chokepoint28"),
        ),
    )
    def test_all_official_chokepoint_names_resolve_to_source_ids(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        source_name: str,
        source_id: str,
    ) -> None:
        daily = {**_chokepoint_volume(), "portid": source_id, "portname": source_name}
        requests = _install_pages(
            monkeypatch,
            [
                (
                    200,
                    _page(_feature({"portid": source_id, "portname": source_name})),
                ),
                (200, _page(_feature(daily))),
            ],
        )
        rows = _fetch(ImfChokePointVolumeFetcher(), chokepoint=name)
        assert len(rows) == 1
        pages = _page_requests(requests)
        assert f"portid = '{source_id}'" in pages[1][1]["where"]

    def test_port_metadata_applies_country_filter_and_rank_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attrs = _port_info()
        second = {
            **attrs,
            "portid": "port1202",
            "portname": "Ningbo",
            "vessel_count_total": 18000,
            "vessel_count_container": 7400,
        }
        requests = _install_pages(
            monkeypatch,
            [(200, _page(_feature(attrs), _feature(second)))],
        )
        rows = _fetch(ImfPortInfoFetcher(), country="chn", limit=1)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, ImfPortInfo)
        assert row.port_name == "Shanghai"
        assert row.country_code == "CHN"
        assert row.vessel_count_tanker == 1550
        pages = _page_requests(requests)
        assert pages[0][1]["where"] == "ISO3 = 'CHN'"
        assert pages[0][1]["orderByFields"] == "ObjectId ASC"
        assert pages[0][1]["resultRecordCount"] == "2"

    def test_port_continent_label_and_combined_country_filter_match_official_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _install_pages(monkeypatch, [(200, _page(_feature(_port_info())))])
        rows = _fetch(ImfPortInfoFetcher(), country="chn", continent="asia_pacific")
        assert rows[0].continent == "Asia & Pacific"
        assert _page_requests(requests)[0][1]["where"] == (
            "ISO3 = 'CHN' AND continent = 'Asia & Pacific'"
        )

    def test_port_volume_maps_all_daily_calls_and_tons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _install_pages(
            monkeypatch,
            [(200, _page(_feature(_port_volume())))],
        )
        rows = _fetch(
            ImfPortVolumeFetcher(),
            port_code="port1201",
            start_date="2024-03-04",
            end_date="2024-03-04",
        )
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, ImfPortVolume)
        assert row.date.isoformat() == "2024-03-04"
        assert row.country_code == "CHN"
        assert row.portcalls == 80
        assert row.portcalls_cargo == 69
        assert row.imports == 8320.0
        assert row.exports == 8640.0
        assert row.imports_cargo_component_delta_metric_tons == 0.0
        assert row.imports_total_component_delta_metric_tons == 0.0
        assert row.exports_cargo_component_delta_metric_tons == 0.0
        assert row.exports_total_component_delta_metric_tons == 0.0
        assert row.imports_cargo_components_consistent is True
        assert row.imports_total_components_consistent is True
        assert row.exports_cargo_components_consistent is True
        assert row.exports_total_components_consistent is True
        assert "portcalls_cargo" in _page_requests(requests)[0][1]["outFields"]
        assert "portid = 'port1201'" in _page_requests(requests)[0][1]["where"]

    def test_port_volume_official_source_row_maps_exact_call_counts_and_tonnage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Source capture SHA-256: 2969ffde68f29b72d8953d66c300bcdcec7605c7b2c9aa391293ef40026dcd39.
        _install_pages(
            monkeypatch,
            [(200, _page(_feature(_official_port_volume_2019_01_03())))],
        )
        rows = _fetch(
            ImfPortVolumeFetcher(),
            port_code="port1325",
            start_date="2019-01-03",
            end_date="2019-01-03",
        )

        assert len(rows) == 1
        row = rows[0]
        assert (row.port_code, row.date.isoformat(), row.country_code) == (
            "port1325",
            "2019-01-03",
            "JPN",
        )
        assert row.portcalls_cargo == row.portcalls == row.portcalls_container == 1
        assert row.imports == row.imports_cargo == row.imports_container == 439.0
        assert row.exports == row.exports_cargo == 0.0
        assert row.imports_cargo_component_delta_metric_tons == 0.0
        assert row.imports_total_component_delta_metric_tons == 0.0
        assert row.exports_cargo_component_delta_metric_tons == 0.0
        assert row.exports_total_component_delta_metric_tons == 0.0
        assert all(
            (
                row.imports_cargo_components_consistent,
                row.imports_total_components_consistent,
                row.exports_cargo_components_consistent,
                row.exports_total_components_consistent,
            )
        )

    def test_port_volume_tonnage_counterfacts_are_preserved_with_exact_diagnostics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_row = {
            **_official_port_volume_2019_01_03(),
            "import_cargo": 440,
            "import": 450,
            "export_cargo": 100,
            "export": 1000,
        }
        _install_pages(monkeypatch, [(200, _page(_feature(source_row)))])

        row = _fetch(
            ImfPortVolumeFetcher(),
            port_code="port1325",
            start_date="2019-01-03",
            end_date="2019-01-03",
        )[0]

        assert row.imports_cargo == 440.0
        assert row.imports == 450.0
        assert row.exports_cargo == 100.0
        assert row.exports == 1000.0
        assert row.imports_cargo_component_delta_metric_tons == 1.0
        assert row.imports_total_component_delta_metric_tons == 11.0
        assert row.exports_cargo_component_delta_metric_tons == 100.0
        assert row.exports_total_component_delta_metric_tons == 1000.0
        assert row.imports_cargo_components_consistent is False
        assert row.imports_total_components_consistent is False
        assert row.exports_cargo_components_consistent is False
        assert row.exports_total_components_consistent is False

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("portcalls_cargo", 2),
            ("portcalls", 2),
            ("portcalls_cargo", None),
        ),
    )
    def test_port_volume_rejects_missing_or_inconsistent_exact_call_counts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        attributes = {**_official_port_volume_2019_01_03(), field: value}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1325")

        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_volume_rejects_missing_portcalls_cargo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attributes = _official_port_volume_2019_01_03()
        del attributes["portcalls_cargo"]
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1325")

        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_volume_rejects_negative_count_even_when_source_sums_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attributes = {
            **_official_port_volume_2019_01_03(),
            "portcalls_container": -1,
            "portcalls_cargo": -1,
            "portcalls": -1,
        }
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1325")

        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("import", -1),
            ("import_cargo", 1.5),
            ("export_container", float("inf")),
        ),
    )
    def test_port_volume_rejects_invalid_metric_ton_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        attributes = {**_official_port_volume_2019_01_03(), field: value}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1325")

        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_volume_missing_metric_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attributes = _port_volume()
        del attributes["import"]
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1201")
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    @pytest.mark.parametrize(
        "field,value",
        (("ISO3", "CH"), ("import", 10.5)),
    )
    def test_port_volume_rejects_invalid_country_identity_or_fractional_source_volume(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        attributes = {**_port_volume(), field: value}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1201")
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_volume_rate_limit_is_not_returned_as_empty_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pages(monkeypatch, [(429, "rate limited")], counts=[1])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortVolumeFetcher(), port_code="port1201")
        assert error.value.code == "IMF_PORTWATCH_HTTP_ERROR"
        assert error.value.status == 429


class TestPaginationAndFailures:
    def test_keyset_paging_returns_all_rows_and_never_silently_truncates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portwatch, "PORTWATCH_PAGE_SIZE", 2)
        first = _port_info()
        second = {
            **first,
            "portid": "port1202",
            "vessel_count_total": 20000,
            "vessel_count_container": 9400,
        }
        third = {
            **first,
            "portid": "port1203",
            "vessel_count_total": 19000,
            "vessel_count_container": 8400,
        }
        pages = [
            (
                200,
                _page(
                    _feature({**first, "ObjectId": 10}),
                    _feature({**second, "ObjectId": 20}),
                    exceeded=True,
                ),
            ),
            (200, _page(_feature({**third, "ObjectId": 30}))),
        ]
        requests = _install_pages(monkeypatch, pages)
        rows = _fetch(ImfPortInfoFetcher())
        assert len(rows) == 3
        page_queries = _page_requests(requests)
        assert [params["where"] for _, params in page_queries] == [
            "1=1",
            "(1=1) AND ObjectId > 20",
        ]
        assert all(params["orderByFields"] == "ObjectId ASC" for _, params in page_queries)
        assert all("resultOffset" not in params for _, params in page_queries)

    def test_port_catalog_reads_all_2065_rows_with_three_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portwatch, "PORTWATCH_PAGE_SIZE", 1000)
        features = [
            _feature({**_port_info(), "portid": f"port{i:04d}", "ObjectId": i})
            for i in range(1, 2066)
        ]
        pages = [
            (200, _page(*features[0:1000], exceeded=True)),
            (200, _page(*features[1000:2000], exceeded=True)),
            (200, _page(*features[2000:2065])),
        ]
        requests = _install_pages(monkeypatch, pages, counts=[2065, 2065])

        rows = _fetch(ImfPortInfoFetcher())

        page_queries = _page_requests(requests)
        assert len(rows) == 2065
        assert len(page_queries) == 3
        assert len(requests) == 5  # before count + 3 pages + after count
        assert [params["where"] for _, params in page_queries] == [
            "1=1",
            "(1=1) AND ObjectId > 1000",
            "(1=1) AND ObjectId > 2000",
        ]

    def test_port_limit_ranks_after_complete_keyset_traversal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portwatch, "PORTWATCH_PAGE_SIZE", 1)
        lower = {
            **_port_info(),
            "ObjectId": 10,
            "portid": "lower",
            "vessel_count_total": 18000,
            "vessel_count_container": 7400,
        }
        higher = {**_port_info(), "ObjectId": 20, "portid": "higher"}
        requests = _install_pages(
            monkeypatch,
            [
                (200, _page(_feature(lower), exceeded=True)),
                (200, _page(_feature(higher))),
            ],
            counts=[2, 2],
        )

        rows = _fetch(ImfPortInfoFetcher(), limit=1)

        assert len(rows) == 1
        assert rows[0].port_code == "higher"
        assert len(_page_requests(requests)) == 2

    def test_duplicate_object_id_across_pages_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portwatch, "PORTWATCH_PAGE_SIZE", 1)
        duplicated = {**_port_info(), "ObjectId": 7}
        _install_pages(
            monkeypatch,
            [
                (200, _page(_feature(duplicated), exceeded=True)),
                (200, _page(_feature(duplicated))),
            ],
            counts=[2],
        )
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_RESPONSE"

    def test_empty_page_with_exceeded_transfer_limit_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pages(monkeypatch, [(200, _page(exceeded=True))], counts=[1])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_RESPONSE"

    def test_page_without_object_id_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attributes = _port_info()
        _install_pages(
            monkeypatch,
            [(200, _page({"attributes": attributes}))],
            counts=[1],
        )
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_RESPONSE"

    def test_source_count_change_fails_closed_after_reading_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pages(
            monkeypatch,
            [(200, _page(_feature(_port_info())))],
            counts=[1, 2],
        )
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_SOURCE_CHANGED"
        assert error.value.incomplete is True

    def test_pagination_budget_exhaustion_raises_incomplete_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pages(monkeypatch, [], counts=[65000])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher(), max_requests=64)
        assert error.value.code == "IMF_PORTWATCH_REQUEST_BUDGET_EXCEEDED"
        assert error.value.incomplete is True

    def test_http_429_on_later_page_returns_no_partial_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(portwatch, "PORTWATCH_PAGE_SIZE", 1)
        _install_pages(
            monkeypatch,
            [
                (200, _page(_feature(_port_info()), exceeded=True)),
                (429, "rate limit"),
            ],
            counts=[2],
        )
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_HTTP_ERROR"
        assert error.value.status == 429
        assert error.value.incomplete is False

    def test_bad_response_shape_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_pages(monkeypatch, [(200, json.dumps({"items": []}))], counts=[1])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_RESPONSE"

    def test_bad_period_or_missing_source_field_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_date = {**_chokepoint_volume(), "date": "not-a-date"}
        _install_pages(monkeypatch, [(200, _page(_feature(bad_date)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointVolumeFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_daily_chokepoint_source_date_is_an_utc_calendar_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pages(monkeypatch, [(200, _page(_feature(_chokepoint_volume())))])
        row = _fetch(ImfChokePointVolumeFetcher())[0]
        assert row.date.isoformat() == "2024-03-04"

    def test_official_suez_capacity_aggregates_are_preserved_with_quality_deltas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Captured source page SHA-256:
        # 23be141171b67cef203e0da99bf24d92b1ac47697fe353bfde4a803870610e3d
        _install_pages(
            monkeypatch,
            [(200, _page(_feature(_official_suez_volume_2021_03_11())))],
        )

        row = _fetch(ImfChokePointVolumeFetcher())[0]

        assert row.capacity_cargo == 1347035.0
        assert row.capacity_total == 2368151.0
        assert row.capacity_cargo_component_delta_metric_tons == 1.0
        assert row.capacity_total_component_delta_metric_tons == 1.0
        assert row.capacity_reported_total_delta_metric_tons == 0.0
        assert row.capacity_cargo_components_consistent is False
        assert row.capacity_total_components_consistent is False
        assert row.capacity_reported_total_consistent is True
        serialized = row.model_dump(mode="json")
        assert serialized["capacity_cargo_component_delta_metric_tons"] == 1.0
        assert serialized["capacity_cargo_components_consistent"] is False

    def test_large_capacity_formula_differences_remain_explicitly_inconsistent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attributes = {
            **_chokepoint_volume(),
            "capacity_cargo": 900000,
            "capacity": 990000,
        }
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        row = _fetch(ImfChokePointVolumeFetcher())[0]

        assert row.capacity_cargo == 900000.0
        assert row.capacity_total == 990000.0
        assert row.capacity_cargo_component_delta_metric_tons == 865150.0
        assert row.capacity_total_component_delta_metric_tons == 855150.0
        assert row.capacity_reported_total_delta_metric_tons == -10000.0
        assert row.capacity_cargo_components_consistent is False
        assert row.capacity_total_components_consistent is False
        assert row.capacity_reported_total_consistent is False

    def test_missing_capacity_tier_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attributes = _chokepoint_volume()
        del attributes["capacity_tanker"]
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])

        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointVolumeFetcher())

        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_non_integral_vessel_average_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_count = {**_chokepoint_info(), "vessel_count_total": 10.5}
        _install_pages(monkeypatch, [(200, _page(_feature(bad_count)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_share_is_preserved_without_an_undocumented_scale_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attributes = {
            **_port_info(),
            "share_country_maritime_import": 123.5,
            "share_country_maritime_export": 0.004,
        }
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        rows = _fetch(ImfPortInfoFetcher())
        assert rows[0].share_country_maritime_import == 123.5
        assert rows[0].share_country_maritime_export == 0.004

    @pytest.mark.parametrize(
        "field,value",
        (
            ("n_cargo", 29),
            ("n_total", 39),
            ("capacity", 134850.5),
            ("capacity_container", -1),
            ("capacity_cargo", float("inf")),
        ),
    )
    def test_chokepoint_volume_rejects_bad_count_sums_or_invalid_capacities(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        attributes = {**_chokepoint_volume(), field: value}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointVolumeFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    @pytest.mark.parametrize(
        "field,value",
        (("vessel_count_total", 19786), ("vessel_count_RoRo", 948)),
    )
    def test_chokepoint_metadata_rejects_bad_aggregate_sum(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        attributes = {**_chokepoint_info(), field: value}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfChokePointInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    @pytest.mark.parametrize(
        "field",
        ("vessel_count_total", "vessel_count_container", "share_country_maritime_import"),
    )
    def test_port_metadata_rejects_missing_required_source_fields(
        self, monkeypatch: pytest.MonkeyPatch, field: str
    ) -> None:
        attributes = _port_info()
        del attributes[field]
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"

    def test_port_metadata_rejects_invalid_iso3_before_typed_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attributes = {**_port_info(), "ISO3": "CH"}
        _install_pages(monkeypatch, [(200, _page(_feature(attributes)))])
        with pytest.raises(ImfPortWatchError) as error:
            _fetch(ImfPortInfoFetcher())
        assert error.value.code == "IMF_PORTWATCH_BAD_OBSERVATION"


class TestOutputContractStrictness:
    def test_contract_models_reject_arbitrary_or_unitless_fields(self) -> None:
        with pytest.raises(ValidationError):
            ImfMaritimeChokePointVolume(
                date="2024-03-04",
                chokepoint="Suez Canal",
                vessels_total=38,
                vessels_cargo=30,
                vessels_tanker=8,
                vessels_container=14,
                vessels_general_cargo=3,
                vessels_dry_bulk=12,
                vessels_roro=1,
                capacity_total=134850.0,
                capacity_cargo=34850.0,
                capacity_tanker=100000.0,
                capacity_container=12000.0,
                capacity_general_cargo=800.0,
                capacity_dry_bulk=22000.0,
                capacity_roro=50.0,
                capacity_unit="kilotons",
            )

    def test_port_volume_contract_rejects_unlabelled_tonnage_units(self) -> None:
        with pytest.raises(ValidationError):
            ImfPortVolume.model_validate(
                {
                    **ImfPortVolume(
                        date="2024-03-04",
                        port_code="port1201",
                        country_code="CHN",
                        portcalls=1,
                        portcalls_cargo=1,
                        portcalls_tanker=0,
                        portcalls_container=1,
                        portcalls_general_cargo=0,
                        portcalls_dry_bulk=0,
                        portcalls_roro=0,
                        imports=1,
                        imports_cargo=1,
                        imports_tanker=0,
                        imports_container=1,
                        imports_general_cargo=0,
                        imports_dry_bulk=0,
                        imports_roro=0,
                        exports=0,
                        exports_cargo=0,
                        exports_tanker=0,
                        exports_container=0,
                        exports_general_cargo=0,
                        exports_dry_bulk=0,
                        exports_roro=0,
                    ).model_dump(),
                    "imports_unit": "kilotons",
                }
            )


@pytest.mark.parametrize(
    ("model", "fetcher_type", "contract", "query", "fixture", "identity_field"),
    (
        (
            "MaritimeChokePointInfo",
            ImfChokePointInfoFetcher,
            ImfMaritimeChokePointInfo,
            {},
            _chokepoint_info(),
            ("chokepoint_code", "chokepoint1"),
        ),
        (
            "MaritimeChokePointVolume",
            ImfChokePointVolumeFetcher,
            ImfMaritimeChokePointVolume,
            {"start_date": "2021-03-11", "end_date": "2021-03-11"},
            _official_suez_volume_2021_03_11(),
            ("chokepoint", "Suez Canal"),
        ),
        (
            "PortInfo",
            ImfPortInfoFetcher,
            ImfPortInfo,
            {"country": "CHN"},
            _port_info(),
            ("country_code", "CHN"),
        ),
        (
            "PortVolume",
            ImfPortVolumeFetcher,
            ImfPortVolume,
            {"port_code": "port1201"},
            {**_port_volume(), "import": 8321},
            ("port_code", "port1201"),
        ),
    ),
)
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    contract: type,
    query: dict[str, object],
    fixture: dict[str, object],
    identity_field: tuple[str, str],
) -> None:
    """The service pins provider identity and serializes each typed contract."""
    fetcher = fetcher_type()
    _install_pages(monkeypatch, [(200, _page(_feature(fixture)))])
    transformed_kwargs: dict[str, object] = {}
    transform_query = fetcher.transform_query

    def _capture_transform_query(**kwargs: object) -> object:
        transformed_kwargs.update(kwargs)
        return transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", _capture_transform_query)
    descriptor = _descriptor(fetcher, model)
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
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="imf",
            model=model,
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_fixture_context(model),
        )
    )

    assert response["source"] == "imf"
    assert response["model"] == model
    assert response["domain"] == fetcher.capability.domain
    assert response["results"]
    assert isinstance(response["results"][0], dict)
    field, expected_value = identity_field
    assert response["results"][0][field] == expected_value
    assert transformed_kwargs["source"] == "imf"
    assert transformed_kwargs["market"] == "global"
    if model == "MaritimeChokePointVolume":
        assert response["results"][0]["capacity_cargo"] == 1347035.0
        assert response["results"][0]["capacity_cargo_component_delta_metric_tons"] == 1.0
        assert response["results"][0]["capacity_cargo_components_consistent"] is False
    if model == "PortVolume":
        assert response["results"][0]["portcalls_cargo"] == 69
        assert response["results"][0]["imports"] == 8321.0
        assert response["results"][0]["imports_total_component_delta_metric_tons"] == 1.0
        assert response["results"][0]["imports_total_components_consistent"] is False


@pytest.mark.parametrize(
    "model_id",
    (
        "AvailableIndicators",
        "ConsumerPriceIndex",
        "DirectionOfTrade",
        "EconomicIndicators",
        "MaritimeChokePointInfo",
        "MaritimeChokePointVolume",
        "PortInfo",
        "PortVolume",
    ),
)
def test_new_imf_capabilities_are_explicit_only(model_id: str) -> None:
    fetcher = IMF_PROVIDER.fetcher_dict[model_id].load("imf")
    capability = fetcher.capability
    assert capability.verified is False
    assert capability.notes == "offline candidate; cross-source validation not completed"

    registry = ProviderRegistry()
    registry.register(fetcher)
    with pytest.raises(LookupError, match="no verified capability"):
        registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
        )
    assert (
        registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
            source="imf",
        )
        is fetcher
    )
