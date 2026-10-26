"""Behavior tests for the read-only legacy migration catalog."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import runpy
import sys
from pathlib import Path

import pytest

from opendata.migration_catalog import (
    LegacyCall,
    LegacyFile,
    MigrationCatalog,
    _collect_legacy_calls,
    _load_baseline,
    _parse,
    _provider_crosswalk,
    _provider_descriptors,
    _provider_imports,
    _provider_models,
    _vendor_exports,
    _walk_python,
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
    assert endpoint["call_count"] == 2
    assert endpoint["script_count"] == 1
    assert endpoint["source_scripts"][0]["source_sha256"] == record.source_sha256
    assert len(endpoint["source_scripts"][0]["call_lines"]) == 2
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


def test_provider_call_scanner_redacts_secrets_and_finds_dynamic_sources_and_sinks() -> None:
    """AST inventory retains endpoint names but never serializes secret argument names."""
    source = ast.parse(
        """import akshare as market_api
from akshare import stock_zh_a_hist as stock_history
import importlib
def collect(symbol, token_value):
    market_api.stock_zh_a_hist(symbol=symbol, api_key=token_value, adjust=["qfq"])
    stock_history(symbol, end_date=make_date())
    getattr(market_api, "stock_zh_a_hist")(symbol=symbol)
    getattr(market_api, endpoint_name)
    importlib.import_module("akshare.stock.cons")
    importlib.import_module(module_name)
    fetch_ak_data(endpoint_name, symbol=symbol)
    fetch_ak_data("stock_zh_a_hist", symbol=symbol, password=token_value)
    frame.to_sql("bars", connection)
    writer.save(frame)
    writer.save_data(frame)
    writer.create_table_if_not_exists("bars")
class Writer(AkshareToMySql):
    pass
