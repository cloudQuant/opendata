"""Behavior tests for the read-only legacy migration catalog."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from opendata.migration_catalog import (
    _parse,
    _provider_models,
    build_migration_catalog,
    render_json,
    validate_output_path,
)


def _write_fixture(root: Path, relative: str, content: str) -> Path:
    """Write one UTF-8 source fixture beneath a temporary repository root."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_real_opendata_stock_fetcher_is_visible_in_static_registry_crosswalk() -> None:
    """The production stock fetcher is found without importing provider code."""
    models = _provider_models(Path(__file__).resolve().parents[1])
    stock_daily = next(item for item in models if item.domain == "stock_daily")

    assert stock_daily.source == "akshare"
    assert stock_daily.model_class == "AkshareStockDailyFetcher"
    assert stock_daily.registered_by_descriptor is True
    assert "stock_zh_a_hist" in stock_daily.vendor_functions
    assert {"symbol", "start_date", "end_date", "adjust"}.issubset(stock_daily.query_fields)


def test_synthetic_legacy_call_joins_to_registered_fetcher_without_running_code(
    tmp_path: Path,
) -> None:
    """A source call with compatible runtime names becomes only an adapter candidate."""
    legacy_root = tmp_path / "legacy"
    opendata_root = tmp_path / "opendata"
    legacy_source = (
        "import akshare as ak\n\n"
        "\nclass Sample:\n"
        "    def fetch_data(self, symbol, start_date, end_date):\n"
        "        return ak.stock_zh_a_hist(\n"
        '            symbol=symbol, start_date=start_date, end_date=end_date, adjust=""\n'
        "        )\n\n"
        "    def fetch_legacy(self, **kwargs):\n"
        '        return self.fetch_ak_data("stock_zh_a_hist", **kwargs)\n\n'
        "    def save(self, frame):\n"
        '        return frame.to_sql("bars", connection)\n'
    )
    script = _write_fixture(
        legacy_root,
        "src/backend/app/data_fetch/scripts/stocks/daily/sample.py",
        legacy_source,
    )
    malformed = _write_fixture(
        legacy_root,
        "src/backend/app/data_fetch/scripts/common/broken.py",
        "def fetch_data(:\n    raise RuntimeError('must not execute')\n",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/_source.py",
        'SOURCE = "akshare"\n',
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/provider.py",
        """from opendata.data.provider import LazyFetcherBinding, Provider
PROVIDER = Provider(
    source="akshare",
    fetcher_bindings=(LazyFetcherBinding(
        "opendata.data.providers.akshare.models.stock_daily",
        "AkshareStockDailyFetcher",
    ),),
)
""",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/models/stock_daily.py",
        """from opendata.data.capability import Capability
from opendata.data.protocol import Fetcher, QueryParams
import opendata.data.providers.akshare._vendor as opendata_http
class StockDailyQuery(QueryParams):
    adjust: str = ""
class AkshareStockDailyFetcher(Fetcher):
    capability = Capability(
        source="akshare", domain="stock_daily", asset_class="equity",
        period="1D", market="cn",
    )
    def extract_data(self, params):
        return opendata_http.stock_zh_a_hist(
            symbol=params.symbol, start_date=params.start_date,
            end_date=params.end_date, adjust=params.adjust,
        )
""",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/_vendor/__init__.py",
        "_EXPORTS = {'stock_zh_a_hist': ('vendor.stock', 'stock_zh_a_hist', False)}\n",
    )
    baseline = legacy_root / "manifest.csv"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    with baseline.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source_path", "source_sha256"])
        writer.writeheader()
        writer.writerow(
            {
                "source_path": "src/backend/app/data_fetch/scripts/stocks/daily/sample.py",
                "source_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }
        )
        writer.writerow(
            {
                "source_path": "src/backend/app/data_fetch/scripts/common/broken.py",
                "source_sha256": hashlib.sha256(malformed.read_bytes()).hexdigest(),
            }
        )

    catalog = build_migration_catalog(legacy_root, opendata_root, baseline_path=baseline)
    record = next(item for item in catalog.files if item.source_path.endswith("sample.py"))
    malformed_record = next(
        item for item in catalog.files if item.source_path.endswith("broken.py")
    )

    assert catalog.summary()["legacy_script_count"] == 2
    assert record.status == "REQUIRES_MANUAL"
    assert record.baseline_hash_match is True
    assert record.calls[0].target_models == ["akshare:stock_daily:AkshareStockDailyFetcher"]
    assert record.calls[0].unresolved_runtime_parameters == []
    assert record.calls[1].call_style == "shared_writer_string_dispatch"
    assert record.calls[1].function == "stock_zh_a_hist"
    assert record.calls[1].unresolved_runtime_parameters == ["**kwargs"]
    assert record.runtime_verification == "NOT_RUN"
    assert record.write_sinks == ["frame.to_sql"]
    assert record.task_reference_paths == []
    assert malformed_record.source_sha256 == hashlib.sha256(malformed.read_bytes()).hexdigest()
    assert malformed_record.parse_error.startswith("syntax error")
    assert malformed_record.status == "REQUIRES_MANUAL"
    endpoint = next(
        item
        for item in catalog.endpoint_catalog()
        if item["source_root"] == "akshare" and item["function"] == "stock_zh_a_hist"
    )
    assert endpoint["status"] == "REQUIRES_MANUAL"
    assert endpoint["runtime_verification"] == "NOT_RUN"
    assert endpoint["source_scripts"][0]["source_sha256"] == record.source_sha256
    assert "endpoint and query-parameter names only" in endpoint["readiness_scope"]
    rendered = render_json(catalog)
    assert json.loads(rendered)["files"][0]["source_sha256"]
    assert '\n    {"baseline_hash_match"' in rendered
    assert "does not mean a script" in catalog.to_dict()["verification_notice"]


def test_malformed_python_is_hashed_and_retained_for_manual_review(tmp_path: Path) -> None:
    """Syntax failures remain visible as source records and are never executed."""
    source = tmp_path / "broken.py"
    source.write_bytes(b"def fetch_data(:\n    raise RuntimeError('must not execute')\n")

    tree, source_hash, error = _parse(source)

    assert tree is None
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "syntax error" in error


def test_output_validation_rejects_input_alias_and_silent_overwrite(tmp_path: Path) -> None:
    """The CLI cannot replace the scanned source or an existing output silently."""
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    input_file = legacy_root / "input.py"
    input_file.write_text("pass\n", encoding="utf-8")
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    existing_output = output_dir / "catalog.json"
    existing_output.write_text("{}\n", encoding="utf-8")

    assert validate_output_path(
        input_file,
        legacy_root=legacy_root,
        input_files={input_file},
    ) == (None, "output_inside_legacy_source_tree")
    assert validate_output_path(
        existing_output,
        legacy_root=legacy_root,
        input_files={input_file},
    ) == (None, "output_exists_requires_overwrite")
    protected_root = tmp_path / "opendata" / "opendata" / "data" / "providers"
    protected_output = protected_root / "catalog.json"
    assert validate_output_path(
        protected_output,
        legacy_root=legacy_root,
        input_files={input_file},
        protected_roots={protected_root},
    ) == (None, "output_inside_opendata_source_tree")
    assert validate_output_path(
        output_dir / ".env.backup",
        legacy_root=legacy_root,
        input_files={input_file},
    ) == (None, "environment_file_output_refused")


def test_catalog_includes_external_trigger_paths_and_keeps_unknowns_open(tmp_path: Path) -> None:
    """A systemd reference joins to the full script path without claiming runtime proof."""
    legacy_root = tmp_path / "legacy"
    opendata_root = tmp_path / "opendata"
    legacy_source = (
        "import akshare as ak\n"
        "def fetch_data(symbol):\n"
        "    return ak.stock_zh_a_hist(symbol=symbol)\n"
    )
    script = _write_fixture(
        legacy_root,
        "src/backend/app/data_fetch/scripts/stocks/daily/job.py",
        legacy_source,
    )
    _write_fixture(
        legacy_root,
        "deploy/systemd/sample.service.example",
        "ExecStart=/usr/bin/python src/backend/app/data_fetch/scripts/stocks/daily/job.py\n",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/_vendor/__init__.py",
        "_EXPORTS = {'stock_zh_a_hist': ('vendor.stock', 'stock_zh_a_hist', False)}\n",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/provider.py",
        """PROVIDER = Provider(
    source="akshare",
    fetcher_bindings=(LazyFetcherBinding(
        "opendata.data.providers.akshare.models.stock_daily",
        "AkshareStockDailyFetcher",
    ),),
)
""",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/models/stock_daily.py",
        """import opendata.data.providers.akshare._vendor as opendata_http
class StockDailyQuery(QueryParams):
    pass
class AkshareStockDailyFetcher:
    capability = Capability(
        source="akshare", domain="stock_daily", asset_class="equity",
        period="1D", market="cn",
    )
    def extract_data(self, params):
        return opendata_http.stock_zh_a_hist(symbol=params.symbol)
""",
    )
    _write_fixture(
        opendata_root,
        "opendata/data/providers/akshare/_source.py",
        'SOURCE = "akshare"\n',
    )
    baseline = legacy_root / "baseline.csv"
    with baseline.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source_path", "source_sha256"])
        writer.writeheader()
        writer.writerow(
            {
                "source_path": "src/backend/app/data_fetch/scripts/stocks/daily/job.py",
                "source_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }
        )

    catalog = build_migration_catalog(legacy_root, opendata_root, baseline_path=baseline)

    assert len(catalog.trigger_references) == 1
    assert catalog.trigger_references[0].target_script_paths == [
        "src/backend/app/data_fetch/scripts/stocks/daily/job.py"
    ]
    assert catalog.runtime_task_evidence[0]["status"] == "DOCUMENTARY_SNAPSHOT_NOT_FOUND"
    assert catalog.files[0].status == "READY_FOR_ADAPTER"
    assert "sink" in catalog.limitations[1]
