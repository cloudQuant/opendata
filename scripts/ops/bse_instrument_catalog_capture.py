"""Capture one fixed BSE catalog response as private raw evidence.

The request shape mirrors the vendored AkShare
``stock_info.stock_info_bj_name_code`` endpoint and form. This command sends
only one bounded POST with ``page=0``. It deliberately does not decode JSON,
JSONP, page counts, fields, or instrument identities: all catalog and
pagination semantics remain unknown until the retained source bytes receive a
separate, evidence-based review.

The response is stored content-addressed below a new owner-private evidence
directory. Receipts include only the bounded request scope, body size and
digests, and fixed unknown statuses. This is a local capture record; it does
not establish source authenticity, market completeness, historical coverage,
or retention rights. No credentials, database, REST API, or scheduler are
used by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable as Callable  # noqa: TC003 -- runtime public API annotation
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import requests
from requests.adapters import HTTPAdapter

from opendata.data.http_client import (
    BoundedHttpResponse,
    GovernedHttpClient,
    HttpClientConfig,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["run_bse_instrument_catalog_capture"]

SOURCE = "bse.public"
CANONICAL_MODEL = "instrument_catalog"
HOST = "www.bse.cn"
ENDPOINT_PATH = "/nqxxController/nqxxCnzq.do"
ENDPOINT_URL = f"https://{HOST}{ENDPOINT_PATH}"
REFERER = "https://www.bse.cn/nq/listedcompany.html"
MAX_REQUESTS = 1
MAX_FORM_BYTES = 2_048
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 12.0
MAX_TASK_TIMEOUT_SECONDS = 20.0
FORM_FIELDS = (
    ("page", "0"),
    ("typejb", "T"),
    ("xxfcbj[]", "2"),
    ("xxzqdm", ""),
    ("sortfield", "xxzqdm"),
    ("sorttype", "asc"),
)
_REQUEST_SUMMARY_FIELDS = ("page", "typejb", "xxfcbj", "xxzqdm", "sortfield", "sorttype")
_SAFE_FAILURE_CODES = frozenset(
    {
        "client_contract_invalid",
        "evidence_directory_invalid",
        "evidence_directory_must_be_new",
        "evidence_directory_unavailable",
        "local_capture_operation_failed",
        "raw_evidence_persistence_failed",
        "request_failed_or_outcome_unknown",
        "response_contract_invalid",
        "response_body_too_large",
    }
)
_PATH_TYPE = type(Path())
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class BSECaptureError(RuntimeError):
    """A fixed, value-free failure code for local capture operations."""

    def __init__(self, code: str) -> None:
        """Create a sanitized failure without exposing response or path data."""
        self.code = code if code in _SAFE_FAILURE_CODES else "local_capture_operation_failed"
        super().__init__(self.code)


class _BoundedPostClient(Protocol):
    """The bounded form POST method needed by this command."""

    def post_form_bounded(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        max_form_bytes: int,
        max_body_bytes: int,
        overall_timeout: float,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Send one scoped form request and return its bounded response."""


@dataclass(frozen=True, slots=True)
class _LiveClient:
    """Owned governed client and its Requests session for deterministic cleanup."""

    client: GovernedHttpClient
    session: requests.Session