"""
    )

    aliases, roots, dynamic_imports = _provider_imports(source)
    calls, dynamic_sources, write_sinks, unknown_sinks = _collect_legacy_calls(
        source, aliases, {"stock_zh_a_hist"}
    )

    assert "akshare" in roots
    assert "constant_dynamic_import:akshare.stock.cons" in dynamic_imports
    assert any(item.startswith("dynamic_import_at_line:") for item in dynamic_imports)
    assert any(
        item.startswith("dynamic_shared_writer_endpoint_at_line:") for item in dynamic_sources
    )
    assert any(item.startswith("dynamic_endpoint_at_line:") for item in dynamic_sources)
    assert {call.call_style for call in calls} >= {
        "qualified_import",
        "direct_import",
        "getattr_constant",
        "shared_writer_string_dispatch",
    }
    assert all("token_value" not in arg.name for call in calls for arg in call.arguments)
    assert any(arg.name == "[redacted-name]" for call in calls for arg in call.arguments)
    assert {"frame.to_sql", "writer.save_data", "writer.create_table_if_not_exists"}.issubset(
        write_sinks
    )
    assert "writer.save" in unknown_sinks
    assert "legacy_shared_writer_subclass:Writer" in write_sinks


def test_baseline_and_source_walker_preserve_malformed_metadata_and_symlink_evidence(
    tmp_path: Path,
) -> None:
    """Invalid manifests and skipped paths have explicit, non-success outcomes."""
    assert _load_baseline(None) == ({}, "NOT_PROVIDED", None)
    invalid_columns = tmp_path / "bad.csv"
    invalid_columns.write_text("path,hash\na.py,abc\n", encoding="utf-8")
    assert _load_baseline(invalid_columns) == ({}, "INVALID_COLUMNS", None)

    valid = tmp_path / "manifest.csv"
    valid.write_text(
        "source_path,source_sha256\n"
        + "ok.py,"
        + "a" * 64
        + "\n"
        + "ignored.py,not-a-hash\n"
        + ","
        + "b" * 64
        + "\n",
        encoding="utf-8",
    )
    entries, status, count = _load_baseline(valid)
    assert (status, count) == ("LOADED", 1)
    assert entries == {"ok.py": "a" * 64}
    env_file = tmp_path / ".env.local"
    env_file.write_text("ignored", encoding="utf-8")
    assert _load_baseline(env_file)[1] == "REFUSED_ENVIRONMENT_FILE"
    assert _load_baseline(tmp_path / "missing.csv")[1] == "ERROR:FileNotFoundError"

    scripts = tmp_path / "scripts"
    _write_fixture(tmp_path, "scripts/__init__.py", "pass\n")
    _write_fixture(tmp_path, "scripts/job.py", "pass\n")
    _write_fixture(tmp_path, "scripts/.git/hidden.py", "pass\n")
    _write_fixture(tmp_path, "scripts/node_modules/hidden.py", "pass\n")
    external = _write_fixture(tmp_path, "external.py", "pass\n")
    (scripts / "linked.py").symlink_to(external)
    paths, errors = _walk_python(scripts)
    assert [path.name for path in paths] == ["job.py"]
    assert errors == [{"path": (scripts / "linked.py").as_posix(), "error": "symlink_skipped"}]
    assert _walk_python(tmp_path / "does-not-exist")[1][0]["error"] == "directory_missing"
    assert {path.name for path in _walk_python(scripts, exclude_init=False)[0]} == {
        "__init__.py",
        "job.py",
    }


def test_parse_reports_decode_and_filesystem_failures(tmp_path: Path) -> None:
    """Decode and filesystem failures retain their distinct diagnostics and hashes."""
    assert _parse(tmp_path / "missing.py") == (None, "", "read error: FileNotFoundError")
    invalid_bytes = tmp_path / "invalid.py"
    invalid_bytes.write_bytes(b"# coding: utf-8\n\xff\n")
    tree, digest, error = _parse(invalid_bytes)
    assert tree is None
    assert digest == hashlib.sha256(invalid_bytes.read_bytes()).hexdigest()
    assert error == "decode error: UnicodeDecodeError"

    nested_source = "import akshare as ak\ndef main():\n"
    nested_source += "".join("    " * (level + 1) + "if True:\n" for level in range(40))
    nested_source += "".join("    " * (level + 2) for level in range(40))
    nested_source += "return ak.stock_zh_a_hist(symbol='000001')\n"
    nested = tmp_path / "deep.py"
    nested.write_text(nested_source, encoding="utf-8")
    tree, _, error = _parse(nested)
    assert tree is not None
    assert error == ""
    aliases, _, _ = _provider_imports(tree)
    calls, _, _, _ = _collect_legacy_calls(tree, aliases, set())
    assert [item.function for item in calls] == ["stock_zh_a_hist"]


def test_vendor_exports_and_provider_registry_metadata_are_static_and_tolerant(
    tmp_path: Path,
) -> None:
    """Provider descriptors are AST parsed, with source and export metadata cross-joined."""
    _write_fixture(
        tmp_path,
        "opendata/data/providers/akshare/provider.py",
        """PROVIDER = Provider(
    fetcher_bindings=(LazyFetcherBinding(
        "opendata.data.providers.akshare.models.stock_daily", "StockFetcher"
    ),),
)
""",
    )
    _write_fixture(
        tmp_path,
        "opendata/data/providers/akshare/_source.py",
        'SOURCE: str = "akshare"\n',
    )
    _write_fixture(
        tmp_path,
        "opendata/data/providers/akshare/models/stock_daily.py",
        """from opendata.data.providers.akshare._vendor import stock_zh_a_hist
class StockQuery:
    adjust: str = ""
class StockFetcher:
    capability = Capability(
        source=SOURCE, domain="daily", asset_class="equity", period="1D", market="cn"
    )
    def fetch(self, params):
        return stock_zh_a_hist(symbol=params.symbol)
