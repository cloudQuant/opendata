"""Behavioral tests for the isolated legacy script inventory scanner."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from opendata.migration_inventory import build_inventory, main, write_csv

if TYPE_CHECKING:
    from pathlib import Path


def _write_script(source_root: Path, relative_path: str, source: str) -> Path:
    """Create a source file in the scanner's conventional tree."""
    path = source_root / "src/backend/app/data_fetch/scripts" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_scan_classifies_static_entry_and_hashes_without_running_source(tmp_path: Path) -> None:
    """AST facts, path candidates, hash, and side-effect isolation are retained."""
    marker = tmp_path / "must-not-exist.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "import akshare as ak\n"
        "__import__('openbb')\n"
        "from app.data_fetch.providers.akshare_to_mysql import AkshareToMySql\n"
        "class EquityHistory:\n"
        "    def fetch_data(self):\n"
        "        return ak.stock_zh_a_hist()\n"
        "def main():\n"
        "    return None\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    source_file = _write_script(tmp_path, "stocks/daily/equity_history.py", source)

    report = build_inventory(tmp_path)

    assert len(report.scripts) == 1
    record = report.scripts[0]
    assert record.source_path == "src/backend/app/data_fetch/scripts/stocks/daily/equity_history.py"
    assert record.source_sha256 == hashlib.sha256(source_file.read_bytes()).hexdigest()
    assert record.asset_family == "stock"
    assert record.legacy_frequency == "daily"
    assert record.static_role.split(";") == [
        "entrypoint_fetch_data",
        "entrypoint_main",
        "module_main_call",
        "__main___guard",
    ]
    assert record.direct_source_imports == ["akshare", "dynamic:openbb"]
    assert "app.data_fetch.providers.akshare_to_mysql" in record.shared_writer_refs
    assert record.akshare_usage == "direct_akshare_import"
    assert record.owner_candidate == "B"
    assert record.migration_route == "PENDING_PROVIDER_MAPPING"
    assert record.source_verification == "NOT_VERIFIED"
    assert not marker.exists()
    assert report.errors == []


def test_scan_excludes_init_and_keeps_helpers_with_explicit_unknowns(tmp_path: Path) -> None:
    """Helper modules remain visible and package initializers are omitted."""
    scripts = tmp_path / "src/backend/app/data_fetch/scripts"
    _write_script(tmp_path, "common/__init__.py", "raise RuntimeError('not scanned')\n")
    _write_script(tmp_path, "common/helpers/normalizer.py", "VALUE = 1\n")
    (scripts / "common/helpers/readme.txt").write_text("not Python", encoding="utf-8")

    report = build_inventory(tmp_path)

    assert [item.source_path for item in report.scripts] == [
        "src/backend/app/data_fetch/scripts/common/helpers/normalizer.py"
    ]
    record = report.scripts[0]
    assert record.static_role == "helper_review_required"
    assert record.asset_family == "other"
    assert record.legacy_frequency == "unspecified"
    assert record.mapping_status == "TASK_MAP_NOT_PROVIDED"
    assert record.target_dataset == "PENDING_DATASET_MAPPING"


def test_empty_script_tree_fails_closed(tmp_path: Path) -> None:
    """A valid but empty path cannot be mistaken for a complete inventory."""
    (tmp_path / "src/backend/app/data_fetch/scripts").mkdir(parents=True)

    report = build_inventory(tmp_path)

    assert report.scripts == []
    assert report.has_errors
    assert report.errors[0].error_type == "NoPythonScriptsFound"


def test_asset_family_candidates_respect_top_level_domain_and_option_names(
    tmp_path: Path,
) -> None:
    """Specialized roots and option scripts nested under funds stay distinguishable."""
    _write_script(tmp_path, "indexs/weekly/caixin_composite_pmi.py", "def run():\n    return []\n")
    _write_script(
        tmp_path,
        "funds/daily/option_cffex_hs300.py",
        "def fetch_data():\n    return []\n",
    )
    _write_script(
        tmp_path,
        "common/monthly/macro_usa_cpi.py",
        "def fetch_data():\n    return []\n",
    )

    report = build_inventory(tmp_path)
    by_name = {item.source_path.rsplit("/", maxsplit=1)[-1]: item for item in report.scripts}

    assert by_name["caixin_composite_pmi.py"].asset_family == "index"
    assert by_name["option_cffex_hs300.py"].asset_family == "options"
    assert by_name["option_cffex_hs300.py"].owner_candidate == "C"
    assert by_name["macro_usa_cpi.py"].asset_family == "macro"


