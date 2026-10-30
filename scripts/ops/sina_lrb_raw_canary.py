"""Capture one bounded, unparsed Sina income-statement response as private raw evidence.

This one-shot canary copies the fixed request used by the vendored AkShare
``stock_financial_report_sina`` function for ``sh600519`` and ``lrb``. It does
not decode the response, infer statement semantics, or publish data. A successful
HTTP response is retained as content-addressed raw bytes with a value-free local
receipt. The receipt does not establish provider authenticity, data rights beyond
the user's authorization for this use, pagination, completeness, or history.
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
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["run_sina_lrb_raw_canary"]

SOURCE = "sina"
CANONICAL_MODEL = "financial_statement"
HOST = "quotes.sina.cn"
ENDPOINT_PATH = "/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
ENDPOINT_URL = f"https://{HOST}{ENDPOINT_PATH}"
QUERY_PARAMS = {
    "paperCode": "sh600519",
    "source": "lrb",
    "type": "0",
    "page": "1",
    "num": "1000",
}
REQUEST_SUMMARY = {
    "paper_code": "sh600519",
    "source": "lrb",
    "statement_type": "0",
    "page": "1",
    "num": "1000",
}
REQUEST_SUMMARY_FIELDS = tuple(REQUEST_SUMMARY)
MAX_REQUESTS = 1
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_TASK_TIMEOUT_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 5.0
_SAFE_FAILURE_CODES = frozenset(
    {
        "client_contract_invalid",
        "evidence_directory_invalid",
        "evidence_directory_must_be_new",
        "evidence_directory_unavailable",
        "governed_request_rejected",
        "local_capture_operation_failed",
        "raw_persistence_failed_or_unknown",
        "request_failed_or_outcome_unknown",
        "response_body_too_large",
        "response_contract_invalid",
    }
)
_SAFE_HTTP_FAILURE_CATEGORIES = frozenset(category.value for category in FailureCategory)
_PATH_TYPE = type(Path())
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class SinaCanaryError(RuntimeError):
    """A fixed, value-free error for local canary operations."""

    def __init__(self, code: str) -> None:
        """Create a sanitized failure without exposing a path or source response."""
        self.code = code if code in _SAFE_FAILURE_CODES else "local_capture_operation_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _LiveClient:
    """Owned governed client and its standard Requests session."""

    client: GovernedHttpClient
    session: requests.Session


def run_sina_lrb_raw_canary(
    evidence_dir: Path,
    *,
    client_factory: Callable[[], _LiveClient] | None = None,
) -> dict[str, object]:
    """Capture exactly one bounded Sina ``lrb`` response without interpreting it.

    With no factory, the command requires an explicit development runtime and
    creates its own non-proxied Requests session. The factory exists only for
    offline tests and must return the same governed client/session shape.

    Args:
        evidence_dir: Absolute path to a new owner-private evidence directory.
        client_factory: Test-only factory for a governed client using fake transport.

    Returns:
        A value-free receipt describing the single local request outcome.

    Raises:
        SinaCanaryError: If the local evidence path cannot be safely initialized.
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
    try:
        _write_json_exclusive(
            directory_fd,
            "run-reservation.json",
            _run_reservation(request_digest),
        )
        budget = _new_request_budget()
        deadline = time.monotonic() + MAX_TASK_TIMEOUT_SECONDS
        writer = RawResponseArtifactWriter(
            evidence_dir / "raw",
            max_response_bytes=MAX_RESPONSE_BYTES,
        )

        if client_factory is None:
            live_client = _new_live_client()
        else:
            candidate = client_factory()
            if (
                type(candidate) is not _LiveClient
                or type(candidate.client) is not GovernedHttpClient
                or type(candidate.session) is not requests.Session
            ):
                raise SinaCanaryError("client_contract_invalid")
            live_client = candidate

        with request_execution_scope(
            source=SOURCE,
            canonical_model=CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
            inherited_scopes=(),
        ):
            request_attempted = True
            receipt = _capture_response(live_client.client, writer, request_digest)
        receipt["budget_reservations"] = budget.attempts_used
        _write_json_exclusive(directory_fd, "receipt.json", receipt)
        return receipt
    except RequestBudgetError:
        receipt = _failure_receipt(
            request_digest,
            failure_code="governed_request_rejected",
            request_attempted=request_attempted,
            transport_attempts=0 if request_attempted else None,
        )
        receipt["budget_reservations"] = budget.attempts_used if budget is not None else 0
        _write_json_if_missing(directory_fd, "receipt.json", receipt)
        return receipt
    except SinaCanaryError as exc:
        receipt = _failure_receipt(
            request_digest,
            failure_code=exc.code,
            request_attempted=request_attempted,
        )
        receipt["budget_reservations"] = budget.attempts_used if budget is not None else 0
        _write_json_if_missing(directory_fd, "receipt.json", receipt)
        return receipt
    except Exception:
        receipt = _failure_receipt(
            request_digest,
            failure_code="local_capture_operation_failed",
            request_attempted=request_attempted,
        )
        receipt["budget_reservations"] = budget.attempts_used if budget is not None else 0
        _write_json_if_missing(directory_fd, "receipt.json", receipt)
        return receipt
    finally:
        if live_client is not None:
            live_client.session.close()
        os.close(directory_fd)