def run_bse_instrument_catalog_capture(
    evidence_dir: Path,
    *,
    client_factory: Callable[[], _BoundedPostClient] | None = None,
) -> dict[str, object]:
    """Capture one BSE catalog page without interpreting its source payload.

    With no injected client, this function requires an explicit development
    runtime and sends exactly one POST to the fixed HTTPS endpoint. The fake
    client seam is restricted to an explicit testing environment. No dotenv,
    application settings, or database configuration is loaded.

    Args:
        evidence_dir: Absolute path to a new owner-private evidence directory.
        client_factory: Test-only bounded client factory that performs no I/O.

    Returns:
        A value-free receipt with one request outcome and fixed unknown statuses.

    Raises:
        BSECaptureError: If the evidence path cannot be safely created.
    """
    if client_factory is None:
        _require_live_environment()
    else:
        _require_offline_fake_environment()

    directory_fd = _create_private_evidence_directory(evidence_dir)
    live_client: _LiveClient | None = None
    request_digest = _request_digest()
    request_attempted = False
    budget: RequestBudget | None = None
    result: dict[str, object]
    try:
        _write_json_exclusive(
            directory_fd,
            "run-reservation.json",
            _run_reservation(request_digest),
        )
        budget = _new_request_budget()
        started = time.monotonic()
        deadline = started + MAX_TASK_TIMEOUT_SECONDS
        artifact_writer = RawResponseArtifactWriter(
            evidence_dir / "raw",
            max_response_bytes=MAX_RESPONSE_BYTES,
        )

        if client_factory is None:
            live_client = _new_live_client()
            client: _BoundedPostClient = live_client.client
        else:
            client = client_factory()
            if not callable(getattr(client, "post_form_bounded", None)):
                raise BSECaptureError("client_contract_invalid")

        with request_execution_scope(
            source=SOURCE,
            canonical_model=CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
            inherited_scopes=(),
        ):
            request_attempted = True
            result = _capture_response(client, artifact_writer, request_digest)
        result["transport_attempts"] = budget.attempts_used
        _write_json_exclusive(directory_fd, "receipt.json", result)
        return result
    except BSECaptureError as exc:
        result = _failure_receipt(
            request_digest,
            failure_code=exc.code,
            request_attempted=request_attempted,
        )
        result["transport_attempts"] = budget.attempts_used if budget is not None else 0
        _write_json_if_missing(directory_fd, "receipt.json", result)
        return result
    except Exception:
        result = _failure_receipt(
            request_digest,
            failure_code="local_capture_operation_failed",
            request_attempted=request_attempted,
        )
        result["transport_attempts"] = budget.attempts_used if budget is not None else 0
        _write_json_if_missing(directory_fd, "receipt.json", result)
        return result
    finally:
        if live_client is not None:
            live_client.session.close()
        os.close(directory_fd)


def _capture_response(
    client: _BoundedPostClient,
    artifact_writer: RawResponseArtifactWriter,
    request_digest: str,
) -> dict[str, object]:
    """Send once, persist the exact body, and return an anonymous receipt."""
    try:
        response = client.post_form_bounded(
            ENDPOINT_URL,
            dict(FORM_FIELDS),
            max_form_bytes=MAX_FORM_BYTES,
            max_body_bytes=MAX_RESPONSE_BYTES,
            overall_timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
                "Referer": REFERER,
            },
            timeout=(4.0, 8.0),
            source=SOURCE,
        )
    except Exception:
        return _failure_receipt(
            request_digest,
            failure_code="request_failed_or_outcome_unknown",
            request_attempted=True,
        )

    if type(response) is not BoundedHttpResponse:
        return _failure_receipt(
            request_digest,
            failure_code="response_contract_invalid",
            request_attempted=True,
        )
    body = response.body
    status_code = response.status_code
    if (
        type(body) is not bytes
        or len(body) > MAX_RESPONSE_BYTES
        or type(status_code) is not int
        or not 200 <= status_code < 300
    ):
        return _failure_receipt(
            request_digest,
            failure_code=(
                "response_body_too_large"
                if type(body) is bytes and len(body) > MAX_RESPONSE_BYTES
                else "response_contract_invalid"
            ),
            request_attempted=True,
            http_status=status_code if type(status_code) is int else None,
            response_bytes=len(body) if type(body) is bytes else None,
            response_suppressed=True,
        )

    response_digest = hashlib.sha256(body).hexdigest()
    try:
        captured = artifact_writer.capture(
            (body,),
            source=RawResponseSource(
                provider_id="bse",
                dataset_id="instrument_catalog",
                operation_id="page_0",
            ),
            request_summary_sha256=request_digest,
            http_status=status_code,
            retention_class="restricted",
            source_history_limit="unknown",
        )
        manifest_digest = hashlib.sha256(captured.manifest_path.read_bytes()).hexdigest()
    except Exception:
        return _failure_receipt(
            request_digest,
            failure_code="raw_evidence_persistence_failed",
            request_attempted=True,
            http_status=status_code,
            response_bytes=len(body),
            response_sha256=response_digest,
            response_suppressed=False,
        )

    return _receipt(
        outcome="raw_page_captured",
        request_digest=request_digest,
        request_attempted=True,
        http_status=status_code,
        response_bytes=len(body),
        response_sha256=response_digest,
        raw_manifest_sha256=manifest_digest,
        raw_response_persisted=True,
        response_suppressed=False,
        failure_code=None,
    )


