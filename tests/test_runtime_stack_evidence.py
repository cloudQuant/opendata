"""Offline tests for saved C65 runtime evidence validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from scripts.ops.verify_isolated_stack import (
    APP_CONTAINER,
    APP_IMAGE_TAG,
    DB_SCHEMAS,
    ENDPOINT,
    ENDPOINT_HOST,
    ENDPOINT_PORT,
    PUBLISHED_PORT,
)
from scripts.quality.runtime_stack_evidence import (
    BUILD_LOG,
    BUILD_SOURCE,
    CONTAINER_INSPECT,
    COPY_ROOTS,
    DRIVER_LOG,
    EXPLICIT_COPY_FILES,
    STACK_REPORT,
    START_RUN,
    VERIFIER_SCRIPT,
    validate,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 30, 22, 45, tzinfo=timezone.utc)
BUILD_TIME = "2026-09-30T21:05:58.373788+00:00"
START_TIME = "2026-09-30T21:17:08.057406+00:00"
DRIVER_TIME = "2026-09-30T22:20:00+00:00"
BUILD_HEAD = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


def write_json(path: Path, document: object) -> None:
    """Write one readable evidence object under the fixture repository."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_bundle(root: Path) -> None:
    """Write a complete synthetic bundle without starting Docker or a service."""
    source_contents = {
        ".dockerignore": "node_modules/\n",
        "Dockerfile": dockerfile_source(),
        **{name: f"fixture for {name}\n" for name in EXPLICIT_COPY_FILES},
        **{
            f"{name}/{relative}": f"fixture source in {name}\n"
            for name, relative in {
                "opendata": "__init__.py",
                "opendata_client": "__init__.py",
                "alembic": "env.py",
                "alembic_data": "env.py",
                "frontend": "src/App.vue",
            }.items()
        },
        "opendata/data/providers/akshare/_vendor/__init__.py": (
            "fixture for nested ported source\n"
        ),
    }
    files: dict[str, str] = {}
    for name, content in source_contents.items():
        source_path = root / name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(content, encoding="utf-8")
        files[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source_identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    write_json(
        root / BUILD_SOURCE,
        {
            "date": BUILD_TIME,
            "source_identity": source_identity,
            "projection": (
                "current Docker COPY files from managed/nonignored set, "
                "excluding credential and cache paths"
            ),
            "files": files,
        },
    )
    build_command = (
        "docker build --label codex.task=opendata-c65 "
        f"--label codex.source={source_identity} -t {APP_IMAGE_TAG} ."
    )
    build_path = root / BUILD_LOG
    build_path.parent.mkdir(parents=True, exist_ok=True)
    build_path.write_text(
        f"Date: {BUILD_TIME}\nHEAD: {BUILD_HEAD}\nCommand: {build_command}\nBUILD_EXIT=0\n",
        encoding="utf-8",
    )
    verifier_path = root / VERIFIER_SCRIPT
    verifier_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_path.write_text("fixture verifier source\n", encoding="utf-8")
    verifier_sha256 = hashlib.sha256(verifier_path.read_bytes()).hexdigest()
    driver_path = root / DRIVER_LOG
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    driver_path.write_text(
        f"Date: {DRIVER_TIME}\n"
        f"HEAD: {BUILD_HEAD}\n"
        "Command: python scripts/ops/verify_isolated_stack.py --apply "
        f"--output {STACK_REPORT.as_posix()}\n"
        f"Verifier_SHA256: {verifier_sha256}\n"
        "RUNTIME_EXIT=0\n",
        encoding="utf-8",
    )
    write_json(
        root / START_RUN,
        {
            "round": "C65",
            "date": START_TIME,
            "command": "docker run 127.0.0.1:33566:8000; owned MySQL 33565",
            "container": APP_CONTAINER,
            "exit": 0,
            "identity": "c" * 64,
            "image_id": IMAGE_ID,
            "source_identity": source_identity,
            "native_3306_writes": 0,
            "target_schemas": [DB_SCHEMAS["MYSQL_DATABASE"], DB_SCHEMAS["DATA_MYSQL_DATABASE"]],
            "mysql_port": 33565,
            "emails_enabled": False,
            "upstream_credentials_supplied": False,
            "secret_values_recorded": False,
        },
    )

    port_binding = [{"HostIp": ENDPOINT_HOST, "HostPort": str(ENDPOINT_PORT)}]
    inspect_record = {
        "Id": "d" * 64,
        "Name": f"/{APP_CONTAINER}",
        "Image": IMAGE_ID,
        "Config": {
            "Image": APP_IMAGE_TAG,
            "Labels": {"codex.task": "opendata-c65", "codex.source": source_identity},
            "Env": [
                "MYSQL_HOST=opendata-iteration01-c65",
                "MYSQL_PORT=33565",
                f"MYSQL_DATABASE={DB_SCHEMAS['MYSQL_DATABASE']}",
                "DATA_MYSQL_HOST=opendata-iteration01-c65",
                "DATA_MYSQL_PORT=33565",
                f"DATA_MYSQL_DATABASE={DB_SCHEMAS['DATA_MYSQL_DATABASE']}",
            ],
        },
        "HostConfig": {"PortBindings": {PUBLISHED_PORT: port_binding}},
        "NetworkSettings": {"Ports": {PUBLISHED_PORT: port_binding}},
        "State": {"Status": "running"},
    }
    write_json(root / CONTAINER_INSPECT, [inspect_record])
    write_json(
        root / STACK_REPORT,
        {
            "date": datetime.fromisoformat(DRIVER_TIME).astimezone().date().isoformat(),
            "mode": "apply",
            "endpoint": ENDPOINT,
            "source": {"image_id": IMAGE_ID, "source_identity": source_identity},
            "health": {"database": "connected", "status": "healthy"},
            "static": {"asset_count": 5, "assets_valid": True, "home_http_200": True},
            "frontend_login": {
                "ok": True,
                "dashboard": {
                    "url": f"{ENDPOINT}/",
                    "title": "首页 - opendata",
                    "authenticated_element": ".user-dropdown .username",
                    "authenticated_element_visible": True,
                },
                "catalog": {
                    "url": f"{ENDPOINT}/data",
                    "title": "数据目录 - opendata",
                    "authenticated_element": ".user-dropdown .username",
                    "authenticated_element_visible": True,
                },
            },
            "userdb_write_isolated": True,
            "error": None,
        },
    )


def dockerfile_source() -> str:
    """Render a fixture Dockerfile whose context COPY sources match the fixed contract."""
    lines = [
        "FROM python:3.11-slim",
        "COPY requirements.txt .",
        "COPY pyproject.toml README.md LICENSE LICENSE-AKSHARE THIRD_PARTY_NOTICES.md ./",
        "COPY alembic.ini alembic_data.ini logging_config.ini ./",
        "COPY frontend/package.json frontend/package-lock.json ./",
    ]
    lines.extend(f"COPY {name}/ ./{name}/" for name in COPY_ROOTS)
    return "\n".join(lines) + "\n"


def validate_fixture(root: Path) -> Any:
    """Validate using a temporary fixture's current files as its managed inventory."""
    managed_paths = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    return validate(root, now=NOW, managed_paths=managed_paths)


def read_json(path: Path) -> Any:
    """Read a fixture object for one explicit tamper test."""
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite_source_identity(root: Path, *, remove_path: str) -> None:
    """Recompute the unkeyed file-map identity after a manifest omission attack."""
    manifest_path = root / BUILD_SOURCE
    manifest = read_json(manifest_path)
    files = manifest["files"]
    del files[remove_path]
    manifest["source_identity"] = hashlib.sha256(
        json.dumps(files, sort_keys=True).encode("utf-8")
    ).hexdigest()
    write_json(manifest_path, manifest)


def test_runtime_evidence_validates_current_isolated_apply_bundle(tmp_path: Path) -> None:
    """A complete same-build, same-image report survives a local-midnight boundary."""
    valid_bundle(tmp_path)

    result = validate_fixture(tmp_path)

    assert result.valid is True
    assert result.state == "validated"
    assert result.issues == ()
    assert result.facts["endpoint"] == ENDPOINT
    assert result.facts["health_database"] == "connected"
    assert result.facts["static_asset_count"] == "5"
    assert result.facts["frontend_identity"] == "verified"
    assert result.facts["driver_exit"] == "0"
    assert result.facts["driver_head"] == BUILD_HEAD


def test_runtime_evidence_binds_date_only_report_to_verifier_local_date(tmp_path: Path) -> None:
    """Date-only report metadata belongs to the verifier's local calendar day, not UTC start."""
    valid_bundle(tmp_path)
    report = read_json(tmp_path / STACK_REPORT)
    verifier_local_date = datetime.fromisoformat(DRIVER_TIME).astimezone().date()
    report["date"] = (verifier_local_date - timedelta(days=1)).isoformat()
    write_json(tmp_path / STACK_REPORT, report)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "runtime-stack-driver-date-mismatch" in {issue.code for issue in result.issues}


def test_runtime_evidence_rejects_stale_runtime_report_date(tmp_path: Path) -> None:
    """A date-only report older than the freshness window cannot validate the current stack."""
    valid_bundle(tmp_path)
    report = read_json(tmp_path / STACK_REPORT)
    report["date"] = "2026-09-28"
    write_json(tmp_path / STACK_REPORT, report)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "runtime-stack-date-stale" in {issue.code for issue in result.issues}


def test_runtime_evidence_rejects_projected_source_byte_drift(tmp_path: Path) -> None:
    """Current source bytes must still match every row bound into the image label."""
    valid_bundle(tmp_path)
    (tmp_path / "requirements.txt").write_text("tampered==9.9\n", encoding="utf-8")

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert result.valid is False
    assert "build-source-file-hash-mismatch" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    "remove_path",
    [
        "opendata/__init__.py",
        "opendata/data/providers/akshare/_vendor/__init__.py",
    ],
)
def test_runtime_evidence_rejects_manifest_omission_even_when_identity_is_recomputed(
    tmp_path: Path, remove_path: str
) -> None:
    """A self-consistent manifest still cannot omit a current managed COPY input."""
    valid_bundle(tmp_path)
    rewrite_source_identity(tmp_path, remove_path=remove_path)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "build-source-projection-set-mismatch" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("mutation", ["new-managed-file", "removed-managed-file"])