def _capture_response(
    client: GovernedHttpClient,
    writer: RawResponseArtifactWriter,
    request_digest: str,
) -> dict[str, object]:
    """Make one governed GET, preserve exact successful bytes, and emit no row values."""
    try:
        response = client.get_bounded(
            ENDPOINT_URL,
            params=QUERY_PARAMS,
            headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"},
            max_body_bytes=MAX_RESPONSE_BYTES,
            overall_timeout=REQUEST_TIMEOUT_SECONDS,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            source=SOURCE,
        )
    except RequestBudgetError:
        return _failure_receipt(
            request_digest,
            failure_code="governed_request_rejected",
            request_attempted=True,
            transport_attempts=0,
        )
    except HttpFetchError as exc:
        category, status, attempts = _http_failure_diagnostic(exc)
        return _failure_receipt(
            request_digest,
            failure_code="request_failed_or_outcome_unknown",
            request_attempted=True,
            http_status=status,
            failure_category=category,
            transport_attempts=attempts,
            http_response_observed=status is not None,
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
    if type(body) is not bytes:
        return _failure_receipt(
            request_digest,
            failure_code="response_contract_invalid",
            request_attempted=True,
            http_status=status_code if type(status_code) is int else None,
            response_suppressed=True,
            transport_attempts=1,
            http_response_observed=True,
        )
    if len(body) > MAX_RESPONSE_BYTES:
        return _failure_receipt(
            request_digest,
            failure_code="response_body_too_large",
            request_attempted=True,
            http_status=status_code if type(status_code) is int else None,
            response_suppressed=True,
            transport_attempts=1,
            http_response_observed=True,
        )
    if type(status_code) is not int or not 200 <= status_code < 300:
        return _failure_receipt(
            request_digest,
            failure_code="response_contract_invalid",
            request_attempted=True,
            http_status=status_code if type(status_code) is int else None,
            response_suppressed=True,
            transport_attempts=1,
            http_response_observed=True,
        )

    response_digest = hashlib.sha256(body).hexdigest()
    try:
        captured = writer.capture(
            (body,),
            source=RawResponseSource(
                provider_id="sina",
                dataset_id="financial_statement_lrb_raw",
                operation_id="sh600519_page_1",
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
            failure_code="raw_persistence_failed_or_unknown",
            request_attempted=True,
            http_status=status_code,
            response_bytes=len(body),
            response_sha256=response_digest,
            raw_persistence_state="unknown_after_writer_failure",
            response_suppressed=False,
            transport_attempts=1,
            http_response_observed=True,
        )

    return _receipt(
        outcome="raw_response_captured",
        request_digest=request_digest,
        request_attempted=True,
        http_status=status_code,
        response_bytes=len(body),
        response_sha256=response_digest,
        raw_manifest_sha256=manifest_digest,
        raw_response_persisted=True,
        raw_persistence_state="content_addressed_private",
        response_suppressed=False,
        failure_code=None,
        transport_attempts=1,
        http_response_observed=True,
    )


def _request_digest() -> str:
    """Hash the fixed logical parameters without persisting them in the manifest."""
    return request_summary_sha256(
        REQUEST_SUMMARY,
        allowed_fields=REQUEST_SUMMARY_FIELDS,
    )


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
    raw_persistence_state: str,
    response_suppressed: bool,
    failure_code: str | None,
    failure_category: str | None = None,
    budget_reservations: int = 0,
    transport_attempts: int | None = None,
    http_response_observed: bool | None = None,
) -> dict[str, object]:
    """Build a fixed anonymous receipt without source fields or local paths."""
    safe_failure = (
        failure_code
        if failure_code is None or failure_code in _SAFE_FAILURE_CODES
        else "local_capture_operation_failed"
    )
    if safe_failure is None:
        safe_category: str | None = None
    elif failure_category in _SAFE_HTTP_FAILURE_CATEGORIES:
        safe_category = failure_category
    else:
        safe_category = "unknown"
    observed = (
        http_response_observed
        if type(http_response_observed) is bool
        else (type(http_status) is int)
    )
    return {
        "schema_version": 1,
        "outcome": outcome
        if outcome in {"raw_response_captured", "partial_or_failed_capture"}
        else "partial_or_failed_capture",
        "source": SOURCE,
        "canonical_model": CANONICAL_MODEL,
        "method": "GET",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "query": dict(QUERY_PARAMS),
        "request_summary_sha256": request_digest,
        "request_attempted": request_attempted,
        "request_attempted_semantics": "bounded_client_invocation_only",
        "client_invoked": request_attempted,
        "delivery_status": (
            "response_received"
            if observed
            else "unknown"
            if request_attempted and transport_attempts != 0
            else "not_sent"
            if request_attempted
            else "not_invoked"
        ),
        "http_response_observed": observed,
        "transport_send_ceiling": MAX_REQUESTS,
        "budget_reservations": budget_reservations
        if type(budget_reservations) is int and 0 <= budget_reservations <= MAX_REQUESTS
        else 0,
        "transport_attempts": transport_attempts
        if type(transport_attempts) is int and 0 <= transport_attempts <= MAX_REQUESTS
        else None,
        "automatic_retry": False,
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
        "raw_persistence_state": raw_persistence_state,
        "response_interpretation": "not_attempted",
        "statement_identity": "not_validated",
        "pagination": "unknown",
        "historical_coverage": "not_established",
        "source_authenticity": "not_established_by_local_hash",
        "publication": "not_performed",
        "scheduled_collection": "not_configured",
        "database_write": "not_performed",
        "row_values_in_receipt": False,
        "response_suppressed": response_suppressed,
        "failure_code": safe_failure,
        "failure_category": safe_category,
    }


def _failure_receipt(
    request_digest: str,
    *,
    failure_code: str,
    request_attempted: bool,
    http_status: int | None = None,
    response_bytes: int | None = None,
    response_sha256: str | None = None,
    raw_persistence_state: str = "not_persisted",
    response_suppressed: bool = True,
    failure_category: str | None = None,
    budget_reservations: int = 0,
    transport_attempts: int | None = None,
    http_response_observed: bool | None = None,
) -> dict[str, object]:
    """Build a fixed terminal receipt while suppressing all exception text."""
    return _receipt(
        outcome="partial_or_failed_capture",
        request_digest=request_digest,
        request_attempted=request_attempted,
        http_status=http_status,
        response_bytes=response_bytes,
        response_sha256=response_sha256,
        raw_manifest_sha256=None,
        raw_response_persisted=False,
        raw_persistence_state=raw_persistence_state,
        response_suppressed=response_suppressed,
        failure_code=failure_code,
        failure_category=failure_category,
        budget_reservations=budget_reservations,
        transport_attempts=transport_attempts,
        http_response_observed=http_response_observed,
    )


def _http_failure_diagnostic(error: HttpFetchError) -> tuple[str, int | None, int | None]:
    """Extract only typed safe fields from the exact governed failure type."""
    if type(error) is not HttpFetchError:
        return "unknown", None, None
    category = error.category.value if type(error.category) is FailureCategory else "unknown"
    attempts = error.attempts if type(error.attempts) is int and 0 <= error.attempts <= 1 else None
    status = (
        error.status
        if attempts is not None
        and attempts > 0
        and type(error.status) is int
        and 100 <= error.status <= 599
        else None
    )
    return category, status, attempts


def _run_reservation(request_digest: str) -> dict[str, object]:
    """Describe the fixed one-shot request before client invocation."""
    return {
        "schema_version": 1,
        "source": SOURCE,
        "canonical_model": CANONICAL_MODEL,
        "method": "GET",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "request_summary_sha256": request_digest,
        "request_attempt_ceiling": MAX_REQUESTS,
        "response_body_cap_bytes": MAX_RESPONSE_BYTES,
        "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
        "read_timeout_seconds": READ_TIMEOUT_SECONDS,
        "overall_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "task_timeout_seconds": MAX_TASK_TIMEOUT_SECONDS,
        "automatic_retry": False,
        "redirects": "disabled",
        "response_interpretation": "not_attempted",
        "pagination": "unknown",
        "historical_coverage": "not_established",
    }


def _new_request_budget() -> RequestBudget:
    """Create a one-minute exact-source/host grant with a single local attempt."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="user_authorized_akshare_data_use",
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
    """Build a one-send, non-proxied governed client with response caching off."""
    session = requests.Session()
    session.trust_env = False
    session.auth = None
    session.proxies.clear()
    session.cookies.clear()
    session.headers["Accept-Encoding"] = "identity"
    session.mount("https://", HTTPAdapter(max_retries=0))
    session.mount("http://", HTTPAdapter(max_retries=0))
    config = HttpClientConfig(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        max_attempts=MAX_REQUESTS,
        backoff_base=0.0,
        backoff_jitter=0.0,
        rate_limit_per_host=1.0,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=0.0,
        default_headers={"Accept-Encoding": "identity"},
        proxies={},
    )
    client = GovernedHttpClient(config, session=session, raw_response_cache=None)
    return _LiveClient(client=client, session=session)


def _require_live_environment() -> None:
    """Require explicit development mode and refuse test processes."""
    if (
        os.environ.get("APP_ENV") != "development"
        or os.environ.get("TESTING", "").lower() == "true"
    ):
        raise SinaCanaryError("local_capture_operation_failed")


def _require_offline_fake_environment() -> None:
    """Require explicit testing mode for an injected governed fake transport."""
    if os.environ.get("APP_ENV") != "testing" or os.environ.get("TESTING", "").lower() != "true":
        raise SinaCanaryError("local_capture_operation_failed")


def _create_private_evidence_directory(path: Path) -> int:
    """Create a new 0700 leaf directory without changing any ancestor permissions."""
    if (
        type(path) is not _PATH_TYPE
        or not path.is_absolute()
        or ".." in path.parts
        or path.name in {"", ".", ".."}
        or not _O_DIRECTORY
        or not _O_NOFOLLOW
        or not hasattr(os, "getuid")
    ):
        raise SinaCanaryError("evidence_directory_invalid")
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
            raise SinaCanaryError("evidence_directory_must_be_new") from None
        child_descriptor = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=descriptor,
        )
        info = os.fstat(child_descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SinaCanaryError("evidence_directory_unavailable")
        os.fchmod(child_descriptor, 0o700)
        os.fsync(child_descriptor)
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(child_descriptor).st_mode) != 0o700:
            raise SinaCanaryError("evidence_directory_unavailable")
        os.close(descriptor)
        descriptor = -1
        result = child_descriptor
        child_descriptor = None
        return result
    except SinaCanaryError:
        raise
    except OSError:
        raise SinaCanaryError("evidence_directory_unavailable") from None
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Atomically write one bounded private JSON file without replacing a prior copy."""
    if name not in {"run-reservation.json", "receipt.json"}:
        raise SinaCanaryError("local_capture_operation_failed")
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
            raise SinaCanaryError("local_capture_operation_failed")
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
    """Keep the first terminal receipt if a later best-effort write races it."""
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
    receipt = run_sina_lrb_raw_canary(args.evidence_dir)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt.get("outcome") == "raw_response_captured" else 1


if __name__ == "__main__":
    sys.exit(main())