def _request_digest() -> str:
    """Hash fixed logical form values without persisting any response fields."""
    logical_form = {
        "page": "0",
        "typejb": "T",
        "xxfcbj": "2",
        "xxzqdm": "",
        "sortfield": "xxzqdm",
        "sorttype": "asc",
    }
    return request_summary_sha256(logical_form, allowed_fields=_REQUEST_SUMMARY_FIELDS)


def _receipt(
    *,
    outcome: str,
    request_digest: str,
    request_attempted: bool,
    http_status: int | None,
    response_bytes: int | None,
    response_sha256: str | None,
    raw_manifest_sha256: str | None,
    raw_response_persisted: bool,
    response_suppressed: bool,
    failure_code: str | None,
) -> dict[str, object]:
    """Return fixed metadata without source row values or response fields."""
    safe_outcome = (
        outcome
        if outcome in {"raw_page_captured", "partial_or_failed_capture"}
        else "partial_or_failed_capture"
    )
    if failure_code is None:
        safe_failure = None
    elif type(failure_code) is str and failure_code in _SAFE_FAILURE_CODES:
        safe_failure = failure_code
    else:
        safe_failure = "local_capture_operation_failed"
    return {
        "schema_version": 1,
        "outcome": safe_outcome,
        "source": SOURCE,
        "method": "POST",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "form_page_value": "0",
        "request_summary_sha256": request_digest,
        "request_summary_basis": "allowlisted_logical_form_fields; bracketed_key_normalized",
        "request_attempted": request_attempted,
        "transport_send_ceiling": MAX_REQUESTS,
        "transport_attempts": 1 if request_attempted else 0,
        "retry_policy": "none",
        "redirects": "disabled",
        "http_status": (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        ),
        "response_bytes": response_bytes
        if type(response_bytes) is int and 0 <= response_bytes <= MAX_RESPONSE_BYTES
        else None,
        "response_sha256": response_sha256,
        "raw_manifest_sha256": raw_manifest_sha256,
        "raw_response_persisted": raw_response_persisted,
        "raw_persistence_state": (
            "content_addressed_private" if raw_response_persisted else "not_persisted"
        ),
        "response_interpretation": "not_parsed",
        "catalog_fields": "unknown",
        "jsonp_envelope": "unknown",
        "total_pages": "unknown",
        "pagination": "unknown",
        "source_identity": "unknown",
        "market_completeness": "not_established",
        "historical_coverage": "not_established",
        "source_authenticity": "not_established_by_local_hash",
        "publication": "not_performed",
        "scheduled_collection": "not_configured",
        "database_write": "not_performed",
        "row_values_in_receipt": False,
        "response_suppressed": response_suppressed,
        "failure_code": safe_failure,
    }


def _failure_receipt(
    request_digest: str,
    *,
    failure_code: str,
    request_attempted: bool,
    http_status: int | None = None,
    response_bytes: int | None = None,
    response_sha256: str | None = None,
    response_suppressed: bool = True,
) -> dict[str, object]:
    """Return an anonymous fixed failure without transport or response details."""
    return _receipt(
        outcome="partial_or_failed_capture",
        request_digest=request_digest,
        request_attempted=request_attempted,
        http_status=http_status,
        response_bytes=response_bytes,
        response_sha256=response_sha256,
        raw_manifest_sha256=None,
        raw_response_persisted=False,
        response_suppressed=response_suppressed,
        failure_code=failure_code,
    )