def test_parse_error_is_a_hashed_script_record_and_cli_returns_nonzero(tmp_path: Path) -> None:
    """Invalid syntax is never dropped and is visible in JSON and the exit code."""
    path = _write_script(tmp_path, "bonds/weekly/broken.py", "def fetch_data(:\n    pass\n")
    output_path = tmp_path / "reports" / "inventory.json"

    exit_code = main(
        ["--source-root", str(tmp_path), "--output", str(output_path), "--format", "json"]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["summary"]["script_count"] == 1
    assert payload["summary"]["parse_error_count"] == 1
    assert payload["summary"]["filesystem_error_count"] == 0
    assert payload["summary"]["total_error_count"] == 1
    assert len(payload["scripts"]) == 1
    assert payload["scripts"][0]["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "syntax error at line 1" in payload["scripts"][0]["parse_error"]
    assert payload["errors"][0]["error_type"] == "SyntaxError"
    assert payload["scripts"][0]["source_verification"] == "NOT_VERIFIED"


def test_task_map_checks_both_directions_and_preserves_helpers(tmp_path: Path) -> None:
    """Exact task links attach to scripts and unmatched endpoints are reported."""
    _write_script(tmp_path, "stocks/daily/linked.py", "def fetch_data():\n    return []\n")
    _write_script(tmp_path, "stocks/daily/unlinked.py", "def run():\n    return []\n")
    _write_script(tmp_path, "common/helper.py", "VALUE = 2\n")
    task_map = tmp_path / "task-map.json"
    task_map.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "legacy-1",
                        "script_path": "src/backend/app/data_fetch/scripts/stocks/daily/linked.py",
                    },
                    {"task_id": "legacy-missing", "script_path": "missing/script.py"},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_inventory(tmp_path, task_map_path=task_map)

    linked = next(item for item in report.scripts if item.source_path.endswith("linked.py"))
    helper = next(item for item in report.scripts if item.source_path.endswith("helper.py"))
    assert linked.mapped_task_ids == ["legacy-1"]
    assert linked.mapping_status == "MAPPED"
    assert helper.mapping_status == "UNMAPPED_HELPER_REVIEW"
    assert {item.issue for item in report.mapping_issues} == {
        "task_references_unknown_script",
        "entrypoint_without_task",
    }
    assert report.has_errors


def test_task_map_rejects_parent_traversal(tmp_path: Path) -> None:
    """Task paths cannot escape the explicit source-root-relative namespace."""
    _write_script(tmp_path, "stocks/daily/entry.py", "def run():\n    return []\n")
    task_map = tmp_path / "task-map.json"
    task_map.write_text(
        json.dumps({"tasks": [{"task_id": "unsafe", "script_path": "../outside.py"}]}),
        encoding="utf-8",
    )

    report = build_inventory(tmp_path, task_map_path=task_map)

    assert any(item.issue == "task_script_path_invalid" for item in report.mapping_issues)
    assert report.scripts[0].mapped_task_ids == []
    assert report.scripts[0].mapping_status == "UNMAPPED_ENTRYPOINT"


def test_cli_output_cannot_overwrite_scanned_source_or_symlink_alias(tmp_path: Path) -> None:
    """Source bytes are protected even when output is an alias to a scanned file."""
    source_file = _write_script(tmp_path, "stocks/daily/entry.py", "def run():\n    return []\n")
    original_source = source_file.read_bytes()

    direct_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--output",
            str(source_file),
            "--overwrite",
        ]
    )
    assert direct_exit == 2
    assert source_file.read_bytes() == original_source

    symlink_output = tmp_path / "reports" / "alias.json"
    symlink_output.parent.mkdir()
    symlink_output.symlink_to(source_file)
    alias_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--output",
            str(symlink_output),
            "--overwrite",
        ]
    )
    assert alias_exit == 2
    assert source_file.read_bytes() == original_source


