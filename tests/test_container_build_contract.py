from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses pytest's tomli dependency.
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _dockerfile_copy_sources(dockerfile: str) -> set[str]:
    normalized = re.sub(r"\\\s*\n", " ", dockerfile)
    sources: set[str] = set()
    for line in normalized.splitlines():
        tokens = shlex.split(line.strip())
        if not tokens or tokens[0].upper() != "COPY":
            continue
        arguments = tokens[1:]
        if any(argument.startswith("--from=") for argument in arguments):
            continue
        while arguments and arguments[0].startswith("--"):
            arguments = arguments[1:]
        if len(arguments) >= 2:
            sources.update(arguments[:-1])
    return sources


def _dockerignore_patterns() -> set[str]:
    text = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _requirement_contract(requirement: str) -> tuple[str, frozenset[str], str]:
    match = re.fullmatch(r"(?i)([a-z0-9_.-]+)(?:\[([^\]]+)\])?(.*)", requirement.strip())
    assert match is not None, f"Invalid dependency declaration: {requirement!r}"
    name, raw_extras, specifier = match.groups()
    extras = (
        frozenset(extra.strip().casefold() for extra in raw_extras.split(",") if extra.strip())
        if raw_extras
        else frozenset()
    )
    return name.casefold(), extras, specifier


def test_dockerfile_copies_current_packages_and_build_runtime_files() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    sources = _dockerfile_copy_sources(dockerfile)

    required_sources = {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "LICENSE-AKSHARE",
        "THIRD_PARTY_NOTICES.md",
        "opendata/",
        "opendata_client/",
        "alembic/",
        "alembic_data/",
        "alembic.ini",
        "alembic_data.ini",
        "logging_config.ini",
    }
    assert required_sources <= sources
    assert "opendata/" in sources
    assert not ({"opendata_http/", "opendata_fuyao/"} & sources)

    missing_sources = [
        source for source in sources if not (PROJECT_ROOT / source.rstrip("/")).exists()
    ]
    assert not missing_sources, f"Docker COPY sources do not exist: {missing_sources}"

    assert "RUN pip install --no-cache-dir ." in dockerfile
    assert "COPY akshare/" not in dockerfile
    assert "setup.cfg*" not in dockerfile


def test_dockerfile_preserves_python_frontend_and_single_worker_runtime() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.11-slim AS builder" in dockerfile
    assert "FROM node:20-alpine AS frontend-builder" in dockerfile
    assert "COPY --from=frontend-builder --chown=appuser:appuser /frontend/dist" in dockerfile
    assert "USER appuser" in dockerfile
    assert "gunicorn opendata.main:app -w ${WORKERS:-1}" in dockerfile