def _run_reservation(request_digest: str) -> dict[str, object]:
    """Describe the exact fixed request scope before the single send."""
    return {
        "schema_version": 1,
        "source": SOURCE,
        "method": "POST",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "form_page_value": "0",
        "request_summary_sha256": request_digest,
        "request_attempt_ceiling": MAX_REQUESTS,
        "response_body_cap_bytes": MAX_RESPONSE_BYTES,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "task_timeout_seconds": MAX_TASK_TIMEOUT_SECONDS,
        "retry_policy": "none",
        "redirects": "disabled",
        "interpretation": "not_parsed",
        "pagination": "unknown",
        "market_completeness": "not_established",
    }


def _new_request_budget() -> RequestBudget:
    """Create one short-lived grant pinned to the exact BSE source and host."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="user_authorized_akshare_bse_canary_terms_unverified",
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        allowed_hosts={HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        grants=(grant,),
        profile=profile,
    )


def _new_live_client() -> _LiveClient:
    """Build a one-send client with environment routing and retries disabled."""
    session = requests.Session()
    session.trust_env = False
    session.headers["Accept-Encoding"] = "identity"
    session.mount("https://", HTTPAdapter(max_retries=0))
    session.mount("http://", HTTPAdapter(max_retries=0))
    config = HttpClientConfig(
        connect_timeout=4.0,
        read_timeout=8.0,
        max_attempts=1,
        backoff_base=0.0,
        backoff_jitter=0.0,
        rate_limit_per_host=1.0,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=0.5,
        default_headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "identity",
        },
        proxies={},
    )
    return _LiveClient(
        client=GovernedHttpClient(config, session=session, raw_response_cache=None),
        session=session,
    )


def _require_live_environment() -> None:
    """Require explicit development mode and refuse test processes."""
    if (
        os.environ.get("APP_ENV") != "development"
        or os.environ.get("TESTING", "").lower() == "true"
    ):
        raise BSECaptureError("local_capture_operation_failed")


def _require_offline_fake_environment() -> None:
    """Keep the injected fake seam inside an explicit offline test process."""
    if os.environ.get("APP_ENV") != "testing" or os.environ.get("TESTING", "").lower() != "true":
        raise BSECaptureError("local_capture_operation_failed")


def _create_private_evidence_directory(path: Path) -> int:
    """Create a new private leaf directory without changing ancestor modes."""
    if (
        type(path) is not _PATH_TYPE
        or not path.is_absolute()
        or ".." in path.parts
        or path.name in {"", ".", ".."}
        or not _O_DIRECTORY
        or not _O_NOFOLLOW
        or not hasattr(os, "getuid")
    ):
        raise BSECaptureError("evidence_directory_invalid")
    descriptor = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    child_descriptor: int | None = None
    try:
        for component in path.parent.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            raise BSECaptureError("evidence_directory_must_be_new") from None
        child_descriptor = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=descriptor,
        )
        info = os.fstat(child_descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BSECaptureError("evidence_directory_unavailable")
        os.fchmod(child_descriptor, 0o700)
        os.fsync(child_descriptor)
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(child_descriptor).st_mode) != 0o700:
            raise BSECaptureError("evidence_directory_unavailable")
        os.close(descriptor)
        descriptor = -1
        result = child_descriptor
        child_descriptor = None
        return result
    except BSECaptureError:
        raise
    except OSError:
        raise BSECaptureError("evidence_directory_unavailable") from None
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Atomically write one bounded private receipt without overwriting."""
    if name not in {"run-reservation.json", "receipt.json"}:
        raise BSECaptureError("local_capture_operation_failed")
    temporary_name = f".pending-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 32 * 1024:
            raise BSECaptureError("local_capture_operation_failed")
        view = memoryview(encoded)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short receipt write")
            view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise
    try:
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise


def _write_json_if_missing(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Preserve the first failure receipt if final receipt creation can be retried."""
    try:
        _write_json_exclusive(directory_fd, name, value)
    except FileExistsError:
        return
    except Exception:
        return


def main(argv: list[str] | None = None) -> int:
    """Run one explicit capture and print only its anonymous receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = run_bse_instrument_catalog_capture(args.evidence_dir)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt.get("outcome") == "raw_page_captured" else 1


if __name__ == "__main__":
    sys.exit(main())