def test_cli_output_cannot_overwrite_task_map_or_env_named_path(tmp_path: Path) -> None:
    """Task-map and environment inputs remain byte-identical after rejected writes."""
    _write_script(tmp_path, "stocks/daily/entry.py", "def run():\n    return []\n")
    task_map = tmp_path / "task-map.json"
    task_map.write_text('{"tasks": []}\n', encoding="utf-8")
    original_task_map = task_map.read_bytes()

    task_map_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--task-map",
            str(task_map),
            "--output",
            str(task_map),
            "--overwrite",
        ]
    )
    assert task_map_exit == 2
    assert task_map.read_bytes() == original_task_map

    task_map_alias = tmp_path / "reports" / "task-map-alias.json"
    task_map_alias.parent.mkdir()
    task_map_alias.symlink_to(task_map)
    task_map_alias_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--task-map",
            str(task_map),
            "--output",
            str(task_map_alias),
            "--overwrite",
        ]
    )
    assert task_map_alias_exit == 2
    assert task_map.read_bytes() == original_task_map

    env_file = tmp_path / ".env.local"
    env_file.write_text("SECRET=preserve-me\n", encoding="utf-8")
    original_env_file = env_file.read_bytes()
    env_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--output",
            str(env_file),
            "--overwrite",
        ]
    )
    assert env_exit == 2
    assert env_file.read_bytes() == original_env_file

    env_alias = tmp_path / "reports" / "env-alias.json"
    env_alias.symlink_to(env_file)
    env_alias_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--output",
            str(env_alias),
            "--overwrite",
        ]
    )
    assert env_alias_exit == 2
    assert env_file.read_bytes() == original_env_file


def test_cli_requires_overwrite_for_existing_report(tmp_path: Path) -> None:
    """Existing report files are preserved unless explicit overwrite is requested."""
    _write_script(tmp_path, "stocks/daily/entry.py", "def run():\n    return []\n")
    output_path = tmp_path / "reports" / "inventory.json"
    output_path.parent.mkdir()
    output_path.write_bytes(b"existing report\n")

    rejected_exit = main(["--source-root", str(tmp_path), "--output", str(output_path)])
    assert rejected_exit == 2
    assert output_path.read_bytes() == b"existing report\n"

    accepted_exit = main(
        [
            "--source-root",
            str(tmp_path),
            "--output",
            str(output_path),
            "--overwrite",
        ]
    )
    assert accepted_exit == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["summary"]["script_count"] == 1


def test_cli_summary_counts_scan_errors_separately_from_parse_errors(tmp_path: Path) -> None:
    """A missing directory contributes only to filesystem error counts."""
    output_path = tmp_path / "inventory.json"

    exit_code = main(["--source-root", str(tmp_path), "--output", str(output_path)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["summary"]["parse_error_count"] == 0
    assert payload["summary"]["filesystem_error_count"] == 1
    assert payload["summary"]["total_error_count"] == 1


def test_environment_files_are_refused_as_task_map_input(tmp_path: Path) -> None:
    """The optional mapping reader never accepts an environment file."""
    _write_script(tmp_path, "stocks/daily/entry.py", "def run():\n    return []\n")
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_VALUE=must-not-appear\n", encoding="utf-8")

    report = build_inventory(tmp_path, task_map_path=env_file)
    issue = next(item for item in report.mapping_issues if item.issue == "environment_file_refused")

    assert report.task_map_status == "ERROR"
    assert "must-not-appear" not in issue.detail
    assert "task_map_read_error" not in {item.issue for item in report.mapping_issues}


def test_csv_keeps_all_discovered_script_rows(tmp_path: Path) -> None:
    """CSV output is deterministic and includes structured evidence columns."""
    _write_script(tmp_path, "stocks/hourly/valid.py", "def run():\n    return []\n")
    _write_script(tmp_path, "stocks/daily/invalid.py", "def run(:\n    pass\n")
    report = build_inventory(tmp_path)

    import io

    stream = io.StringIO()
    write_csv(report, stream)
    rows = stream.getvalue().splitlines()

    assert len(rows) == 4
    assert "source_sha256" in rows[0]
    assert "NOT_VERIFIED" in rows[1] or "NOT_VERIFIED" in rows[2]
    assert "syntax error" in stream.getvalue()
