"""Guard the first-party/vendor boundary and historical quality baselines."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import tomllib
import yaml

from scripts.codemod import verify_no_akshare
from scripts.quality import ratchet
from scripts.quality.source_layout import (
    FIRST_PARTY,
    PORTED,
    VENDOR_ROOT,
    SourceLayoutError,
    classify_path,
    historical_identity,
    iter_unique_python_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_classification_uses_exact_nested_vendor_prefix() -> None:
    assert classify_path(VENDOR_ROOT) == PORTED
    assert classify_path(f"{VENDOR_ROOT}/stock/cons.py") == PORTED
    assert classify_path("opendata/data/providers/akshare/adapter.py") == FIRST_PARTY
    assert classify_path("opendata/data/providers/akshare/_vendorish/adapter.py") == FIRST_PARTY
    assert classify_path("other/_vendor/adapter.py") is None
    assert classify_path("opendata_client/_vendor/adapter.py") == FIRST_PARTY


def test_historical_paths_map_to_one_current_source_identity() -> None:
    assert historical_identity("opendata_http/stock/cons.py") == (f"{VENDOR_ROOT}/stock/cons.py")


def test_all_fuyao_historical_identities_match_the_applied_layout_without_collisions() -> None:
    actual_layout = {
        "__init__.py": "opendata/data/providers/ths/transport/__init__.py",
        "credentials.py": "opendata/data/providers/ths/transport/credentials.py",
        "dumps.py": "opendata/data/providers/ths/dumps.py",
        "endpoint_map.py": "opendata/data/providers/ths/endpoint_map.py",
        "endpoint_map.yaml": "opendata/data/providers/ths/endpoint_map.yaml",
        "endpoints.py": "opendata/data/providers/ths/endpoints.py",
        "envelope.py": "opendata/data/providers/ths/transport/envelope.py",
        "error_messages.yaml": "opendata/data/providers/ths/transport/error_messages.yaml",
        "errors.py": "opendata/data/providers/ths/transport/errors.py",
        "http_client.py": "opendata/data/providers/ths/transport/http_client.py",
        "rate_limiter.py": "opendata/data/providers/ths/transport/rate_limiter.py",
    }

    mapped = {name: historical_identity(f"opendata_fuyao/{name}") for name in actual_layout}
    assert mapped == actual_layout
    assert len(set(mapped.values())) == len(actual_layout)
    assert mapped["__init__.py"] != "opendata/data/providers/ths/__init__.py"
    with pytest.raises(SourceLayoutError, match="no single file identity"):
        historical_identity("opendata_fuyao")


def test_recursive_layers_are_complete_and_overlapping_roots_are_unique(tmp_path: Path) -> None:
    vendor = tmp_path / VENDOR_ROOT
    vendor.mkdir(parents=True)
    vendor_file = vendor / "stock" / "sample.py"
    vendor_file.parent.mkdir()
    vendor_file.write_text("ported = True\n", encoding="utf-8")
    adapter = vendor.parent / "adapter.py"
    adapter.write_text("first_party = True\n", encoding="utf-8")
    lookalike = vendor.parent / "_vendorish" / "adapter.py"
    lookalike.parent.mkdir()
    lookalike.write_text("first_party = True\n", encoding="utf-8")

    first_party = iter_unique_python_files(tmp_path, ("opendata",), layers=frozenset({FIRST_PARTY}))
    ported = iter_unique_python_files(
        tmp_path,
        ("opendata", VENDOR_ROOT),
        layers=frozenset({PORTED}),
    )

    assert {item.identity for item in first_party} == {
        "opendata/data/providers/akshare/adapter.py",
        "opendata/data/providers/akshare/_vendorish/adapter.py",
    }
    assert [item.identity for item in ported] == [
        "opendata/data/providers/akshare/_vendor/stock/sample.py"
    ]


def test_source_iteration_fails_closed_for_missing_or_empty_roots(tmp_path: Path) -> None:
    with pytest.raises(SourceLayoutError):
        iter_unique_python_files(tmp_path, ("missing",))

    (tmp_path / "empty").mkdir()
    with pytest.raises(SourceLayoutError):
        iter_unique_python_files(tmp_path, ("empty",))


def test_current_quality_planes_cover_vendor_once_and_keep_ratchet_debts() -> None:
    snapshot = json.loads((REPO_ROOT / "docs/quality/ratchet.json").read_text(encoding="utf-8"))
    assert snapshot["metrics"] == {
        "ruff_selfdev": 148,
        "mypy_selfdev": 0,
        "bandit_selfdev": 1,
        "ruff_ported": 2188,
        "direct_http_ported": 1071,
    }
    assert ratchet.PORTED_PATHS == (VENDOR_ROOT,)
    assert ratchet.count_direct_http(VENDOR_ROOT) == 1071

    expected = iter_unique_python_files(REPO_ROOT, ("opendata",), layers=frozenset({FIRST_PARTY}))
    vendor = iter_unique_python_files(
        REPO_ROOT, ("opendata", VENDOR_ROOT), layers=frozenset({PORTED})
    )
    all_opendata = iter_unique_python_files(
        REPO_ROOT,
        ("opendata",),
        layers=frozenset({FIRST_PARTY, PORTED}),
    )
    assert len({item.identity for item in expected + vendor}) == len(all_opendata)
    assert len({item.identity for item in expected + vendor}) == len(expected) + len(vendor)


def test_ported_ruff_scan_matches_complete_directory_and_identity_scans() -> None:
    vendor_files = iter_unique_python_files(REPO_ROOT, (VENDOR_ROOT,), layers=frozenset({PORTED}))
    assert len(vendor_files) == 325
    identities = [item.identity for item in vendor_files]
    assert len(set(identities)) == 325

    show_files = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ruff", "check", VENDOR_ROOT, "--show-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert show_files.returncode == 0, show_files.stderr
    walked = {
        Path(line).resolve().relative_to(REPO_ROOT).as_posix()
        for line in show_files.stdout.splitlines()
        if line.strip()
    }
    assert walked == set(identities)

    directory_scan = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "E,F",
            "--output-format=json",
            VENDOR_ROOT,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    identity_scan = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "E,F",
            "--output-format=json",
            *identities,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert directory_scan.returncode in (0, 1), directory_scan.stderr
    assert identity_scan.returncode in (0, 1), identity_scan.stderr

    def finding_keys(
        result: subprocess.CompletedProcess[str],
    ) -> Counter[tuple[str, str, int, int]]:
        findings = json.loads(result.stdout or "[]")
        return Counter(
            (
                Path(item["filename"]).resolve().relative_to(REPO_ROOT).as_posix(),
                item["code"],
                item["location"]["row"],
                item["location"]["column"],
            )
            for item in findings
        )

    assert finding_keys(directory_scan) == finding_keys(identity_scan)
    assert sum(finding_keys(identity_scan).values()) == ratchet.count_ruff(
        ratchet.PORTED_PATHS, select="E,F"
    )


@pytest.mark.parametrize(
    "filename",
    [
        f"{VENDOR_ROOT}/quality_canary.py",
        "opendata/data/providers/akshare/quality_canary.py",
    ],
)
def test_e_f_rule_canaries_stay_visible_inside_and_outside_vendor_prefix(
    filename: str,
) -> None:
    source = f"import os\nmarker = 1\nimport sys\nlong_line = {'x' * 110!r}\n"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "E,F",
            "--output-format=json",
            "--stdin-filename",
            filename,
            "-",
        ],
        cwd=REPO_ROOT,
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    codes = {item["code"] for item in json.loads(result.stdout or "[]")}
    assert {"E501", "E402", "F401"} <= codes


def test_quality_exclusions_are_exact_and_packaging_scopes_are_migrated() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bandit = yaml.safe_load((REPO_ROOT / "bandit.yaml").read_text(encoding="utf-8"))
    precommit = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))

    vendor_prefix = f"{VENDOR_ROOT}/"
    assert vendor_prefix in project["tool"]["ruff"]["exclude"]
    assert f"^{vendor_prefix}" in project["tool"]["mypy"]["exclude"]
    assert vendor_prefix in bandit["exclude_dirs"]
    assert project["tool"]["coverage"]["run"]["source"] == ["opendata"]
    assert f"{vendor_prefix}*" in project["tool"]["coverage"]["run"]["omit"]

    ruff_patterns = project["tool"]["ruff"]["exclude"]
    mypy_patterns = project["tool"]["mypy"]["exclude"]
    for path in (
        "opendata/data/providers/akshare/adapter.py",
        "opendata/data/providers/akshare/_vendorish/adapter.py",
        "other/_vendor/adapter.py",
    ):
        assert not any(re.search(pattern, path) for pattern in mypy_patterns)
        assert classify_path(path) != PORTED
    assert [
        pattern
        for pattern in ruff_patterns
        if "akshare" in pattern.casefold() or "vendor" in pattern.casefold()
    ] == [vendor_prefix]
    assert [
        pattern
        for pattern in mypy_patterns
        if "akshare" in pattern.casefold() or "vendor" in pattern.casefold()
    ] == [f"^{vendor_prefix}"]

    hooks = {
        hook["id"]: hook
        for repository in precommit["repos"]
        for hook in repository.get("hooks", [])
    }
    assert hooks["ruff"]["exclude"] == f"^{vendor_prefix}"
    assert hooks["ruff-format"]["exclude"] == f"^{vendor_prefix}"
    ruff_rev = next(repo["rev"] for repo in precommit["repos"] if "ruff-pre-commit" in repo["repo"])
    bandit_rev = next(repo["rev"] for repo in precommit["repos"] if "PyCQA/bandit" in repo["repo"])
    assert ruff_rev == "v0.15.20"
    assert bandit_rev == "1.9.4"


def test_zero_dependency_baseline_keeps_partition_guards_after_historical_rekey() -> None:
    raw = json.loads(
        (REPO_ROOT / "docs/quality/zero-dep-baseline.json").read_text(encoding="utf-8")
    )
    baseline = verify_no_akshare._parse_baseline(raw)

    expected_scope = ("opendata", VENDOR_ROOT, "opendata_client")
    assert expected_scope == verify_no_akshare.DEFAULT_TARGETS
    assert baseline.scope == expected_scope
    assert len(set(baseline.scope)) == len(baseline.scope)
    assert set(baseline.files) == set(expected_scope)
    assert all(type(count) is int for count in baseline.files.values())
    assert baseline.files[VENDOR_ROOT] == 325
    assert baseline.files["opendata_client"] == 2
    historical_opendata_lower_bound = 215
    assert baseline.files["opendata"] >= historical_opendata_lower_bound
    assert baseline.entries == ()
    raw_policy = json.loads(
        (REPO_ROOT / "docs/quality/akshare-reference-allowlist.json").read_text(encoding="utf-8")
    )
    policy = verify_no_akshare.parse_reference_policy(raw_policy)
    entry = policy.by_path["opendata/data/providers/ths/endpoint_map.yaml"]
    assert entry.sha256 == "b391ee24ad40a1eefc306083cddbd10c463dfd0f4f13477bb90cb98ecd99e777"
    assert hashlib.sha256((REPO_ROOT / entry.path).read_bytes()).hexdigest() == entry.sha256

    census = verify_no_akshare.file_census(verify_no_akshare.DEFAULT_TARGETS)
    unique_files = verify_no_akshare.iter_target_files(verify_no_akshare.DEFAULT_TARGETS)
    assert set(census) == set(expected_scope)
    assert all(type(count) is int for count in census.values())
    assert census[VENDOR_ROOT] == 325
    assert census["opendata_client"] == 2
    assert census["opendata"] >= baseline.files["opendata"]
    assert len(unique_files) == sum(census.values())

    one_file_short = dict(baseline.files)
    one_file_short["opendata"] -= 1
    shrink_problems = verify_no_akshare.surface_problems(baseline, one_file_short)
    assert any(verify_no_akshare.SURFACE_SHRANK in problem for problem in shrink_problems)