def test_runtime_evidence_reconciles_manifest_with_current_managed_projection(
    tmp_path: Path, mutation: str
) -> None:
    """New or removed source paths invalidate the build projection independently of its hashes."""
    valid_bundle(tmp_path)
    source_path = tmp_path / "opendata" / "new_module.py"
    if mutation == "new-managed-file":
        source_path.write_text("from opendata import __version__\n", encoding="utf-8")
    else:
        (tmp_path / "opendata" / "__init__.py").unlink()

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "build-source-projection-set-mismatch" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("mutation", ["port", "schema"])
def test_runtime_evidence_rejects_nonisolated_inspect_target(tmp_path: Path, mutation: str) -> None:
    """The saved inspect object must independently prove fixed ports and schemas."""
    valid_bundle(tmp_path)
    inspect_document = read_json(tmp_path / CONTAINER_INSPECT)
    container = inspect_document[0]
    if mutation == "port":
        container["HostConfig"]["PortBindings"][PUBLISHED_PORT][0]["HostPort"] = "33565"
    else:
        for index, entry in enumerate(container["Config"]["Env"]):
            if entry == "MYSQL_DATABASE=opendata":
                container["Config"]["Env"][index] = "MYSQL_DATABASE=wrong_schema"
    write_json(tmp_path / CONTAINER_INSPECT, inspect_document)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "container-inspect-invalid" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("identity_field", ["image_id", "source_identity"])
