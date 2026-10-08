"""HTTP and service contracts for read-only canonical model metadata."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, get_type_hints

import pytest
import requests
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.provider_models import get_provider_model_registry, router
from opendata.core.database import get_db
from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.registry import ProviderRegistry
from opendata.services import provider_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from typing_extensions import assert_type

    from opendata.services import (
        AkshareProvider,
        DataAcquisitionService,
        DataService,
        ExecutionService,
        ScriptService,
        get_scheduler_service,
        init_scheduler_service,
        task_scheduler,
    )
    from opendata.services.data_acquisition import (
        DataAcquisitionService as DataAcquisitionServiceSource,
    )
    from opendata.services.data_service import DataService as DataServiceSource
    from opendata.services.execution_service import ExecutionService as ExecutionServiceSource
    from opendata.services.scheduler import TaskScheduler
    from opendata.services.scheduler_service import (
        SchedulerService,
    )
    from opendata.services.script_service import ScriptService as ScriptServiceSource

    # data_fetch is deliberately mypy-dark in pyproject.toml; preserve its source Any.
    assert_type(AkshareProvider, Any)
    assert_type(DataAcquisitionService, type[DataAcquisitionServiceSource])
    assert_type(DataService, type[DataServiceSource])
    assert_type(ExecutionService, type[ExecutionServiceSource])
    assert_type(ScriptService, type[ScriptServiceSource])
    assert_type(get_scheduler_service, Callable[[], SchedulerService | None])
    assert_type(init_scheduler_service, Callable[[], SchedulerService])
    assert_type(task_scheduler, TaskScheduler)


def _local_registry() -> ProviderRegistry:
    """Register only the eight canonical model bindings in an isolated registry."""
    from opendata.data.providers.bls.provider import PROVIDER as BLS
    from opendata.data.providers.fmp.provider import PROVIDER as FMP
    from opendata.data.providers.fred.provider import PROVIDER as FRED

    registry = ProviderRegistry()
    for descriptor in (FRED, BLS, FMP):
        for binding in descriptor.fetcher_bindings:
            if binding.canonical_model_ids:
                registry.register_provider_models(
                    descriptor.source,
                    binding.canonical_model_ids,
                    binding.load(descriptor.source),
                )
    return registry


def _client(
    registry: ProviderRegistry,
    *,
    scopes: tuple[str, ...] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = Principal(
        user=cast("Any", SimpleNamespace()),
        api_key_id=41 if scopes is not None else None,
        scopes=scopes,
    )
    app.dependency_overrides[get_provider_model_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def _unauthenticated_client(registry: ProviderRegistry) -> TestClient:
    """Build an API client with auth enabled and a database that cannot do I/O."""
    app = FastAPI()
    app.include_router(router, prefix="/providers")

    async def override_get_db() -> AsyncIterator[_NoDatabaseAccess]:
        yield _NoDatabaseAccess()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_provider_model_registry] = lambda: registry
    return TestClient(app)


class _NoDatabaseAccess:
    """Sentinel that fails if unauthenticated auth performs a database query."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unauthenticated request touched database attribute {name}")


def _block_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("metadata endpoint attempted network I/O")