""",
    )
    vendor = _write_fixture(
        tmp_path,
        "opendata/data/providers/akshare/_vendor/__init__.py",
        "_EXPORTS = {'stock_zh_a_hist': ('vendor.stock', 'stock_zh_a_hist', False)}\n",
    )
    _write_fixture(tmp_path, "opendata/data/providers/akshare/_vendor/broken.py", "def broken(:\n")

    assert _provider_descriptors(tmp_path) == {
        (
            "akshare",
            "opendata.data.providers.akshare.models.stock_daily",
            "StockFetcher",
        )
    }
    models = _provider_models(tmp_path)
    assert len(models) == 1
    assert models[0].registered_by_descriptor is True
    assert models[0].domain == "daily"
    assert models[0].query_fields == [
        "adjust",
        "end_date",
        "market",
        "source",
        "start_date",
        "symbol",
    ]
    assert models[0].vendor_functions == ["stock_zh_a_hist"]
    assert _vendor_exports(tmp_path) == {"stock_zh_a_hist"}

    vendor.write_text("_EXPORTS = build_exports()\n", encoding="utf-8")
    assert _vendor_exports(tmp_path) == set()
    _write_fixture(
        tmp_path,
        "opendata/data/providers/custom/models/unknown.py",
        'class UnknownFetcher:\n    capability = Capability(source="custom")\n',
    )
    unknown = next(item for item in _provider_models(tmp_path) if item.source == "custom")
    assert unknown.registered_by_descriptor is False
    assert unknown.domain == unknown.asset_class == unknown.period == unknown.market == "UNKNOWN"
    assert unknown.query_fields == ["end_date", "market", "source", "start_date", "symbol"]


def test_crosswalk_requires_export_evidence_before_saying_missing_provider() -> None:
    """A vendor API with no registered Fetcher differs from an unconfirmed source call."""
    known_missing = LegacyFile(
        "src/backend/app/data_fetch/scripts/a.py",
        "a" * 64,
        None,
        None,
        calls=[
            LegacyCall(
                "akshare",
                "akshare",
                "stock_historical_api",
                1,
                "qualified_import",
                vendor_exported=True,
                classification="EXPORTED_SOURCE_ENDPOINT_WITHOUT_FETCHER",
            )
        ],
    )
    unconfirmed = LegacyFile(
        "src/backend/app/data_fetch/scripts/b.py",
        "b" * 64,
        None,
        None,
        calls=[
            LegacyCall(
                "akshare",
                "akshare",
                "not_in_vendor_export",
                1,
                "qualified_import",
                vendor_exported=False,
                classification="SOURCE_CALL_WITHOUT_FETCHER_MATCH",
            )
        ],
    )
    mixed = LegacyFile(
        "src/backend/app/data_fetch/scripts/mixed.py",
        "c" * 64,
        None,
        None,
        calls=[
            LegacyCall(
                "akshare",
                "akshare",
                "stock_historical_api",
                1,
                "qualified_import",
                vendor_exported=True,
            ),
            LegacyCall(
                "akshare",
                "akshare",
                "unconfirmed_api",
                2,
                "qualified_import",
                vendor_exported=False,
            ),
        ],
    )

    _provider_crosswalk(known_missing, [], {"stock_historical_api"})
    _provider_crosswalk(unconfirmed, [], {"stock_historical_api"})
    _provider_crosswalk(mixed, [], {"stock_historical_api"})

    assert known_missing.status == "MISSING_PROVIDER"
    assert "no_registered_opendata_fetcher_for_endpoint" in known_missing.manual_reasons
    assert unconfirmed.status == "REQUIRES_MANUAL"
    assert "source_endpoint_not_confirmed_in_vendor_export_catalog" in unconfirmed.manual_reasons
    assert mixed.status == "REQUIRES_MANUAL"


def test_endpoint_groups_keep_vendor_and_fetcher_evidence_separate() -> None:
    """Endpoint status and source path/hash join are aggregated independently."""
    ready = LegacyCall(
        "akshare",
        "akshare",
        "ready_api",
        7,
        "qualified_import",
        vendor_exported=True,
        target_models=["akshare:daily:Fetcher"],
        classification="REGISTERED_FETCHER_MATCH",
    )
    vendor_only = LegacyCall(
        "akshare",
        "akshare",
        "vendor_only_api",
        8,
        "qualified_import",
        vendor_exported=True,
        classification="EXPORTED_SOURCE_ENDPOINT_WITHOUT_FETCHER",
    )
    unknown = LegacyCall(
        "akshare",
        "akshare",
        "unknown_api",
        9,
        "qualified_import",
        vendor_exported=False,
        classification="SOURCE_CALL_WITHOUT_FETCHER_MATCH",
    )
    catalog = MigrationCatalog(
        "1.0",
        "/legacy",
        "/opendata",
        None,
        "LOADED",
        3,
        files=[
            LegacyFile("a.py", "a" * 64, None, None, status="READY_FOR_ADAPTER", calls=[ready]),
            LegacyFile(
                "b.py", "b" * 64, None, None, status="MISSING_PROVIDER", calls=[vendor_only]
            ),
            LegacyFile("c.py", "c" * 64, None, None, status="REQUIRES_MANUAL", calls=[unknown]),
        ],
    )

    grouped = {item["function"]: item for item in catalog.endpoint_catalog()}
    assert grouped["ready_api"]["status"] == "READY_FOR_ADAPTER"
    assert grouped["vendor_only_api"]["status"] == "MISSING_PROVIDER"
    assert grouped["unknown_api"]["status"] == "REQUIRES_MANUAL"
    assert grouped["vendor_only_api"]["vendor_exported"] is True
    assert grouped["vendor_only_api"]["target_models"] == []
    assert grouped["unknown_api"]["vendor_exported"] is False
    assert grouped["ready_api"]["source_scripts"][0]["source_sha256"] == "a" * 64
    assert catalog.summary()["provider_endpoint_status_counts"] == {
        "MISSING_PROVIDER": 1,
        "READY_FOR_ADAPTER": 1,
        "REQUIRES_MANUAL": 1,
    }


def test_catalog_reports_baseline_mismatch_and_historical_evidence_branches(tmp_path: Path) -> None:
    """Hash and row-count disagreements plus documentary runtime evidence stay visible."""
    legacy = tmp_path / "legacy"
    opendata = tmp_path / "opendata"
    script = _write_fixture(
        legacy,
        "src/backend/app/data_fetch/scripts/daily/job.py",
        "def main():\n    return None\n",
    )
    baseline = legacy / "manifest.csv"
    baseline.write_text(
        (
            "source_path,source_sha256\n"
            "src/backend/app/data_fetch/scripts/daily/job.py," + "0" * 64 + "\n"
            "other.py," + "1" * 64 + "\n"
        ),
        encoding="utf-8",
    )
    evidence_path = (
        legacy / "docs/iterations/迭代203-全量数据补齐与持续调度治理/EXECUTION_STATUS.md"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("1,098 active jobs and 5个B23 helper仍inactive\n", encoding="utf-8")

    catalog = build_migration_catalog(legacy, opendata, baseline_path=baseline)

    assert catalog.baseline_status == "LOADED"
    assert catalog.files[0].baseline_hash_match is False
    assert any("baseline_count_mismatch" in item["error"] for item in catalog.errors)
    assert (
        catalog.runtime_task_evidence[0]["evidence_kind"]
        == "documented_historical_runtime_snapshot"
    )
    assert catalog.runtime_task_evidence[0]["extracted_counts"] == ["1,098", "5"]
    assert script.is_file()


def test_operational_inventory_and_trigger_matching_cover_path_basename_and_class(
    tmp_path: Path,
) -> None:
    """Operational scripts, provider modules, scheduler hints, and references are joined."""
    legacy = tmp_path / "legacy"
    first = _write_fixture(
        legacy,
        "src/backend/app/data_fetch/scripts/daily/one.py",
        "class FirstTask:\n    pass\n",
    )
    second = _write_fixture(
        legacy,
        "src/backend/app/data_fetch/scripts/other/one.py",
        "class SecondTask:\n    pass\n",
    )
    _write_fixture(legacy, "src/backend/app/services/job_scheduler.py", "class Scheduler: pass\n")
    _write_fixture(legacy, "src/backend/app/data_fetch/providers.py", "pass\n")
    _write_fixture(
        legacy, "scripts/ops/run.sh", "python src/backend/app/data_fetch/scripts/daily/one.py\n"
    )
    _write_fixture(legacy, "deploy/systemd/job.service", "ExecStart=python one.py\n# SecondTask\n")
    _write_fixture(legacy, "scripts/ops/ignored.bin", "not searchable\n")
    records = [
        LegacyFile(
            first.relative_to(legacy).as_posix(), "a" * 64, None, None, classes=["FirstTask"]
        ),
        LegacyFile(
            second.relative_to(legacy).as_posix(), "b" * 64, None, None, classes=["SecondTask"]
        ),
    ]
    from opendata.migration_catalog import _auxiliary_inventory, _trigger_references

    auxiliary, searchable, errors = _auxiliary_inventory(legacy)
    triggers = _trigger_references(legacy, records, searchable)

    assert not errors
    assert any(item.role == "scheduler_controller_or_startup" for item in auxiliary)
    assert any(item.role == "data_fetch_provider_or_runtime" for item in auxiliary)
    assert {item.relation for item in triggers} == {"exact_path", "basename_or_class_reference"}
    assert any(
        item.target_script_paths == [records[1].source_path]
        and item.relation == "basename_or_class_reference"
        for item in triggers
    )
    assert records[0].task_reference_paths == ["deploy/systemd/job.service", "scripts/ops/run.sh"]


def test_output_validation_rejects_symlinks_directories_and_path_escape(tmp_path: Path) -> None:
    """Output path guards reject links, missing parents, directories, and input aliases."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    input_file = safe / "input.py"
    input_file.write_text("pass\n", encoding="utf-8")
    link = safe / "catalog-link.json"
    link.symlink_to(input_file)
    assert validate_output_path(link, legacy_root=legacy, input_files=set()) == (
        None,
        "output_symlink_refused",
    )
    assert validate_output_path(
        safe / "missing" / "catalog.json", legacy_root=legacy, input_files=set()
    ) == (None, "output_parent_directory_missing")
    assert validate_output_path(safe, legacy_root=legacy, input_files=set()) == (
        None,
        "output_target_is_directory",
    )
    assert validate_output_path(
        safe / "input.py", legacy_root=legacy, input_files={input_file}
    ) == (None, "output_aliases_input_file")
    traversal_target = legacy / "nested" / ".." / "catalog.json"
    assert validate_output_path(traversal_target, legacy_root=legacy, input_files=set()) == (
        None,
        "output_inside_legacy_source_tree",
    )
    accepted, error = validate_output_path(
        safe / "nested" / ".." / "catalog.json",
        legacy_root=legacy,
        input_files=set(),
        overwrite=True,
    )
    assert accepted == safe / "catalog.json"
    assert error is None


