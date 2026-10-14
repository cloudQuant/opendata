"""Isolated Docker/MySQL and loopback helpers for the AC21 browser contract.

The test owns one uniquely named MySQL container, ordinary bridge network, and
volume. Only a random 127.0.0.1 host port is published. The guard allows SQLAlchemy
connections only to that container's port and unique schema; generated credentials
are never written to evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

MYSQL_IMAGE_REPOSITORY = "docker.io/library/mysql"
MYSQL_IMAGE_DIGEST = "sha256:ca3f0494c0f1fc86eb45f5e4786a1bb9f64d2f85b562cc74a9595046f6519a42"
MYSQL_IMAGE_REFERENCE = f"{MYSQL_IMAGE_REPOSITORY}@{MYSQL_IMAGE_DIGEST}"
MYSQL_IMAGE_SHORT_REPO_DIGEST = f"mysql@{MYSQL_IMAGE_DIGEST}"


def _mysql_image_inspect_args() -> list[str]:
    """Inspect the locally available canonical MySQL image without pulling."""
    return [
        "image",
        "inspect",
        "--format",
        "{{json .RepoDigests}}",
        MYSQL_IMAGE_REFERENCE,
    ]


def _mysql_image_run_args() -> list[str]:
    """Use the same immutable image reference and prohibit a pull at create time."""
    return ["--pull=never", MYSQL_IMAGE_REFERENCE]


def _has_pinned_mysql_repo_digest(repo_digests: object) -> bool:
    """Require only the two reviewed MySQL repository spellings and exact digest."""
    allowed_repo_digests = {MYSQL_IMAGE_REFERENCE, MYSQL_IMAGE_SHORT_REPO_DIGEST}
    return (
        type(repo_digests) is list
        and bool(repo_digests)
        and all(
            type(repo_digest) is str and repo_digest in allowed_repo_digests
            for repo_digest in repo_digests
        )
    )


def _validate_mysql_image_inspection(stdout: str) -> None:
    """Fail closed when inspect output does not prove the pinned local digest."""
    try:
        repo_digests = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(
            "Docker image inspection did not confirm the pinned MySQL digest"
        ) from None
    if not _has_pinned_mysql_repo_digest(repo_digests):
        raise RuntimeError("Docker image inspection did not confirm the pinned MySQL digest")


def _stderr_fingerprint(stderr: object) -> tuple[str | None, int | None]:
    """Return only a digest and byte length for captured stderr, never its content."""
    if isinstance(stderr, bytes):
        raw_stderr = stderr
    elif isinstance(stderr, str):
        raw_stderr = stderr.encode("utf-8", errors="replace")
    else:
        return None, None
    return hashlib.sha256(raw_stderr).hexdigest(), len(raw_stderr)


def _decode_docker_output(output: object) -> str:
    """Decode successful byte output for the existing string-based harness API."""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    if isinstance(output, str):
        return output
    return ""


class DatabaseTargetError(RuntimeError):
    """Raised before connecting to a SQLAlchemy URL outside the run lease."""


class DockerCommandError(RuntimeError):
    """Describe a failed Docker command without retaining argv or process text."""

    def __init__(
        self,
        *,
        failure_class: str,
        stage: str,
        return_code: int | None,
        stderr_sha256: str | None = None,
        stderr_bytes: int | None = None,
    ) -> None:
        self.failure_class = failure_class
        self.stage = stage
        self.return_code = return_code
        self.stderr_sha256 = stderr_sha256
        self.stderr_bytes = stderr_bytes
        stderr_summary = ""
        if stderr_sha256 is not None and stderr_bytes is not None:
            stderr_summary = f" stderr_sha256={stderr_sha256} stderr_bytes={stderr_bytes}"
        super().__init__(
            f"Docker command failed: class={failure_class} stage={stage} "
            f"return_code={return_code}{stderr_summary}"
        )


class DockerCommandResult(subprocess.CompletedProcess[str]):
    """Keep successful-process stderr fingerprints without retaining raw bytes."""

    def __init__(
        self,
        *,
        args: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
        stderr_sha256: str | None,
        stderr_bytes: int | None,
    ) -> None:
        super().__init__(args=args, returncode=returncode, stdout=stdout, stderr=stderr)
        self.stderr_sha256 = stderr_sha256
        self.stderr_bytes = stderr_bytes


@dataclass(frozen=True)
class MysqlTarget:
    run_id: str
    container_name: str
    container_id: str
    network_name: str
    network_id: str
    volume_name: str
    host_port: int
    schema: str
    username: str
    password: str
    root_password: str
    owner_label: str = "opendata-c100-ac21"

    def url(self) -> URL:
        """Return the async URL for this run's local-only schema."""
        return URL.create(
            "mysql+aiomysql",
            username=self.username,
            password=self.password,
            host="127.0.0.1",
            port=self.host_port,
            database=self.schema,
        )

    def public_manifest(self) -> dict[str, Any]:
        """Expose resource ownership without exposing generated credentials."""
        return {
            "run_id": self.run_id,
            "owner_label": self.owner_label,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "network_name": self.network_name,
            "network_id": self.network_id,
            "volume_name": self.volume_name,
            "host_ip": "127.0.0.1",
            "host_port": self.host_port,
            "schema": self.schema,
            "username": self.username,
            "credentials_recorded": False,
        }