def test_service_cold_import_does_not_load_configuration_catalog_or_sources() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import socket
        import sys

        blocked = (
            "opendata.core.config",
            "opendata.data.providers.catalog",
            "opendata.data.providers.akshare.models",
            "opendata.data.providers.fred.models",
            "opendata.data.providers.bls.models",
            "opendata.data.providers.fmp.models",
            "opendata.data_fetch.providers.akshare_provider",
        )

        class ImportBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                    raise AssertionError("forbidden import: " + fullname)
                return None

        sys.meta_path.insert(0, ImportBlocker())
        def no_network(*args, **kwargs):
            raise AssertionError("service import attempted network I/O")
        socket.create_connection = no_network

        import opendata.services.provider_models

        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in blocked
            for name in sys.modules
        )
        """
    )
    env = os.environ.copy()
    for key in ("FRED_API_KEY", "BLS_API_KEY", "FMP_API_KEY", "OPENDATA_ALLOW_LIVE_E2E"):
        env.pop(key, None)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline test code
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_services_package_lazy_exports_keep_original_objects_and_unknown_names() -> None:
    script = textwrap.dedent(
        """
        import sys
        import opendata.services as services

        expected = [
            "AkshareProvider", "DataAcquisitionService", "DataService",
            "ExecutionService", "ScriptService", "get_scheduler_service",
            "init_scheduler_service", "task_scheduler",
        ]
        assert services.__all__ == expected
        assert "opendata.core.config" not in sys.modules
        assert "opendata.data_fetch.providers.akshare_provider" not in sys.modules
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider
        from opendata.services.data_acquisition import DataAcquisitionService
        from opendata.services.data_service import DataService
        from opendata.services.execution_service import ExecutionService
        from opendata.services.scheduler import task_scheduler
        from opendata.services.scheduler_service import (
            get_scheduler_service,
            init_scheduler_service,
        )
        from opendata.services.script_service import ScriptService
        import opendata.services.scheduler as scheduler_module
        from opendata.services import scheduler

        originals = {
            "AkshareProvider": AkshareProvider,
            "DataAcquisitionService": DataAcquisitionService,
            "DataService": DataService,
            "ExecutionService": ExecutionService,
            "ScriptService": ScriptService,
            "get_scheduler_service": get_scheduler_service,
            "init_scheduler_service": init_scheduler_service,
            "task_scheduler": task_scheduler,
        }
        assert {name: getattr(services, name) for name in expected} == originals
        assert all(getattr(services, name) is value for name, value in originals.items())
        assert all(getattr(services, name) is value for name, value in originals.items())
        assert scheduler is scheduler_module
        try:
            services.no_such_service_export
        except AttributeError:
            pass
        else:
            raise AssertionError("unknown service export did not raise AttributeError")
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline test code
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_registry_listing_is_exact_and_an_empty_registry_stays_empty() -> None:
    registry = _local_registry()
    with _client(registry) as client:
        response = client.get("/providers/models")
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["success"] is True
        models = envelope["data"]
        assert {(item["source"], item["model"]) for item in models} == {
            ("fred", "FredSearch"),
            ("fred", "FredSeries"),
            ("fred", "SOFR"),
            ("fred", "SONIA"),
            ("bls", "BlsSearch"),
            ("bls", "BlsSeries"),
            ("fmp", "EquityHistorical"),
            ("fmp", "EquityQuote"),
        }
        assert all(item["verified"] is False for item in models)
        assert all(
            item["capability_identity"]["source"] == item["source"]
            and item["capability_identity"]["domain"] == item["domain"]
            for item in models
        )
        for item in models:
            capability = registry.resolve_model(item["source"], item["model"]).capability
            assert item["capability_identity"] == {
                "asset_class": capability.asset_class,
                "domain": capability.domain,
                "period": capability.period,
                "market": capability.market,
                "source": capability.source,
            }
        assert len(client.get("/providers/models?source=fred").json()["data"]) == 4
        assert client.get("/providers/models?source=auto").status_code == 400
        assert client.get("/providers/models?source=bad-source").status_code == 400
        assert client.get("/providers/models?source=unknown").status_code == 404

    with _client(ProviderRegistry()) as client:
        assert client.get("/providers/models").json()["data"] == []


def test_metadata_routes_are_mounted_in_the_shared_api_router() -> None:
    from opendata.api import api_router

    paths = {route.path for route in api_router.routes if isinstance(route, APIRoute)}
    assert "/providers/models" in paths
    assert "/providers/{source}/models/{model}/schema" in paths