def test_sqlalchemy_asyncio_extra_matches_project_metadata_and_requirements() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    web_requirements = project["project"]["optional-dependencies"]["web"]
    metadata_matches = [
        requirement
        for requirement in web_requirements
        if re.match(r"(?i)^sqlalchemy(?:\[|[<=>!~])", requirement)
    ]
    requirements_lines = (
        (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )
    requirements_matches = [
        line.split("#", 1)[0].strip()
        for line in requirements_lines
        if re.match(r"(?i)^\s*sqlalchemy(?:\[|[<=>!~])", line)
    ]

    assert len(metadata_matches) == 1
    assert len(requirements_matches) == 1
    metadata_contract = _requirement_contract(metadata_matches[0])
    requirements_contract = _requirement_contract(requirements_matches[0])
    assert metadata_contract == requirements_contract
    assert metadata_contract == ("sqlalchemy", frozenset({"asyncio"}), ">=2.0.35")


def test_dockerignore_excludes_credentials_and_local_artifacts() -> None:
    patterns = _dockerignore_patterns()

    required_patterns = {
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        ".git",
        "**/__pycache__/",
        "*.py[cod]",
        ".venv/",
        "**/node_modules/",
        "frontend/dist/",
        "/data/",
        "/localdata/",
        "/local_data/",
        "/local-data/",
        "/raw/",
        "/raw_data/",
        "/raw-data/",
        "/logs/",
        "/backups/",
        "/ssl/",
        "/private/",
        "/certs/",
        "/docs/evidence/",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.crt",
        "*.cer",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/*.crt",
        "**/*.cer",
    }
    assert required_patterns <= patterns


def test_dockerignore_keeps_application_sources_and_package_data() -> None:
    patterns = _dockerignore_patterns()
    overly_broad_patterns = {
        "data",
        "data/",
        "**/data",
        "**/data/",
        "**/data/**",
        "*.json",
        "*.yaml",
        "*.yml",
        "*.js",
    }
    assert not (patterns & overly_broad_patterns)

    required_package_data = {
        "opendata/data/authority.json",
        "opendata/data/domains.yaml",
        "opendata/data/openbb_map.yaml",
        "opendata/data/mappings/akshare.yaml",
        "opendata/data/mappings/ths.yaml",
        "opendata/pipeline/schedules.yaml",
        "opendata/data/providers/akshare/_vendor/manifest.json",
        "opendata/data/providers/akshare/_vendor/upstream.lock",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/file_fold/calendar.json",
        "opendata/data/providers/akshare/_vendor/stock_feature/ths.js",
        "opendata/data/providers/ths/endpoint_map.yaml",
        "opendata/data/providers/ths/transport/error_messages.yaml",
        "opendata_client/opendata_client/client.py",
    }
    missing_package_data = [
        path for path in required_package_data if not (PROJECT_ROOT / path).is_file()
    ]
    assert not missing_package_data, f"Required package files are missing: {missing_package_data}"


def test_setuptools_package_discovery_keeps_client_separate() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    discovery = project["tool"]["setuptools"]["packages"]["find"]

    assert discovery["include"] == ["opendata", "opendata.*"]
    assert "opendata_client" not in discovery["include"]


def test_isolated_wheel_and_sdist_include_nested_vendor_resources(tmp_path: Path) -> None:
    """Build from a source whitelist outside the checkout and inspect both archives."""
    if importlib.util.find_spec("build") is None:
        pytest.skip("NOT_RUN: Python build frontend is not installed")

    source_root = tmp_path / "source"
    source_root.mkdir()
    for filename in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "LICENSE-AKSHARE",
        "THIRD_PARTY_NOTICES.md",
    ):
        shutil.copy2(PROJECT_ROOT / filename, source_root / filename)
    shutil.copytree(
        PROJECT_ROOT / "opendata",
        source_root / "opendata",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".pytest_cache"),
    )

    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    build_env = {
        name: value
        for name, value in os.environ.items()
        if name != "COVERAGE_PROCESS_START" and not name.startswith("COV_CORE_")
    }
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(output_dir),
        ],
        cwd=source_root,
        env=build_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]

    required = {
        "opendata/data/providers/akshare/_vendor/manifest.json",
        "opendata/data/providers/akshare/_vendor/upstream.lock",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/file_fold/calendar.json",
        "opendata/data/providers/akshare/_vendor/stock_feature/ths.js",
        "opendata/data/providers/ths/endpoint_map.yaml",
        "opendata/data/providers/ths/transport/error_messages.yaml",
    }
    wheels = list(output_dir.glob("*.whl"))
    sdists = list(output_dir.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    with ZipFile(wheels[0]) as archive:
        wheel_files = set(archive.namelist())
    with tarfile.open(sdists[0], "r:gz") as archive:
        sdist_files = {name.split("/", maxsplit=1)[1] for name in archive.getnames() if "/" in name}

    assert required <= wheel_files
    assert required <= sdist_files
    assert not any(name.startswith("opendata_client/") for name in wheel_files)
    assert not any(name.startswith("opendata_client/") for name in sdist_files)
    assert not any(name.startswith("opendata_http/") for name in sdist_files)
    assert not any(name.startswith("opendata_fuyao/") for name in sdist_files)