def test_cli_writes_atomically_reports_validation_errors_and_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI output guards and error codes are user-visible and leave existing files untouched."""
    from scripts.ops import build_migration_catalog as cli

    legacy = tmp_path / "legacy"
    opendata = tmp_path / "opendata"
    output = tmp_path / "reports/catalog.json"
    legacy.mkdir()
    output.parent.mkdir()
    catalog = MigrationCatalog("1.0", str(legacy), str(opendata), None, "LOADED", 0)
    monkeypatch.setattr(cli, "build_migration_catalog", lambda *args, **kwargs: catalog)
    arguments = [
        "--legacy-root",
        str(legacy),
        "--opendata-root",
        str(opendata),
        "--baseline",
        str(legacy / "baseline.csv"),
        "--output",
        str(output),
    ]

    assert cli.main(arguments) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert cli.main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists_requires_overwrite"
    assert cli.main([*arguments, "--overwrite"]) == 0
    catalog.errors.append({"path": "bad.py", "error": "syntax"})
    assert cli.main([*arguments, "--overwrite"]) == 1

    raced_output = tmp_path / "reports/race.json"
    original_render = cli.render_json

    def create_racing_output(value: MigrationCatalog) -> str:
        raced_output.write_text("race\n", encoding="utf-8")
        return original_render(value)

    monkeypatch.setattr(cli, "render_json", create_racing_output)
    race_args = [*arguments[:-2], "--output", str(raced_output)]
    assert cli.main(race_args) == 2
    assert raced_output.read_text(encoding="utf-8") == "race\n"
    assert not list(raced_output.parent.glob(".afi204-catalog-*"))


def test_cli_module_entrypoint_and_argument_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executable entrypoint parses explicit roots and argparse rejects unknown flags."""
    from scripts.ops import build_migration_catalog as cli

    legacy = tmp_path / "legacy"
    opendata = tmp_path / "opendata"
    _write_fixture(legacy, "src/backend/app/data_fetch/scripts/daily/job.py", "def main(): pass\n")
    _write_fixture(
        opendata, "opendata/data/providers/akshare/_vendor/__init__.py", "_EXPORTS = {}\n"
    )
    output = tmp_path / "catalog.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(Path(cli.__file__)),
            "--legacy-root",
            str(legacy),
            "--opendata-root",
            str(opendata),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit, match="1"):
        runpy.run_path(cli.__file__, run_name="__main__")
    assert json.loads(output.read_text(encoding="utf-8"))["baseline"]["status"] == (
        "ERROR:FileNotFoundError"
    )
    with pytest.raises(SystemExit, match="2"):
        cli.main(["--unknown-option"])