@pytest.mark.parametrize(
    ("source", "model"),
    [
        ("fred", "FredSearch"),
        ("fred", "FredSeries"),
        ("fred", "SOFR"),
        ("fred", "SONIA"),
        ("bls", "BlsSearch"),
        ("bls", "BlsSeries"),
        ("fmp", "EquityHistorical"),
        ("fmp", "EquityQuote"),
    ],
)
def test_endpoint_returns_the_registered_query_model_validation_schema(
    source: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    fetcher = registry.resolve_model(source, model)
    query_type = get_type_hints(type(fetcher).transform_query)["return"]
    expected = query_type.model_json_schema(mode="validation")
    capability = fetcher.capability
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(requests.Session, "request", _block_network)

    with _client(registry) as client:
        response = client.get(f"/providers/{source}/models/{model}/schema")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == source
    assert data["model"] == model
    assert data["schema"] == expected
    assert data["verified"] is False
    assert data["capability_identity"] == {
        "asset_class": capability.asset_class,
        "domain": capability.domain,
        "period": capability.period,
        "market": capability.market,
        "source": capability.source,
    }


def test_schema_preserves_fred_vintages_filters_bls_budgets_and_fmp_fixed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FMP_API_KEY", "fake-metadata-test-key")
    monkeypatch.setenv("FRED_API_KEY", "fake-metadata-test-key")
    with _client(_local_registry()) as client:
        fmp_response = client.get("/providers/fmp/models/EquityQuote/schema")
        assert "fake-metadata-test-key" not in fmp_response.text
        fred = client.get("/providers/fred/models/FredSearch/schema").json()["data"]["schema"]
        bls = client.get("/providers/bls/models/BlsSeries/schema").json()["data"]["schema"]
        fmp = fmp_response.json()["data"]["schema"]

    fred_props = fred["properties"]
    assert {"realtime_start", "realtime_end", "filter_variable", "filter_value"} <= set(fred_props)
    assert "search_text" in fred["required"]
    assert fred_props["realtime_start"]["anyOf"][0]["format"] == "date"
    assert fred_props["realtime_end"]["anyOf"][0]["format"] == "date"
    assert fred["additionalProperties"] is False
    filter_variable = fred_props["filter_variable"]
    assert any(
        values == ["frequency", "units", "seasonal_adjustment"]
        for values in _nested_enums(filter_variable)
    )

    bls_props = bls["properties"]
    assert "series_ids" in bls["required"] and "max_requests" in bls["required"]
    assert bls_props["series_ids"]["minItems"] == 1
    assert bls_props["max_requests"]["minimum"] == 1
    assert bls_props["max_requests"]["maximum"] == 500
    assert bls["additionalProperties"] is False

    fmp_props = fmp["properties"]
    assert fmp_props["source"]["const"] == "fmp"
    assert fmp_props["source"]["default"] == "fmp"
    assert fmp_props["market"]["const"] == "us"
    assert "symbol" in fmp["required"]
    assert fmp["additionalProperties"] is False


def _nested_enums(value: object) -> list[list[object]]:
    """Collect enum arrays from a nested Pydantic JSON schema fragment."""
    if isinstance(value, dict):
        result = [value["enum"]] if isinstance(value.get("enum"), list) else []
        for nested in value.values():
            result.extend(_nested_enums(nested))
        return result
    if isinstance(value, list):
        return [enum for nested in value for enum in _nested_enums(nested)]
    return []


def test_unknown_auto_malformed_and_unscoped_schema_requests_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    with _client(registry) as client:
        assert client.get("/providers/auto/models/FredSearch/schema").status_code == 400
        assert client.get("/providers/fred/models/auto/schema").status_code == 400
        assert client.get("/providers/bad-source/models/FredSearch/schema").status_code == 400
        assert client.get("/providers/fred/models/Unknown/schema").status_code == 404
        assert client.get("/providers/unknown/models/FredSearch/schema").status_code == 404

    with _client(registry, scopes=("equity_quote",)) as client:
        visible = client.get("/providers/models").json()["data"]
        assert [(item["source"], item["model"]) for item in visible] == [("fmp", "EquityQuote")]

    def unexpected_schema(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("schema was built before domain authorization")

    monkeypatch.setattr("opendata.api.provider_models.query_schema_for_model", unexpected_schema)
    with _client(registry, scopes=("equity_quote",)) as client:
        response = client.get("/providers/fred/models/FredSeries/schema")
    assert response.status_code == 403


def test_both_metadata_routes_require_authentication_without_database_queries() -> None:
    with _unauthenticated_client(_local_registry()) as client:
        list_response = client.get("/providers/models")
        schema_response = client.get("/providers/fred/models/FredSearch/schema")

    assert list_response.status_code == 401
    assert schema_response.status_code == 401


def test_schema_generation_errors_return_sanitized_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    fetcher = registry.resolve_model("fmp", "EquityQuote")
    query_type = get_type_hints(type(fetcher).transform_query)["return"]

    def fail_schema(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("fake-secret /private/provider/model_module.py")

    monkeypatch.setattr(query_type, "model_json_schema", fail_schema)
    with _client(registry) as client:
        response = client.get("/providers/fmp/models/EquityQuote/schema")

    assert response.status_code == 500
    assert response.json()["detail"] == "Registered provider model schema is unavailable"
    for internal_detail in ("fake-secret", "RuntimeError", "/private/provider/model_module.py"):
        assert internal_detail not in response.text


def test_invalid_query_model_schema_type_fails_closed() -> None:
    class InvalidQueryFetcher(Fetcher[Any, Any]):
        capability = Capability(
            asset_class="test",
            domain="invalid_query_schema",
            period="snapshot",
            market="test",
            source="schema_test",
            verified=False,
        )

        def transform_query(self, **kwargs: object) -> str:
            return str(kwargs)

        def extract_data(self, params: str, ctx: FetchContext) -> object:
            return params

        def transform_data(self, raw: object, params: str) -> FetchResult:
            return ()

    registry = ProviderRegistry()
    fetcher = InvalidQueryFetcher()
    registry.register_provider_models("schema_test", ("InvalidQuery",), fetcher)
    descriptor = provider_models.find_provider_model(registry, "schema_test", "InvalidQuery")
    with pytest.raises(provider_models.ProviderModelSchemaError):
        provider_models.query_schema_for_model(registry, descriptor)
