"""Offline counterfacts for the explicit AC2-23 performance probe."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.quality import iter2_performance_probe as probe


def _family(result_hash: str, expected_rows: int) -> dict[str, object]:
    samples = [float(value) for value in range(1, probe.MEASURED_SAMPLES + 1)]
    return {
        "warmup_count": probe.WARMUPS,
        "measured_count": probe.MEASURED_SAMPLES,
        "samples_seconds": samples,
        "p95_seconds": probe._p95(samples),
        "result_sha256": result_hash,
        "result_row_count": expected_rows,
        "process_peak_rss_max_bytes": 100_000_000,
        "connection_counts": {"connect": 1, "checkout": 3, "checkin": 3},
    }


def _snapshot(label: str) -> dict[str, object]:
    suffix = "a1b2c3d4e5f6"
    source_schema = probe._schema_name(suffix, f"{label}_source")
    target_schema = probe._schema_name(suffix, f"{label}_target")
    schema = {
        "columns": [("thscode", "varchar(32)", False), ("trade_date", "date", False)],
        "primary_key": list(probe.KEY),
    }
    return {
        "label": label,
        "source_tree_sha256": "e" * 64,
        "frozen_input": {
            "archive_root": f"/retained/{label}",
            "source_package_archive_tree_sha256": "d" * 64,
            "extracted_package_tree_sha256": "e" * 64,
            "source_bundle_archive_sha256": "b" * 64,
            "source_bundle_manifest_sha256": "c" * 64,
            "runner_sha256": "f" * 64,
            "pyproject_sha256": "a" * 64,
        },
        "api_route_signature_adaptation": {
            "route": "query_domain_data",
            "inspected_at_runtime": True,
            "optional_none_arguments_passed": ["as_of", "market"],
            "optional_none_arguments_omitted": [],
        },
        "connection_guard": {
            "allowed_destination": "127.0.0.1:49152",
            "allowed_connection_attempts": 3,
            "blocked_attempts": 0,
            "docker_endpoint": "unix:///local-docker.sock",
            "container_id": "c" * 64,
            "container_name": f"opendata-c100-perf-20261012T010203Z-{suffix}",
            "volume_name": f"opendata-c100-perf-{suffix}",
            "schemas": {"source": source_schema, "target": target_schema},
            "verified_task_schemas": sorted(
                probe._schema_name(suffix, f"{side}_{role}")
                for side in ("before", "after")
                for role in ("source", "target")
            ),
            "sql_guard_schemas": {"source": source_schema, "target": target_schema},
        },
        "process_guard": {
            "mode": "worker_process_guard",
            "fixed_ps_attempts": 4,
            "blocked_attempts": 0,
            "installed_hooks": ["Popen.__init__"],
            "native_syscall_boundary": (
                "Python entry points only; arbitrary native syscalls are not proven blocked"
            ),
        },
        "http_guard": {
            "mode": "deny_all_python_http_dispatch",
            "installed_hooks": [
                "urllib.request.urlopen",
                "http.client.HTTPConnection.connect",
            ],
            "blocked_attempts": 0,
            "blocked_reasons": {},
        },
        "payload_sha256": "same-payload",
        "ddl_sha256": "same-ddl",
        "fixture_config": {"rows": probe.EXPECTED_ROWS, "seed": probe.FIXTURE_SEED},
        "schema_shape": {
            "source": schema,
            "target": schema,
            "source_equals_target": True,
            "source_ddl_sha256": "same-show-create",
            "target_ddl_sha256": "same-show-create",
        },
        "source_readonly_verified": True,
        "connection_counts": {
            name: {"connect": 1, "checkout": 3, "checkin": 3} for name in ("source", "target")
        },
        "explain": {
            "query_sql_sha256": "qsql",
            "query_plan_sha256": "qplan",
            "export_sql_sha256": "esql",
            "export_plan_sha256": "eplan",
        },
        "import_runs": [
            {
                "kind": kind,
                "elapsed_seconds": 1.0,
                "rows_read": probe.EXPECTED_ROWS,
                "rows_written": probe.EXPECTED_ROWS,
                "batches_written": 10,
                "target_rows_before": 0 if kind == "cold" else probe.EXPECTED_ROWS,
                "target_rows_after": probe.EXPECTED_ROWS,
                "target_business_sha256": "same-ods",
                "source_readonly_verified": True,
                "process_peak_rss_bytes": 100_000_000,
            }
            for kind in ("cold", "idempotent_replay_1", "idempotent_replay_2")
        ],
        "measurements": {
            "query": _family("same-query", probe.QUERY_PAGE_SIZE),
            "export": _family("same-export", probe.EXPORT_LIMIT),
        },
        "import_connection_counts": {
            name: {"connect": 1, "checkout": 3, "checkin": 3} for name in ("source", "target")
        },
        "source_rows": probe.EXPECTED_ROWS,
        "target_rows": probe.EXPECTED_ROWS,
        "process_peak_rss_bytes": 100_000_000,
    }


def test_default_invocation_is_inert(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("default invocation must not reach Docker or DB code")

    monkeypatch.setattr(probe, "_isolated_run", forbidden)
    monkeypatch.setattr(probe, "_docker", forbidden)

    assert probe.main([]) == 2
    assert "No action taken" in capsys.readouterr().out


def test_payload_is_fixed_size_and_repeatable():
    assert sum(1 for _ in probe._synthetic_rows()) == probe.EXPECTED_ROWS == 100_000
    assert probe.synthetic_payload_sha256() == probe.synthetic_payload_sha256()


def test_nearest_rank_p95_and_invalid_samples():
    assert probe._p95(list(range(1, 21))) == 19.0
    with pytest.raises(ValueError, match="at least one"):
        probe._p95([])
    with pytest.raises(ValueError, match="finite"):
        probe._p95([float("nan")])


def test_optional_none_route_arguments_follow_the_inspected_signature():
    def frozen_route(*, market=None):
        return market

    arguments, passed, omitted = probe._optional_none_signature_arguments(
        frozen_route, ("as_of", "market")
    )

    assert arguments == {"market": None}
    assert passed == ["market"]
    assert omitted == ["as_of"]


def test_comparison_accepts_identical_complete_inputs_and_results():
    result = probe._validate_comparison(_snapshot("before"), _snapshot("after"))

    assert result["comparable"] is True
    assert result["results_equal"] is True
    assert result["issues"] == []


def test_comparison_rejects_different_fixture_input():
    before = _snapshot("before")
    after = _snapshot("after")
    after["payload_sha256"] = "different-payload"

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is False
    assert "input_payload_mismatch" in result["issues"]


def test_comparison_rejects_schema_or_ddl_mismatch():
    before = _snapshot("before")
    after = _snapshot("after")
    after["schema_shape"] = {"source": {}, "target": {}, "source_equals_target": False}
    after["ddl_sha256"] = "different-ddl"

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is False
    assert {"schema_shape_mismatch", "ddl_mismatch"} <= set(result["issues"])


def test_comparison_reports_result_hash_difference_without_hiding_it():
    before = _snapshot("before")
    after = _snapshot("after")
    after["measurements"]["query"]["result_sha256"] = "different-query"  # type: ignore[index]

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is True
    assert result["results_equal"] is False
    assert "query_result_hash_diff" in result["issues"]
    assert result["families"]["query"]["results_equal"] is False  # type: ignore[index]


def test_comparison_reports_ods_import_hash_difference():
    before = _snapshot("before")
    after = _snapshot("after")
    after["import_runs"][1]["target_business_sha256"] = "different-ods"  # type: ignore[index]

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is True
    assert result["results_equal"] is False
    assert "import_result_hash_diff" in result["issues"]


def test_comparison_rejects_missing_metrics_connections_and_explain():
    before = _snapshot("before")
    after = _snapshot("after")
    del after["measurements"]["export"]  # type: ignore[index]
    after["connection_counts"] = {}
    after["explain"] = {}

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is False
    assert "missing_export_metrics" in result["issues"]
    assert "after_connection_counts_missing" in result["issues"]
    assert "after_explain_missing" in result["issues"]


def test_comparison_rejects_missing_or_blocked_socket_guard_evidence():
    before = _snapshot("before")
    after = _snapshot("after")
    after["connection_guard"]["blocked_attempts"] = 1  # type: ignore[index]

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is False
    assert "invalid_after_connection_guard" in result["issues"]

    del after["connection_guard"]
    result = probe._validate_comparison(before, after)
    assert result["comparable"] is False
    assert "after_connection_guard_missing" in result["issues"]


def test_comparison_rejects_missing_or_blocked_process_and_http_guards():
    before = _snapshot("before")
    after = _snapshot("after")
    after["process_guard"]["blocked_attempts"] = 1  # type: ignore[index]
    after["http_guard"]["installed_hooks"] = []  # type: ignore[index]

    result = probe._validate_comparison(before, after)

    assert result["comparable"] is False
    assert "invalid_after_process_guard" in result["issues"]
    assert "invalid_after_http_guard" in result["issues"]

    del after["process_guard"]
    del after["http_guard"]
    result = probe._validate_comparison(before, after)
    assert result["comparable"] is False
    assert "after_process_guard_missing" in result["issues"]
    assert "after_http_guard_missing" in result["issues"]


def test_loopback_port_guard_rejects_wildcard_and_protected_ports():
    assert probe._parse_loopback_port("127.0.0.1:49152") == 49152
    for endpoint in ("0.0.0.0:49152", "[::]:49152", "127.0.0.1:3306", "127.0.0.1:33565"):
        with pytest.raises(ValueError):
            probe._parse_loopback_port(endpoint)


def test_socket_guard_refuses_before_calling_the_original_connect():
    guard = probe._TaskSocketGuard(49152)
    calls = 0

    def fake_connect(_sock, _address):
        nonlocal calls
        calls += 1
        return "connected"

    assert (
        guard.call_connect(
            fake_connect,
            probe.socket.AF_INET,
            object(),
            ("127.0.0.1", 49152),
            probe.socket.SOCK_STREAM,
        )
        == "connected"
    )
    assert calls == 1
    for family, address in (
        (probe.socket.AF_INET, ("127.0.0.1", 3306)),
        (probe.socket.AF_INET, ("127.0.0.1", 33565)),
        (probe.socket.AF_INET, ("192.0.2.1", 49152)),
        (probe.socket.AF_INET, ("database.internal", 49152)),
        (probe.socket.AF_UNIX, "/tmp/mysql.sock"),
        (probe.socket.AF_INET6, ("::1", 49152, 0, 0)),
    ):
        with pytest.raises(PermissionError):
            guard.call_connect(fake_connect, family, object(), address)
    with pytest.raises(PermissionError, match="non_task_destination"):
        guard.call_create_connection(fake_connect, ("203.0.113.5", 49152))
    with pytest.raises(PermissionError, match="non_tcp"):
        guard.call_connect(
            fake_connect,
            probe.socket.AF_INET,
            object(),
            ("127.0.0.1", 49152),
            probe.socket.SOCK_DGRAM,
        )
    assert calls == 1
    assert guard.allowed_connection_attempts == 1
    assert guard.blocked_attempts == 8


def test_socket_guard_blocks_dns_and_datagrams_without_calling_originals():
    guard = probe._TaskSocketGuard(49152)
    resolutions = 0
    sends = 0

    def fake_resolution(*_args, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        return []

    def fake_sendto(*_args, **_kwargs):
        nonlocal sends
        sends += 1
        return 0

    assert guard.call_getaddrinfo(fake_resolution, "127.0.0.1", 49152) == []
    for host, port in (("mysql.example", 49152), ("127.0.0.1", 3306), (None, None)):
        with pytest.raises(PermissionError):
            guard.call_getaddrinfo(fake_resolution, host, port)
    with pytest.raises(PermissionError):
        guard.call_sendto(fake_sendto, object(), b"packet", ("127.0.0.1", 49152))
    with pytest.raises(PermissionError, match="non_ipv4_name_resolution"):
        guard.call_getaddrinfo(fake_resolution, "127.0.0.1", 49152, probe.socket.AF_INET6)
    assert resolutions == 1
    assert sends == 0
    assert guard.blocked_attempts == 5


def test_socket_guard_install_preserves_asyncio_socketpair_and_restores_methods():
    guard = probe._TaskSocketGuard(49152)
    original = probe.socket.socket.connect
    guard.install()
    try:
        left, right = probe.socket.socketpair()
        left.close()
        right.close()
        assert guard.allowed_connection_attempts == 0
        assert guard.blocked_attempts == 0
    finally:
        guard.restore()
    assert probe.socket.socket.connect is original


def test_process_guard_denies_generic_commands_and_allows_only_fixed_ps():
    guard = probe._TaskProcessGuard()
    original_popen_init = subprocess.Popen.__init__
    guard.install()
    try:
        with pytest.raises(PermissionError, match="subprocess.Popen"):
            subprocess.run(["/bin/sh", "-c", "true"], check=False)
        with pytest.raises(PermissionError, match="os.system"):
            probe.os.system("true")
        uninitialized_process = object.__new__(subprocess.Popen)
        with pytest.raises(PermissionError, match="Popen._execute_child"):
            uninitialized_process._execute_child()
        rss = probe._current_rss_bytes()
        assert isinstance(rss, int) and rss > 0
        assert guard.fixed_ps_attempts == 1
        assert guard.blocked_attempts == 3
    finally:
        guard.restore()
    assert subprocess.Popen.__init__ is original_popen_init


def test_http_guard_blocks_standard_and_installed_client_dispatch():
    guard = probe._TaskHTTPGuard()
    guard.install()
    try:
        urllib_request = probe.importlib.import_module("urllib.request")
        with pytest.raises(PermissionError, match="urllib.request.urlopen"):
            urllib_request.urlopen("https://example.invalid")
        if "requests.sessions.Session.request" in guard.installed_hooks:
            import requests

            with pytest.raises(PermissionError, match="requests.sessions.Session.request"):
                requests.get("https://example.invalid")
        if "curl_cffi.Curl.perform" in guard.installed_hooks:
            curl_module = probe.importlib.import_module("curl_cffi")
            with pytest.raises(PermissionError, match="curl_cffi.Curl.perform"):
                curl_module.Curl().perform()
        if "pycurl.Curl" in guard.installed_hooks:
            pycurl = probe.importlib.import_module("pycurl")
            with pytest.raises(PermissionError, match="pycurl.Curl"):
                pycurl.Curl()
    finally:
        guard.restore()
    assert guard.blocked_attempts >= 1
    assert guard.installed_hooks


def test_offline_ddl_guard_denies_network_before_original_dispatch():
    guard = probe._DenyAllNetworkGuard()
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    guard.install()
    try:
        with pytest.raises(PermissionError, match="getaddrinfo"):
            socket.getaddrinfo("database.example", 3306)
        with pytest.raises(PermissionError, match="gethostbyname"):
            socket.gethostbyname("database.example")
        with pytest.raises(PermissionError, match="create_connection"):
            socket.create_connection(("127.0.0.1", 49152), timeout=0.01)
        with pytest.raises(PermissionError, match="connect"):
            tcp.connect(("127.0.0.1", 49152))
        with pytest.raises(PermissionError, match="sendto"):
            udp.sendto(b"blocked", ("127.0.0.1", 49152))
        evidence = guard.evidence()
        assert evidence["mode"] == "deny_all"
        assert evidence["blocked_attempts"] == 5
        assert evidence["original_dispatches"]
        assert set(evidence["original_dispatches"].values()) == {0}  # type: ignore[union-attr]
    finally:
        guard.restore()
        tcp.close()
        udp.close()


def test_ddl_guard_recognizes_only_denied_urllib3_bootstrap_probe():
    probe_event = {
        "surface": "bind",
        "phase": "http_guard_bootstrap",
        "stack": [
            {
                "module": "urllib3.util.connection",
                "file": "connection.py",
                "line": 127,
                "function": "_has_ipv6",
            }
        ],
    }
    assert probe._DenyAllNetworkGuard._is_known_bootstrap_capability_probe(probe_event)

    wrong_phase = {**probe_event, "phase": "product_bootstrap"}
    wrong_surface = {**probe_event, "surface": "connect"}
    wrong_module = {
        **probe_event,
        "stack": [{**probe_event["stack"][0], "module": "application.network"}],
    }
    assert not probe._DenyAllNetworkGuard._is_known_bootstrap_capability_probe(wrong_phase)
    assert not probe._DenyAllNetworkGuard._is_known_bootstrap_capability_probe(wrong_surface)
    assert not probe._DenyAllNetworkGuard._is_known_bootstrap_capability_probe(wrong_module)


def test_render_ddl_reports_zero_dispatch_offline_guard(monkeypatch, tmp_path, capsys):
    ddl_module = types.ModuleType("opendata.pipeline.ddl")

    class FakeColumn:
        def __init__(self, name, sql_type, nullable=True):
            self.name = name
            self.sql_type = sql_type
            self.nullable = nullable

    ddl_module.Column = FakeColumn  # type: ignore[attr-defined]
    ddl_module.ods_table_ddl = lambda *_args, **_kwargs: (
        "CREATE TABLE `ods_stock_daily_ths` (PRIMARY KEY (`thscode`, `trade_date`))"
    )  # type: ignore[attr-defined]
    package = types.ModuleType("opendata")
    package.__path__ = []  # type: ignore[attr-defined]
    pipeline = types.ModuleType("opendata.pipeline")
    pipeline.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opendata", package)
    monkeypatch.setitem(sys.modules, "opendata.pipeline", pipeline)
    monkeypatch.setitem(sys.modules, "opendata.pipeline.ddl", ddl_module)

    probe._render_ddl(tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert result["ddl"].startswith("CREATE TABLE")
    guard = result["offline_guard"]
    assert guard["mode"] == "deny_all"
    assert guard["blocked_attempts"] in (0, 1)
    assert guard["known_bootstrap_probe_attempts"] == guard["blocked_attempts"]
    assert guard["unclassified_blocked_attempts"] == 0
    assert guard["all_denied_attempts_recorded"] is True
    assert all(
        probe._DenyAllNetworkGuard._is_known_bootstrap_capability_probe(event)
        for event in guard["blocked_events"]
    )
    assert guard["target_dispatch_delta"] == 0
    assert guard["original_dispatches"]
    assert set(guard["original_dispatches"].values()) == {0}
    assert guard["saved_original_dispatches"] == guard["original_dispatches"]
    assert guard["process_guard"]["blocked_attempts"] == 0
    assert guard["http_guard"]["blocked_attempts"] == 0


def test_baseline_ddl_rejects_missing_offline_guard_evidence(monkeypatch, tmp_path):
    (tmp_path / "opendata").mkdir()
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ddl": "PRIMARY KEY (`thscode`, `trade_date`)"}),
        ),
    )

    with pytest.raises(RuntimeError, match="zero-dispatch offline guard"):
        probe._render_baseline_ddl(tmp_path)


def test_source_snapshot_filter_omits_secrets_and_generated_output(tmp_path):
    source = tmp_path / "opendata"
    (source / "pipeline").mkdir(parents=True)
    (source / "pipeline" / "job.py").write_text("value = 1\n", encoding="utf-8")
    (source / ".env").write_text("PASSWORD=secret\n", encoding="utf-8")
    (source / "private.pem").write_text("private key\n", encoding="utf-8")
    (source / "runs" / "run-1").mkdir(parents=True)
    (source / "runs" / "run-1" / "result.json").write_text("{}", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "job.pyc").write_bytes(b"bytecode")
    before = probe._tree_sha256(source)
    source_state = probe._source_tree_state(source)
    assert source_state[0] == before

    (source / ".env").write_text("PASSWORD=changed\n", encoding="utf-8")
    (source / "private.pem").write_text("changed key\n", encoding="utf-8")
    (source / "runs" / "run-1" / "result.json").write_text("changed", encoding="utf-8")
    assert probe._tree_sha256(source) == before

    included, excluded = probe._source_inventory(source)
    assert [path.relative_to(source).as_posix() for path in included] == ["pipeline/job.py"]
    assert excluded == {
        "environment_file": 1,
        "generated_directory": 2,
        "secret_or_database_file": 1,
    }


def test_current_source_archive_is_reproducible_and_manifest_is_exact(tmp_path):
    source = tmp_path / "snapshot" / "opendata"
    (source / "data").mkdir(parents=True)
    (source / "data" / "contract.py").write_text("answer = 42\n", encoding="utf-8")
    (source / ".env.production").write_text("TOKEN=do-not-archive\n", encoding="utf-8")
    run_suffix = "a1b2c3d4e5f6"
    first_run = tmp_path / "first-run"
    second_run = tmp_path / "second-run"
    first_run.mkdir()
    second_run.mkdir()

    first = probe._persist_current_source_archive(source, first_run, "run-id", run_suffix)
    second = probe._persist_current_source_archive(source, second_run, "run-id", run_suffix)

    assert first["archive_sha256"] == second["archive_sha256"]
    assert (first_run / str(first["archive_file"])).read_bytes() == (
        second_run / str(second["archive_file"])
    ).read_bytes()
    manifest_path = first_run / str(first["manifest_file"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["role"] == "after"
    assert manifest["source_tree_sha256"] == probe._tree_sha256(source)
    assert manifest["archive_sha256"] == probe._sha256_file(first_run / str(first["archive_file"]))
    assert [entry["path"] for entry in manifest["files"]] == ["data/contract.py"]
    with probe.tarfile.open(first_run / str(first["archive_file"]), "r:") as archive:
        assert archive.getnames() == ["opendata/data/contract.py"]
        assert archive.extractfile("opendata/data/contract.py").read() == b"answer = 42\n"


def test_current_source_archive_refuses_to_overwrite_artifacts(tmp_path):
    source = tmp_path / "opendata"
    source.mkdir()
    (source / "module.py").write_text("pass\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    probe._persist_current_source_archive(source, run_dir, "run-id", "a1b2c3d4e5f6")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        probe._persist_current_source_archive(source, run_dir, "run-id", "a1b2c3d4e5f6")


def test_retained_bundle_contains_runner_and_extracts_immutable_exact_sources(tmp_path):
    source_root = tmp_path / "source"
    package = source_root / "opendata"
    package.mkdir(parents=True)
    (package / "marker.py").write_text("VALUE = 'frozen'\n", encoding="utf-8")
    runner = source_root / "scripts/quality/iter2_performance_probe.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("print('frozen runner')\n", encoding="utf-8")
    project = source_root / "pyproject.toml"
    project.write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (source_root / ".env").write_text("PASSWORD=omit\n", encoding="utf-8")
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()

    evidence = probe._persist_source_bundle(source_root, run_dir, "run-id", "after", "a1b2c3d4e5f6")
    extracted = probe._extract_verified_source_bundle(
        run_dir / str(evidence["archive_file"]),
        run_dir / str(evidence["manifest_file"]),
        run_dir / "source-after-input",
    )

    assert (extracted / "opendata/marker.py").read_text(encoding="utf-8") == "VALUE = 'frozen'\n"
    assert (extracted / "scripts/quality/iter2_performance_probe.py").read_text(
        encoding="utf-8"
    ) == "print('frozen runner')\n"
    assert not (extracted / ".env").exists()
    assert not (extracted.stat().st_mode & 0o222)
    assert not ((extracted / "opendata/marker.py").stat().st_mode & 0o222)
    staging_receipt = probe._verify_staged_source_bundle(
        run_dir / str(evidence["archive_file"]),
        run_dir / str(evidence["manifest_file"]),
        extracted,
    )
    assert staging_receipt["all_staging_bytes_match_archive_and_manifest"] is True
    assert staging_receipt["verified_file_count"] == 3
    (package / "marker.py").write_text("VALUE = 'changed live checkout'\n", encoding="utf-8")
    assert (extracted / "opendata/marker.py").read_text(encoding="utf-8") == "VALUE = 'frozen'\n"
    for path in sorted(extracted.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    extracted.chmod(0o755)


def test_retained_bundle_rejects_archive_tampering_before_extraction(tmp_path):
    source_root = tmp_path / "source"
    (source_root / "opendata").mkdir(parents=True)
    (source_root / "opendata/marker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source_root / "scripts/quality").mkdir(parents=True)
    (source_root / "scripts/quality/iter2_performance_probe.py").write_text(
        "print('runner')\n", encoding="utf-8"
    )
    (source_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    evidence = probe._persist_source_bundle(source_root, run_dir, "run-id", "after", "a1b2c3d4e5f6")
    archive_path = run_dir / str(evidence["archive_file"])
    archive_path.write_bytes(archive_path.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="archive hash does not match"):
        probe._extract_verified_source_bundle(
            archive_path,
            run_dir / str(evidence["manifest_file"]),
            run_dir / "source-after-input",
        )
    assert not (run_dir / "source-after-input").exists()


def test_worker_preflight_binds_to_sealed_archive_hashes_and_rejects_tampering():
    proof = {
        "source_bundle_role": "after",
        "snapshot_source_root": "/sealed/current",
        "source_package_archive_tree_sha256": "d" * 64,
        "extracted_package_tree_sha256": "e" * 64,
        "source_bundle_archive_sha256": "b" * 64,
        "source_bundle_manifest_sha256": "c" * 64,
        "runner_sha256": "f" * 64,
        "pyproject_sha256": "a" * 64,
    }
    result = {
        "label": "after",
        "status": "preflight_pass",
        "source_tree_sha256": "e" * 64,
        "runner_sha256": "f" * 64,
        "frozen_input": probe._worker_frozen_input_claim(proof),
    }

    probe._validate_preflight_worker_input(result, proof)

    result["frozen_input"] = {**result["frozen_input"], "source_bundle_archive_sha256": "0" * 64}
    with pytest.raises(RuntimeError, match="sealed source bundle"):
        probe._validate_preflight_worker_input(result, proof)


def test_live_worktree_drift_is_recorded_without_changing_sealed_input_claim(tmp_path):
    package = tmp_path / "opendata"
    source_file = package / "module.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 'sealed'\n", encoding="utf-8")
    start_tree, start_inventory, start_excluded, start_cache = probe._source_tree_state(package)
    assert start_tree == probe._tree_sha256(package)
    bundle_manifest = {
        "source_package_tree_sha256": start_tree,
        "files": [
            {
                "path": "opendata/module.py",
                **start_inventory["module.py"],
            }
        ],
    }

    source_file.write_text("VALUE = 'live edit after capture'\n", encoding="utf-8")
    cache_directory = package / "__pycache__"
    cache_directory.mkdir()
    (cache_directory / "module.cpython-311.pyc").write_bytes(b"generated cache")
    end_tree, end_inventory, end_excluded, end_cache = probe._source_tree_state(package)
    assert end_tree == probe._tree_sha256(package)
    drift = probe._current_worktree_drift_evidence(
        start_inventory,
        end_inventory,
        bundle_manifest,
        start_tree,
        end_tree,
        start_excluded,
        end_excluded,
        start_cache,
        end_cache,
    )

    assert drift["has_worktree_drift"] is True
    assert [entry["path"] for entry in drift["captured_bundle_to_end_source_deltas"]] == [
        "module.py"
    ]
    assert drift["python_cache_counts"]["delta"] == {
        "bytecode_file_count": 1,
        "directory_count": 1,
    }
    assert drift["comparison_scope"].startswith("included source files only")

    proof = {
        "source_bundle_role": "after",
        "snapshot_source_root": "/sealed/current",
        "source_package_archive_tree_sha256": "d" * 64,
        "extracted_package_tree_sha256": "e" * 64,
        "source_bundle_archive_sha256": "b" * 64,
        "source_bundle_manifest_sha256": "c" * 64,
        "runner_sha256": "f" * 64,
        "pyproject_sha256": "a" * 64,
    }
    worker_result = {
        "label": "after",
        "status": "preflight_pass",
        "source_tree_sha256": "e" * 64,
        "runner_sha256": "f" * 64,
        "frozen_input": probe._worker_frozen_input_claim(proof),
    }
    probe._validate_preflight_worker_input(worker_result, proof)


def test_sql_guard_allows_owned_reads_and_target_writes_but_blocks_cross_schema():
    guard = probe._TaskSQLGuard(
        "opendata_c100_a1b2c3d4e5f6_before_source",
        "opendata_c100_a1b2c3d4e5f6_before_target",
    )

    class FakeConnection:
        def __init__(self, database):
            self.engine = type(
                "FakeEngine", (), {"url": type("URL", (), {"database": database})()}
            )()

    def check(database, sql):
        guard.before_cursor_execute(FakeConnection(database), None, sql, None, None, False)

    check("opendata_c100_a1b2c3d4e5f6_before_source", "SELECT * FROM `ods_stock_daily_ths`")
    check(
        "opendata_c100_a1b2c3d4e5f6_before_target", "INSERT INTO `ods_stock_daily_ths` VALUES (1)"
    )
    check(
        "opendata_c100_a1b2c3d4e5f6_before_target",
        "TRUNCATE TABLE `_stg_ods_stock_daily_ths`",
    )
    check("opendata_c100_a1b2c3d4e5f6_before_target", "EXPLAIN FORMAT=JSON SELECT * FROM t")
    with pytest.raises(PermissionError, match="source-side write"):
        check("opendata_c100_a1b2c3d4e5f6_before_source", "DELETE FROM t")
    with pytest.raises(PermissionError, match="source-side write"):
        check(
            "opendata_c100_a1b2c3d4e5f6_before_source",
            "TRUNCATE TABLE `_stg_ods_stock_daily_ths`",
        )
    with pytest.raises(PermissionError, match="cross-schema"):
        check(
            "opendata_c100_a1b2c3d4e5f6_before_target",
            "INSERT INTO `opendata_c100_a1b2c3d4e5f6_after_target`.`t` SELECT * FROM t",
        )
    with pytest.raises(PermissionError, match="database-level"):
        check("opendata_c100_a1b2c3d4e5f6_before_target", "CREATE DATABASE forbidden")
    with pytest.raises(PermissionError, match="database-level"):
        check("opendata_c100_a1b2c3d4e5f6_before_target", "USE forbidden")
    with pytest.raises(PermissionError, match="global server setting"):
        check("opendata_c100_a1b2c3d4e5f6_before_target", "SET GLOBAL sql_mode = ''")
    with pytest.raises(PermissionError, match="unowned schema"):
        check("opendata_c100_a1b2c3d4e5f6_after_target", "SELECT 1")


def test_worker_schema_pair_is_exact_to_the_snapshot_side():
    suffix = "a1b2c3d4e5f6"
    probe._validate_worker_schema_pair(
        "before",
        suffix,
        probe._schema_name(suffix, "before_source"),
        probe._schema_name(suffix, "before_target"),
    )
    with pytest.raises(ValueError, match="not owned"):
        probe._validate_worker_schema_pair(
            "before",
            suffix,
            probe._schema_name(suffix, "after_source"),
            probe._schema_name(suffix, "after_target"),
        )


def test_worker_mode_requires_ownership_arguments_before_docker(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("worker CLI must reject missing proof before Docker inspection")

    monkeypatch.setattr(probe, "_docker", forbidden)
    with pytest.raises(SystemExit, match="missing a required argument"):
        probe.main(
            [
                "--worker",
                "--source-root",
                "/tmp/snapshot",
                "--label",
                "before",
                "--source-schema",
                "opendata_c100_a1b2c3d4e5f6_before_source",
                "--target-schema",
                "opendata_c100_a1b2c3d4e5f6_before_target",
                "--port",
                "49152",
            ]
        )


def test_local_docker_context_guard_rejects_remote_endpoints(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(probe, "_docker", lambda _args: "unix:///local-docker.sock")
    assert probe._require_local_docker_endpoint() == "unix:///local-docker.sock"

    monkeypatch.setattr(probe, "_docker", lambda _args: "ssh://remote.example")
    with pytest.raises(RuntimeError, match="local Unix-socket"):
        probe._require_local_docker_endpoint()
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda _args: pytest.fail("DOCKER_HOST override must be checked before Docker I/O"),
    )
    with pytest.raises(RuntimeError, match="local Unix-socket"):
        probe._require_local_docker_endpoint()


@pytest.mark.parametrize(
    ("resource_kind", "name", "expected_name", "not_found_stderr"),
    [
        (
            "container",
            "opendata-c100-perf-20261012T010203Z-a1b2c3d4e5f6",
            "/opendata-c100-perf-20261012T010203Z-a1b2c3d4e5f6",
            "Error: No such object: opendata-c100-perf-20261012T010203Z-a1b2c3d4e5f6",
        ),
        (
            "volume",
            "opendata-c100-perf-a1b2c3d4e5f6",
            "opendata-c100-perf-a1b2c3d4e5f6",
            "Error response from daemon: get opendata-c100-perf-a1b2c3d4e5f6: no such volume",
        ),
    ],
)
def test_docker_resource_probe_uses_exit_status_and_exact_not_found(
    monkeypatch, resource_kind, name, expected_name, not_found_stderr
):
    calls = []

    def fake_run_command(arguments, *, env=None):
        calls.append((arguments, env))
        return subprocess.CompletedProcess(
            args=arguments,
            returncode=1,
            stdout="[]\n",
            stderr=f"{not_found_stderr}\n",
        )

    monkeypatch.setattr(probe, "_executable", lambda executable: f"/test/{executable}")
    monkeypatch.setattr(probe, "_run_command", fake_run_command)

    assert not probe._docker_resource_exists(resource_kind, name)
    expected_arguments = (
        ["/test/docker", "inspect", name]
        if resource_kind == "container"
        else ["/test/docker", "volume", "inspect", name]
    )
    assert calls == [(expected_arguments, None)]

    def found_run_command(arguments, *, env=None):
        return subprocess.CompletedProcess(
            args=arguments,
            returncode=0,
            stdout=json.dumps([{"Name": expected_name}]),
            stderr="",
        )

    monkeypatch.setattr(probe, "_run_command", found_run_command)
    assert probe._docker_resource_exists(resource_kind, name)


def test_docker_resource_probe_fails_closed_on_daemon_and_identity_errors(monkeypatch):
    name = "opendata-c100-perf-a1b2c3d4e5f6"
    monkeypatch.setattr(probe, "_executable", lambda executable: f"/test/{executable}")
    monkeypatch.setattr(
        probe,
        "_run_command",
        lambda arguments, *, env=None: subprocess.CompletedProcess(
            args=arguments,
            returncode=1,
            stdout="[]\n",
            stderr="Cannot connect to the Docker daemon\n",
        ),
    )
    with pytest.raises(RuntimeError, match="unable to verify task Docker volume"):
        probe._docker_resource_exists("volume", name)

    monkeypatch.setattr(
        probe,
        "_run_command",
        lambda arguments, *, env=None: subprocess.CompletedProcess(
            args=arguments,
            returncode=0,
            stdout="[]",
            stderr="",
        ),
    )
    with pytest.raises(RuntimeError, match="unexpected identity"):
        probe._docker_resource_exists("volume", name)

    with pytest.raises(ValueError, match="task naming scheme"):
        probe._docker_resource_exists("volume", "user-volume")


def test_container_port_preflight_rejects_non_loopback_and_wrong_port():
    port = 49152
    base = {
        "Id": "c" * 64,
        "Name": "/opendata-c100-perf-20261012T010203Z-a1b2c3d4e5f6",
        "Config": {
            "Image": probe.MYSQL_IMAGE,
            "Labels": {
                probe.RUN_LABEL: "run-id",
                probe.PURPOSE_LABEL: probe.PROBE_PURPOSE,
            },
        },
        "HostConfig": {
            "PortBindings": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
            "NetworkMode": "bridge",
            "Memory": 2_147_483_648,
            "NanoCpus": 2_000_000_000,
        },
        "NetworkSettings": {
            "Ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
        },
    }
    assert probe._port_mapping_evidence(
        base, expected_port=port, published_output=f"127.0.0.1:{port}\n"
    )["valid"]

    non_loopback = {
        **base,
        "HostConfig": {
            **base["HostConfig"],
            "PortBindings": {"3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": ""}]},
        },
    }
    assert not probe._port_mapping_evidence(
        non_loopback, expected_port=port, published_output=f"127.0.0.1:{port}\n"
    )["valid"]

    wrong_port = {
        **base,
        "NetworkSettings": {"Ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]}},
    }
    assert not probe._port_mapping_evidence(
        wrong_port, expected_port=port, published_output="127.0.0.1:49153\n"
    )["valid"]
    wrong_configured_port = {
        **base,
        "HostConfig": {
            **base["HostConfig"],
            "PortBindings": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]},
        },
    }
    assert not probe._port_mapping_evidence(
        wrong_configured_port, expected_port=port, published_output=f"127.0.0.1:{port}\n"
    )["valid"]
    assert not probe._port_mapping_evidence(
        base, expected_port=port, published_output="127.0.0.1:49153\n"
    )["valid"]

    foreign_owner_labels = {
        probe.RUN_LABEL: "another-run",
        probe.PURPOSE_LABEL: probe.PROBE_PURPOSE,
    }
    with pytest.raises(RuntimeError, match="ownership"):
        probe._require_owned_resource(
            json.dumps(foreign_owner_labels), "run-id", purpose=probe.PROBE_PURPOSE
        )


def test_child_worker_environment_points_only_to_verified_target(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "old-host")
    monkeypatch.setenv("DATABASE_URL", "mysql://user:secret@old-host/old-db")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/live/checkout")
    env = probe._child_env(
        "synthetic-password",
        port=49152,
        database="opendata_c100_a1b2c3d4e5f6_before_target",
    )

    assert env["TESTING"] == "true"
    assert env["APP_ENV"] == "testing"
    assert env["MYSQL_HOST"] == env["DATA_MYSQL_HOST"] == "127.0.0.1"
    assert env["MYSQL_PORT"] == env["DATA_MYSQL_PORT"] == "49152"
    assert (
        env["MYSQL_DATABASE"]
        == env["DATA_MYSQL_DATABASE"]
        == ("opendata_c100_a1b2c3d4e5f6_before_target")
    )
    assert env["MYSQL_PASSWORD"] == env["DATA_MYSQL_PASSWORD"] == "synthetic-password"
    assert "DATABASE_URL" not in env
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env


def test_child_worker_environment_pins_pythonpath_to_frozen_root(monkeypatch, tmp_path):
    frozen_root = tmp_path / "bundle"
    (frozen_root / "opendata").mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/live/checkout")

    env = probe._child_env("synthetic-password", pythonpath=frozen_root)

    assert env["PYTHONPATH"] == str(frozen_root.resolve())
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONHOME" not in env


def test_snapshot_worker_executes_archive_runner_and_ignores_live_checkout(tmp_path):
    live_repository = tmp_path / "live"
    live_runner = live_repository / "scripts/quality/iter2_performance_probe.py"
    live_runner.parent.mkdir(parents=True)
    live_runner.write_text(
        "raise RuntimeError('live checkout runner executed')\n", encoding="utf-8"
    )
    live_package = live_repository / "opendata"
    live_package.mkdir()
    (live_package / "marker.py").write_text("VALUE = 'live checkout'\n", encoding="utf-8")

    frozen_root = tmp_path / "frozen"
    frozen_runner = frozen_root / "scripts/quality/iter2_performance_probe.py"
    frozen_runner.parent.mkdir(parents=True)
    frozen_package = frozen_root / "opendata"
    frozen_package.mkdir()
    (frozen_package / "marker.py").write_text("VALUE = 'retained archive'\n", encoding="utf-8")
    (frozen_root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    frozen_runner.write_text(
        "import argparse, json, os\n"
        "from pathlib import Path\n"
        "from opendata.marker import VALUE\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--label')\n"
        "args, _ = parser.parse_known_args()\n"
        "print(json.dumps({'label': args.label, 'value': VALUE, 'cwd': os.getcwd(), "
        "'pythonpath': os.environ.get('PYTHONPATH'), "
        "'runner': str(Path(__file__).resolve())}))\n",
        encoding="utf-8",
    )
    live_runner.write_text(
        "raise RuntimeError('mutated live runner must stay unused')\n", encoding="utf-8"
    )
    (live_package / "marker.py").write_text("VALUE = 'mutated live source'\n", encoding="utf-8")
    destination = tmp_path / "result"
    destination.mkdir()
    proof = {"snapshot_source_root": str(frozen_root.resolve())}

    result = probe._snapshot_worker(
        live_repository,
        "before",
        frozen_root,
        "opendata_c100_a1b2c3d4e5f6_before_source",
        "opendata_c100_a1b2c3d4e5f6_before_target",
        49152,
        "synthetic-password",
        destination,
        "run-id",
        "c" * 64,
        "opendata-c100-perf-20261012T010203Z-a1b2c3d4e5f6",
        "opendata-c100-perf-a1b2c3d4e5f6",
        "unix:///local-docker.sock",
        proof,
    )

    assert result["value"] == "retained archive"
    assert result["cwd"] == str(frozen_root.resolve())
    assert result["pythonpath"] == str(frozen_root.resolve())
    assert result["runner"] == str(frozen_runner.resolve())


def test_docker_cleanup_requires_exact_run_ownership():
    run_id = "run-123"
    encoded = (
        '{"org.opendata.c100.performance.run-id":"run-123",'
        '"org.opendata.c100.performance.purpose":"iter2-stock-daily-ods"}'
    )

    assert (
        probe._require_owned_resource(encoded, run_id, purpose=probe.PROBE_PURPOSE)[probe.RUN_LABEL]
        == run_id
    )
    with pytest.raises(RuntimeError, match="ownership"):
        probe._require_owned_resource(encoded, "other-run", purpose=probe.PROBE_PURPOSE)


def test_tree_digest_changes_with_source_contents(tmp_path: Path):
    package = tmp_path / "opendata"
    package.mkdir()
    source = package / "probe.py"
    source.write_text("value = 1\n", encoding="utf-8")
    before = probe._tree_sha256(package)
    source.write_text("value = 2\n", encoding="utf-8")

    assert probe._tree_sha256(package) != before