def test_runtime_evidence_rejects_report_image_or_source_mismatch(
    tmp_path: Path, identity_field: str
) -> None:
    """A live report is bound to the same source and image proved by inspect."""
    valid_bundle(tmp_path)
    report = read_json(tmp_path / STACK_REPORT)
    report["source"][identity_field] = (
        "sha256:" + "e" * 64 if identity_field == "image_id" else "e" * 64
    )
    write_json(tmp_path / STACK_REPORT, report)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "runtime-stack-image-source-mismatch" in {issue.code for issue in result.issues}


def test_runtime_evidence_requires_visible_authenticated_dashboard_and_catalog(
    tmp_path: Path,
) -> None:
    """A successful-looking stack report cannot omit or misidentify browser pages."""
    valid_bundle(tmp_path)
    report = read_json(tmp_path / STACK_REPORT)
    report["frontend_login"]["catalog"]["authenticated_element_visible"] = False
    write_json(tmp_path / STACK_REPORT, report)

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert "frontend-catalog-identity-invalid" in {issue.code for issue in result.issues}


def test_runtime_evidence_rejects_missing_browser_and_non_null_error(tmp_path: Path) -> None:
    """Either missing browser proof or a report error keeps the current run failed."""
    valid_bundle(tmp_path)
    report = read_json(tmp_path / STACK_REPORT)
    report["frontend_login"] = None
    report["error"] = {"type": "playwright_flow_failed"}
    write_json(tmp_path / STACK_REPORT, report)

    result = validate_fixture(tmp_path)

    codes = {issue.code for issue in result.issues}
    assert result.state == "failed"
    assert "frontend-login-missing" in codes
    assert "runtime-report-error" in codes