def guard_database_url(
    url: URL,
    *,
    target: MysqlTarget,
    role: str,
    allowed_sqlite_path: Path | None = None,
) -> None:
    """Validate a database destination before creating or opening a session.

    The app-metadata SQLite exception is exact-path only. MySQL URLs must use
    the test lease's loopback port, schema, and synthetic username.
    """
    if (
        role == "app-metadata"
        and allowed_sqlite_path is not None
        and url.get_backend_name() == "sqlite"
        and url.database
        and Path(url.database).resolve() == allowed_sqlite_path.resolve()
    ):
        return

    if (
        url.drivername == "mysql+aiomysql"
        and url.host == "127.0.0.1"
        and url.port == target.host_port
        and url.database == target.schema
        and url.username == target.username
    ):
        return

    # Do not interpolate the URL: it can contain credentials or user database names.
    raise DatabaseTargetError(f"blocked SQLAlchemy target for role {role!r}")


def guarded_connect(engine: Any, *, target: MysqlTarget, role: str) -> Any:
    """Call connect only after its SQLAlchemy URL passed the target guard."""
    guard_database_url(engine.url, target=target, role=role)
    return engine.connect()


class OwnedMysql:
    """Create, record, and remove exactly one AC21-owned local MySQL lease."""

    owner_label = "opendata-c100-ac21"

    def __init__(self) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{timestamp}_{secrets.token_hex(4)}"
        self.container_name = f"opendata-c100-ac21-{self.run_id.lower().replace('_', '-')}"
        self.network_name = f"{self.container_name}-network"
        self.volume_name = f"{self.container_name}-volume"
        self.schema = f"ac21_{self.run_id.split('_', 1)[1]}"
        self.username = f"ac21_{self.run_id.split('_', 1)[1]}"
        self.password = secrets.token_urlsafe(28)
        self.root_password = secrets.token_urlsafe(32)
        self.container_id: str | None = None
        self.network_id: str | None = None
        self.host_port: int | None = None
        self.last_readiness_result: dict[str, Any] | None = None
        self.container_recovery: dict[str, object] = {
            "attempted": False,
            "state": "NOT_NEEDED",
        }
        self.network_recovery: dict[str, object] = {
            "attempted": False,
            "state": "NOT_NEEDED",
        }
        self.cleanup_errors: list[dict[str, str | int | None]] = []

    def public_manifest(self) -> dict[str, Any]:
        """Return the test lease identity without either generated password."""
        return {
            "run_id": self.run_id,
            "owner_label": self.owner_label,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "network_name": self.network_name,
            "network_id": self.network_id,
            "network_recovery": dict(self.network_recovery),
            "volume_name": self.volume_name,
            "host_ip": "127.0.0.1",
            "host_port": self.host_port,
            "schema": self.schema,
            "username": self.username,
            "credentials_recorded": False,
        }

    def capture_diagnostics(self, evidence_dir: Path) -> dict[str, Any]:
        """Persist this run's redacted logs and narrowly selected resource state."""
        if self.container_id is None:
            return {"captured": False, "reason": "container was not created"}

        evidence_dir.mkdir(parents=True, exist_ok=True)
        logs = self._docker(
            ["logs", "--tail", "200", self.container_id],
            check=False,
        )
        log_text = self._redact(f"{logs.stdout}\n{logs.stderr}")
        log_path = evidence_dir / "mysql-container-logs-redacted.txt"
        log_path.write_text(log_text, encoding="utf-8")

        inspect_format = (
            "{{.Id}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}|"
            "{{.State.OOMKilled}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|"
            "{{.State.Restarting}}"
        )
        runtime = self._docker(
            ["inspect", "--format", inspect_format, self.container_id],
            check=False,
        )
        network_state: dict[str, Any] = {"captured": False}
        if self.network_id is not None and re.fullmatch(r"[0-9a-f]{64}", self.network_id):
            network_format = (
                "{{.Id}}|{{.Name}}|{{.Driver}}|{{.Internal}}|"
                '{{index .Labels "codex.owner"}}|{{index .Labels "codex.run-id"}}'
            )
            network = self._docker(
                ["network", "inspect", "--format", network_format, self.network_id],
                check=False,
            )
            network_state = {
                "captured": network.returncode == 0,
                "fields": "id,name,driver,internal,owner_label,run_id",
                "result": self._redact(network.stdout.strip()),
                "exit_code": network.returncode,
            }
        return {
            "captured": True,
            "container_id": self.container_id,
            "mysql_logs": {
                "path": log_path.name,
                "sha256": hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
                "bytes": len(log_text.encode("utf-8")),
                "exit_code": logs.returncode,
            },
            "runtime_inspect": {
                "fields": "id,status,exit_code,error,oom_killed,started_at,finished_at,restarting",
                "result": self._redact(runtime.stdout.strip()),
                "exit_code": runtime.returncode,
            },
            "network_inspect": network_state,
            "last_readiness_result": self.last_readiness_result,
        }

    def _redact(self, value: str) -> str:
        """Remove run-generated credentials before persisting diagnostics."""
        for secret in (self.password, self.root_password):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def start(self) -> MysqlTarget:
        """Start an isolated, volume-backed container without pulling images."""
        image_inspection = self._docker(_mysql_image_inspect_args())
        try:
            _validate_mysql_image_inspection(image_inspection.stdout)
        except RuntimeError:
            raise DockerCommandError(
                failure_class="PinnedImageDigestMismatch",
                stage="image-inspect",
                return_code=image_inspection.returncode,
                stderr_sha256=image_inspection.stderr_sha256,
                stderr_bytes=image_inspection.stderr_bytes,
            ) from None
        network = self._docker(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                f"codex.owner={self.owner_label}",
                "--label",
                f"codex.run-id={self.run_id}",
                self.network_name,
            ]
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", network):
            raise RuntimeError("Docker did not return a valid AC21 network ID")
        self.network_id = network

        volume = self._docker(
            [
                "volume",
                "create",
                "--label",
                f"codex.owner={self.owner_label}",
                "--label",
                f"codex.run-id={self.run_id}",
                self.volume_name,
            ]
        ).stdout.strip()
        if volume != self.volume_name:
            raise RuntimeError("Docker returned an unexpected AC21 volume identity")

        image_run_args = _mysql_image_run_args()
        result = self._docker(
            [
                "run",
                "--detach",
                *image_run_args[:1],
                "--name",
                self.container_name,
                "--label",
                f"codex.owner={self.owner_label}",
                "--label",
                f"codex.run-id={self.run_id}",
                "--network",
                self.network_name,
                "--publish",
                "127.0.0.1::3306",
                "--mount",
                f"type=volume,source={self.volume_name},target=/var/lib/mysql",
                "--env",
                f"MYSQL_ROOT_PASSWORD={self.root_password}",
                "--env",
                f"MYSQL_DATABASE={self.schema}",
                "--env",
                f"MYSQL_USER={self.username}",
                "--env",
                f"MYSQL_PASSWORD={self.password}",
                image_run_args[1],
                "--log-bin-trust-function-creators=1",
            ]
        )
        returned_container_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", returned_container_id):
            raise RuntimeError("Docker did not return a valid AC21 container ID")
        self.container_id = returned_container_id

        published = self._docker(["port", self.container_id, "3306/tcp"]).stdout.strip()
        self.host_port = self._parse_loopback_port(published)
        self._wait_ready()
        return MysqlTarget(
            run_id=self.run_id,
            container_name=self.container_name,
            container_id=self.container_id,
            network_name=self.network_name,
            network_id=self.network_id,
            volume_name=self.volume_name,
            host_port=self.host_port,
            schema=self.schema,
            username=self.username,
            password=self.password,
            root_password=self.root_password,
        )

    def _cleanup_command(self, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Run one cleanup lookup/removal and retain only safe failure identity."""
        try:
            result = self._docker(args, check=False)
        except (DockerCommandError, OSError, RuntimeError) as exc:
            if isinstance(exc, DockerCommandError):
                failure_class = exc.failure_class
                stage = exc.stage
                return_code = exc.return_code
            else:
                failure_class = type(exc).__name__
                stage = self._docker_stage(args)
                return_code = None
            safe_error: dict[str, str | int | None] = {
                "stage": stage,
                "failure_class": failure_class,
                "return_code": return_code,
            }
            if isinstance(exc, DockerCommandError):
                if exc.stderr_sha256 is not None:
                    safe_error["stderr_sha256"] = exc.stderr_sha256
                if exc.stderr_bytes is not None:
                    safe_error["stderr_bytes"] = exc.stderr_bytes
            self.cleanup_errors.append(safe_error)
            return None
        if result.returncode != 0:
            safe_nonzero_error: dict[str, str | int | None] = {
                "stage": self._docker_stage(args),
                "failure_class": "NonzeroExit",
                "return_code": result.returncode,
            }
            if isinstance(result, DockerCommandResult):
                if result.stderr_sha256 is not None:
                    safe_nonzero_error["stderr_sha256"] = result.stderr_sha256
                if result.stderr_bytes is not None:
                    safe_nonzero_error["stderr_bytes"] = result.stderr_bytes
            self.cleanup_errors.append(safe_nonzero_error)
        return result

    def recover_created_container(self) -> dict[str, object]:
        """Recover a lost create response only after exact name, ID and labels verify."""
        if self.container_id is not None:
            return {"attempted": False, "state": "ID_ALREADY_AVAILABLE"}
        if self.container_recovery.get("attempted") is True:
            return dict(self.container_recovery)

        self.container_recovery = {"attempted": True, "state": "LOOKUP_STARTED"}
        listing = self._cleanup_command(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label=codex.owner={self.owner_label}",
                "--filter",
                f"label=codex.run-id={self.run_id}",
                "--format",
                "{{.ID}}|{{.Names}}",
            ]
        )
        if listing is None:
            self.container_recovery = {"attempted": True, "state": "LOOKUP_ERROR"}
            return dict(self.container_recovery)
        if listing.returncode != 0:
            self.container_recovery = {"attempted": True, "state": "LOOKUP_FAILED"}
            return dict(self.container_recovery)

        entries = [
            line.strip().split("|", 1) for line in listing.stdout.splitlines() if line.strip()
        ]
        if not entries:
            self.container_recovery = {"attempted": True, "state": "NO_MATCH"}
            return dict(self.container_recovery)
        if len(entries) != 1 or len(entries[0]) != 2:
            self.container_recovery = {"attempted": True, "state": "AMBIGUOUS_MATCH"}
            return dict(self.container_recovery)
        candidate_id, candidate_name = entries[0]
        if candidate_name != self.container_name:
            self.container_recovery = {"attempted": True, "state": "NAME_MISMATCH"}
            return dict(self.container_recovery)
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
            self.container_recovery = {"attempted": True, "state": "INVALID_ID"}
            return dict(self.container_recovery)

        inspection = self._cleanup_command(
            [
                "inspect",
                "--format",
                (
                    '{{.Id}}|{{.Name}}|{{index .Config.Labels "codex.owner"}}|'
                    '{{index .Config.Labels "codex.run-id"}}'
                ),
                candidate_id,
            ]
        )
        if inspection is None or inspection.returncode != 0:
            self.container_recovery = {"attempted": True, "state": "INSPECT_FAILED"}
            return dict(self.container_recovery)
        fields = inspection.stdout.strip().split("|")
        if fields != [
            candidate_id,
            f"/{self.container_name}",
            self.owner_label,
            self.run_id,
        ]:
            self.container_recovery = {"attempted": True, "state": "IDENTITY_MISMATCH"}
            return dict(self.container_recovery)

        self.container_id = candidate_id
        self.container_recovery = {"attempted": True, "state": "RECOVERED"}
        return dict(self.container_recovery)

    def recover_created_network(self) -> dict[str, object]:
        """Recover a lost network-create response only after exact identity verifies."""
        if self.network_id is not None and re.fullmatch(r"[0-9a-f]{64}", self.network_id):
            self.network_recovery = {"attempted": False, "state": "ID_ALREADY_AVAILABLE"}
            return dict(self.network_recovery)
        if self.network_recovery.get("attempted") is True:
            return dict(self.network_recovery)

        self.network_id = None
        self.network_recovery = {"attempted": True, "state": "LOOKUP_STARTED"}
        listing = self._cleanup_command(
            [
                "network",
                "ls",
                "--no-trunc",
                "--filter",
                f"label=codex.owner={self.owner_label}",
                "--filter",
                f"label=codex.run-id={self.run_id}",
                "--format",
                "{{.ID}}|{{.Name}}",
            ]
        )
        if listing is None:
            self.network_recovery = {"attempted": True, "state": "LOOKUP_ERROR"}
            return dict(self.network_recovery)
        if listing.returncode != 0:
            self.network_recovery = {"attempted": True, "state": "LOOKUP_FAILED"}
            return dict(self.network_recovery)

        entries = [
            line.strip().split("|", 1) for line in listing.stdout.splitlines() if line.strip()
        ]
        if not entries:
            self.network_recovery = {"attempted": True, "state": "NO_MATCH"}
            return dict(self.network_recovery)
        if len(entries) != 1 or len(entries[0]) != 2:
            self.network_recovery = {"attempted": True, "state": "AMBIGUOUS_MATCH"}
            return dict(self.network_recovery)
        candidate_id, candidate_name = entries[0]
        if candidate_name != self.network_name:
            self.network_recovery = {"attempted": True, "state": "NAME_MISMATCH"}
            return dict(self.network_recovery)
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
            self.network_recovery = {"attempted": True, "state": "INVALID_ID"}
            return dict(self.network_recovery)

        inspect_format = (
            "{{.Id}}|{{.Name}}|{{.Driver}}|{{.Internal}}|"
            '{{index .Labels "codex.owner"}}|{{index .Labels "codex.run-id"}}'
        )
        inspection = self._cleanup_command(
            ["network", "inspect", "--format", inspect_format, candidate_id]
        )
        if inspection is None or inspection.returncode != 0:
            self.network_recovery = {"attempted": True, "state": "INSPECT_FAILED"}
            return dict(self.network_recovery)
        fields = inspection.stdout.strip().split("|")
        if fields != [
            candidate_id,
            self.network_name,
            "bridge",
            "false",
            self.owner_label,
            self.run_id,
        ]:
            self.network_recovery = {"attempted": True, "state": "IDENTITY_MISMATCH"}
            return dict(self.network_recovery)

        self.network_id = candidate_id
        self.network_recovery = {"attempted": True, "state": "RECOVERED"}
        return dict(self.network_recovery)

    def cleanup(self) -> dict[str, Any]:
        """Remove only resources whose exact IDs, names and owner labels match."""
        removed: dict[str, Any] = {"container": False, "network": False, "volume": False}
        if self.container_id is None:
            self.recover_created_container()
        if self.network_id is None or not re.fullmatch(r"[0-9a-f]{64}", self.network_id):
            self.recover_created_network()
        if self.container_id and re.fullmatch(r"[0-9a-f]{64}", self.container_id):
            inspection = self._cleanup_command(
                [
                    "inspect",
                    "--format",
                    (
                        '{{.Id}}|{{.Name}}|{{index .Config.Labels "codex.owner"}}|'
                        '{{index .Config.Labels "codex.run-id"}}'
                    ),
                    self.container_id,
                ]
            )
            fields = inspection.stdout.strip().split("|") if inspection is not None else []
            if (
                inspection is not None
                and inspection.returncode == 0
                and fields
                == [
                    self.container_id,
                    f"/{self.container_name}",
                    self.owner_label,
                    self.run_id,
                ]
            ):
                removal = self._cleanup_command(["rm", "--force", self.container_id])
                removed["container"] = removal is not None and removal.returncode == 0

        volume_inspection = self._cleanup_command(
            [
                "volume",
                "inspect",
                "--format",
                '{{.Name}} {{index .Labels "codex.owner"}} {{index .Labels "codex.run-id"}}',
                self.volume_name,
            ]
        )
        fields = volume_inspection.stdout.strip().split() if volume_inspection is not None else []
        if (
            volume_inspection is not None
            and volume_inspection.returncode == 0
            and fields
            == [
                self.volume_name,
                self.owner_label,
                self.run_id,
            ]
        ):
            removal = self._cleanup_command(["volume", "rm", self.volume_name])
            removed["volume"] = removal is not None and removal.returncode == 0

        if self.network_id is not None and re.fullmatch(r"[0-9a-f]{64}", self.network_id):
            network_inspection = self._cleanup_command(
                [
                    "network",
                    "inspect",
                    "--format",
                    (
                        "{{.Id}}|{{.Name}}|{{.Driver}}|{{.Internal}}|"
                        '{{index .Labels "codex.owner"}}|{{index .Labels "codex.run-id"}}'
                    ),
                    self.network_id,
                ]
            )
            fields = (
                network_inspection.stdout.strip().split("|")
                if network_inspection is not None
                else []
            )
            if (
                network_inspection is not None
                and network_inspection.returncode == 0
                and fields
                == [
                    self.network_id,
                    self.network_name,
                    "bridge",
                    "false",
                    self.owner_label,
                    self.run_id,
                ]
            ):
                removal = self._cleanup_command(["network", "rm", self.network_id])
                removed["network"] = removal is not None and removal.returncode == 0
        return removed

    @staticmethod
    def _parse_loopback_port(published: str) -> int:
        lines = [line.strip() for line in published.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].startswith("127.0.0.1:"):
            raise RuntimeError("AC21 MySQL must publish exactly one 127.0.0.1 port")
        port = int(lines[0].rsplit(":", 1)[1])
        if not 1 <= port <= 65535 or port in {3306, 33565}:
            raise RuntimeError("Docker returned an invalid AC21 loopback port")
        return port

    def _wait_ready(self) -> None:
        assert self.container_id is not None
        # MySQL 8 initializes a fresh volume before the final server accepts
        # connections. On a busy shared Docker host this can take a few minutes.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = self._docker(
                [
                    "exec",
                    self.container_id,
                    "mysqladmin",
                    "ping",
                    "--host=127.0.0.1",
                    "--user=root",
                    f"--password={self.root_password}",
                ],
                check=False,
            )
            self.last_readiness_result = {
                "return_code": result.returncode,
                "stdout": self._redact(result.stdout[-2000:]),
                "stderr": self._redact(result.stderr[-2000:]),
            }
            if result.returncode == 0:
                return
            time.sleep(1)
        raise TimeoutError("AC21-owned MySQL did not become ready in 240 seconds")

    @staticmethod
    def _docker_stage(args: list[str]) -> str:
        """Map the fixed Docker argv prefixes to non-sensitive operation names."""
        if not args:
            return "unknown"
        if args[0] == "run":
            return "container-start"
        if args[0] == "port":
            return "container-port"
        if args[0] == "logs":
            return "container-logs"
        if args[0] == "ps":
            return "container-list"
        if args[0] == "inspect":
            return "container-inspect"
        if args[:2] == ["image", "inspect"]:
            return "image-inspect"
        if args[:2] == ["network", "create"]:
            return "network-create"
        if args[:2] == ["network", "ls"]:
            return "network-list"
        if args[:2] == ["network", "inspect"]:
            return "network-inspect"
        if args[:2] == ["network", "rm"]:
            return "network-remove"
        if args[:2] == ["volume", "create"]:
            return "volume-create"
        if args[:2] == ["volume", "inspect"]:
            return "volume-inspect"
        if args[:2] == ["volume", "rm"]:
            return "volume-remove"
        if args[0] == "rm":
            return "container-remove"
        return "other"

    def _docker(self, args: list[str], *, check: bool = True) -> DockerCommandResult:
        """Run a fixed Docker operation and redact secrets from all returned text."""
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker CLI is required for the AC21 owned database")
        # The CLI path is resolved once; argv is built only from the harness.
        command = [docker, *args]
        stage = self._docker_stage(args)
        failure: DockerCommandError | None = None
        result: subprocess.CompletedProcess[bytes] | None = None
        try:
            result = subprocess.run(  # noqa: S603
                command,
                check=check,
                capture_output=True,
                text=False,
                timeout=120,
                env={**os.environ, "DOCKER_CLI_HINTS": "false"},
            )
        except subprocess.CalledProcessError as exc:
            return_code = exc.returncode if isinstance(exc.returncode, int) else None
            stderr_sha256, stderr_bytes = _stderr_fingerprint(exc.stderr)
            failure = DockerCommandError(
                failure_class="CalledProcessError",
                stage=stage,
                return_code=return_code,
                stderr_sha256=stderr_sha256,
                stderr_bytes=stderr_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            stderr_sha256, stderr_bytes = _stderr_fingerprint(exc.stderr)
            failure = DockerCommandError(
                failure_class="TimeoutExpired",
                stage=stage,
                return_code=None,
                stderr_sha256=stderr_sha256,
                stderr_bytes=stderr_bytes,
            )
        except OSError:
            failure = DockerCommandError(failure_class="OSError", stage=stage, return_code=None)

        if failure is not None:
            # Raise after leaving the except block so no raw subprocess exception
            # remains as the diagnostic's implicit context.
            raise failure from None
        if result is None:
            raise RuntimeError("Docker command returned no result")
        stderr_sha256, stderr_bytes = _stderr_fingerprint(result.stderr)
        return DockerCommandResult(
            args=[docker, *(self._redact(argument) for argument in args)],
            returncode=result.returncode,
            stdout=self._redact(_decode_docker_output(result.stdout)),
            stderr=self._redact(_decode_docker_output(result.stderr)),
            stderr_sha256=stderr_sha256,
            stderr_bytes=stderr_bytes,
        )


def assert_guard_counterfactuals(target: MysqlTarget) -> list[dict[str, Any]]:
    """Prove default and unrelated local MySQL URLs fail before connect()."""

    class NeverConnect:
        def __init__(self, url: URL):
            self.url = url
            self.connect_calls = 0

        def connect(self) -> None:
            self.connect_calls += 1
            raise AssertionError("unsafe database connect should never run")

    candidates = [
        URL.create(
            "mysql+aiomysql",
            username="counterfactual",
            password="synthetic-only",
            host="localhost",
            port=3306,
            database="opendata",
        ),
        URL.create(
            "mysql+aiomysql",
            username="counterfactual",
            password="synthetic-only",
            host="127.0.0.1",
            port=33565,
            database="opendata_data",
        ),
        make_url("sqlite+aiosqlite:///:memory:"),
    ]
    observations: list[dict[str, Any]] = []
    for candidate in candidates:
        engine = NeverConnect(candidate)
        try:
            guarded_connect(engine, target=target, role="warehouse")
        except DatabaseTargetError:
            pass
        else:
            raise AssertionError("counterfactual unsafe SQLAlchemy URL was allowed")
        observations.append(
            {
                "driver": candidate.drivername,
                "rejected_before_connect": engine.connect_calls == 0,
                "connect_calls": engine.connect_calls,
                "credentials_recorded": False,
            }
        )
        if engine.connect_calls != 0:
            raise AssertionError("database target guard ran after connect")
    return observations


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