def test_runtime_evidence_distinguishes_not_started_from_failed(tmp_path: Path) -> None:
    """No runtime artifacts report not-started; a preflight report proves no apply run."""
    empty = validate_fixture(tmp_path)
    assert empty.state == "not-started"
    assert empty.valid is False

    write_json(tmp_path / STACK_REPORT, {"date": "2026-09-30", "mode": "preflight"})
    preflight = validate_fixture(tmp_path)
    assert preflight.state == "not-started"
    assert preflight.valid is False


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("missing", "runtime-driver-missing"),
        ("exit", "runtime-driver-exit-not-zero"),
        ("stale-hash", "runtime-driver-verifier-hash-mismatch"),
        ("stale-date", "runtime-driver-stale"),
        ("missing-date", "runtime-driver-date-invalid"),
        ("missing-head", "runtime-driver-head-invalid"),
    ],
)
def test_runtime_evidence_requires_a_current_successful_verifier_driver(
    tmp_path: Path, mutation: str, expected_issue: str
) -> None:
    """The apply report is accepted only with a fresh successful run of current verifier code."""
    valid_bundle(tmp_path)
    driver_path = tmp_path / DRIVER_LOG
    if mutation == "missing":
        driver_path.unlink()
    else:
        driver = driver_path.read_text(encoding="utf-8")
        if mutation == "exit":
            driver = driver.replace("RUNTIME_EXIT=0", "RUNTIME_EXIT=1")
        elif mutation == "stale-hash":
            driver = driver.replace(
                hashlib.sha256((tmp_path / VERIFIER_SCRIPT).read_bytes()).hexdigest(), "0" * 64
            )
        elif mutation == "stale-date":
            driver = driver.replace(DRIVER_TIME, "2026-09-29T21:30:00+00:00")
        elif mutation == "missing-date":
            driver = driver.replace(f"Date: {DRIVER_TIME}\n", "")
        else:
            driver = driver.replace(f"HEAD: {BUILD_HEAD}\n", "")
        driver_path.write_text(driver, encoding="utf-8")

    result = validate_fixture(tmp_path)

    assert result.state == "failed"
    assert expected_issue in {issue.code for issue in result.issues}
