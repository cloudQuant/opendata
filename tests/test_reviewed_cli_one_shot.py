"""Offline contract tests for the reviewed CLI one-shot operations."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from opendata.pipeline.run_dispatch import DispatchIdentity, DispatchReceipt, RunDispatcher
from opendata.pipeline.run_ledger import RunExecutionUncertainError, RunRecord
from opendata.pipeline.run_service import RunOutcome
from opendata.pipeline.runtime_roles import RuntimeRolePlan, build_runtime_role_plan
from opendata.reviewed_cli import ReviewedCLIApplication, run_reviewed_cli

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
RUN_KEY = "a" * 64
JOB_ID = "fuyao.financial.one-shot"
VERSION = "v1"


class _OneShotDispatcher(RunDispatcher):
    """Capture one-shot submissions without opening storage or running handlers."""

    def __init__(self) -> None:
        """Create an isolated in-memory dispatcher double."""
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    def dispatch_one_shot(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Record the request and return a synthetic bounded receipt."""
        self.calls.append((job_id, version, dict(payload), dry_run))
        return DispatchReceipt(
            identity=DispatchIdentity(
                run_key=RUN_KEY,
                run_type="manual",
                job_id=job_id,
                version=version,
                request_bytes=128,
            ),
            status="dry_run" if dry_run else "queued",
            created_at=None if dry_run else NOW,
        )


def _role_plan(role: str) -> RuntimeRolePlan:
    """Build one explicit reviewed process role for the command under test."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles={role},  # type: ignore[arg-type]
        reviewed_config_id="test.one-shot-cli",
        reviewed_sink_ids={"financial.statement"},
    )


def _body(operation: str, **fields: object) -> bytes:
    """Encode one bounded CLI request with a fixed schema version."""
    return json.dumps(
        {"schema_version": 1, "operation": operation, **fields},
        separators=(",", ":"),
    ).encode("utf-8")


def _invoke(body: bytes, app: ReviewedCLIApplication) -> tuple[int, dict[str, object], str]:
    """Invoke the CLI against in-memory streams and return its safe output."""
    out = io.StringIO()
    err = io.StringIO()
    code = run_reviewed_cli(
        ["--request", "-"],
        application=app,
        stdin=io.BytesIO(body),
        stdout=out,
        stderr=err,
    )
    text = out.getvalue() or err.getvalue()
    return code, json.loads(text), text


def _executor_app(worker: Callable[[str], RunOutcome | None]) -> ReviewedCLIApplication:
    """Build an executor application from a synthetic one-shot worker SPI."""
    return ReviewedCLIApplication(
        runtime_plan=_role_plan("one_shot_executor"),
        one_shot_run=worker,
    )


def _outcome(
    status: str,
    *,
    run_key: str = RUN_KEY,
    claim_policy: str = "one_shot",
    fencing_token: int = 1,
    finished: bool | None = None,
) -> RunOutcome:
    """Create a synthetic ledger result with intentionally private fields."""
    has_finished = status == "succeeded" if finished is None else finished
    record = RunRecord(
        run_key=run_key,
        job_snapshot_json='{"password":"do-not-return"}',
        status=status,  # type: ignore[arg-type]
        worker="private-worker-id",
        lease_id="private-lease-id",
        fencing_token=fencing_token,
        lease_expires_at=NOW + timedelta(minutes=1),
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        finished_at=NOW if has_finished else None,
        error_text="password=private-error-value",
        claim_policy=claim_policy,  # type: ignore[arg-type]
    )
    return RunOutcome(record=record, publication=None)


def test_one_shot_submit_uses_dedicated_role_and_dispatcher_receipt() -> None:
    """Route the exact safe manual payload through the one-shot dispatcher."""
    dispatcher = _OneShotDispatcher()
    app = ReviewedCLIApplication(
        runtime_plan=_role_plan("one_shot_submitter"),
        dispatcher=dispatcher,
    )
    body = _body(
        "one_shot_submit",
        job_id=JOB_ID,
        version=VERSION,
        payload={"idempotency_key": "first-run", "query": {"symbol": "000001.SZ"}},
    )

    code, receipt, text = _invoke(body, app)

    assert code == 0
    assert receipt == {
        "status": "queued",
        "run_key": RUN_KEY,
        "run_type": "manual",
        "job_id": JOB_ID,
        "version": VERSION,
        "request_bytes": 128,
    }
    assert dispatcher.calls == [
        (JOB_ID, VERSION, {"idempotency_key": "first-run", "query": {"symbol": "000001.SZ"}}, False)
    ]
    assert "private" not in text


def test_one_shot_submit_dry_run_is_forwarded_and_omitted_flag_defaults_false() -> None:
    """Preserve existing CLI dry-run default while safely forwarding an explicit preview."""
    dispatcher = _OneShotDispatcher()
    app = ReviewedCLIApplication(
        runtime_plan=_role_plan("one_shot_submitter"),
        dispatcher=dispatcher,
    )
    payload = {"idempotency_key": "preview"}

    code, receipt, _ = _invoke(
        _body("one_shot_submit", job_id=JOB_ID, version=VERSION, payload=payload, dry_run=True),
        app,
    )
    assert code == 0
    assert receipt["status"] == "dry_run"
    assert dispatcher.calls[-1] == (JOB_ID, VERSION, payload, True)

    code, receipt, _ = _invoke(
        _body("one_shot_submit", job_id=JOB_ID, version=VERSION, payload=payload),
        app,
    )
    assert code == 0
    assert receipt["status"] == "queued"
    assert dispatcher.calls[-1] == (JOB_ID, VERSION, payload, False)


def test_api_role_cannot_submit_one_shot_and_does_not_call_dispatcher() -> None:
    """Require the dedicated submitter role even when ordinary API dispatch is enabled."""
    dispatcher = _OneShotDispatcher()
    app = ReviewedCLIApplication(runtime_plan=_role_plan("api"), dispatcher=dispatcher)
    code, receipt, _ = _invoke(
        _body(
            "one_shot_submit",
            job_id=JOB_ID,
            version=VERSION,
            payload={"idempotency_key": "forbidden"},
        ),
        app,
    )
    assert code == 78
    assert receipt == {"status": "rejected", "code": "role_not_enabled"}
    assert dispatcher.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"job_id": JOB_ID, "version": VERSION},
        {
            "job_id": JOB_ID,
            "version": VERSION,
            "payload": {"idempotency_key": "invalid", "database_url": "sqlite:///secret.db"},
        },
        {
            "job_id": JOB_ID,
            "version": VERSION,
            "payload": {"idempotency_key": "invalid", "query": "mysql://user:secret@host/db"},
        },
        {
            "job_id": JOB_ID,
            "version": VERSION,
            "payload": {"idempotency_key": "invalid"},
            "worker": "not-accepted",
        },
    ],
)
def test_one_shot_submit_rejects_missing_payload_unsafe_values_and_extra_fields(
    fields: dict[str, object],
) -> None:
    """Reject malformed submission shapes before the injected dispatcher is called."""
    dispatcher = _OneShotDispatcher()
    app = ReviewedCLIApplication(
        runtime_plan=_role_plan("one_shot_submitter"),
        dispatcher=dispatcher,
    )
    code, receipt, text = _invoke(_body("one_shot_submit", **fields), app)
    assert code == 2
    assert receipt == {"status": "rejected", "code": "invalid_request"}
    assert dispatcher.calls == []
    assert "secret" not in text


def test_one_shot_run_dry_run_does_not_call_worker() -> None:
    """Return a preview receipt without reading the ledger through the SPI."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        return _outcome("succeeded")

    app = _executor_app(worker)
    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY, dry_run=True),
        app,
    )
    assert code == 0
    assert receipt == {"status": "dry_run", "run_key": RUN_KEY}
    assert calls == []


def test_one_shot_run_returns_only_confirmed_success_fields() -> None:
    """Expose success only for a complete record from the initial one-shot claim."""
    app = _executor_app(lambda run_key: _outcome("succeeded", run_key=run_key))
    code, receipt, text = _invoke(_body("one_shot_run", run_key=RUN_KEY), app)
    assert code == 0
    assert receipt == {"status": "succeeded", "run_key": RUN_KEY}
    assert "do-not-return" not in text
    assert "private-error-value" not in text
    assert "private-worker-id" not in text
    assert "private-lease-id" not in text


@pytest.mark.parametrize(
    "record_options",
    [
        {"claim_policy": "recover_expired"},
        {"fencing_token": 0},
        {"fencing_token": 2},
        {"finished": False},
    ],
)
def test_success_without_exact_one_shot_claim_evidence_is_uncertain(
    record_options: dict[str, object],
) -> None:
    """Do not let an injected SPI report ordinary or incomplete runs as success."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome:
        calls.append(run_key)
        return _outcome("succeeded", run_key=run_key, **record_options)  # type: ignore[arg-type]

    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]


def test_one_shot_run_none_returns_not_claimed_without_retry_guidance() -> None:
    """Do not infer completion or retryability from the SPI's no-result response."""
    calls: list[str] = []

    def worker(run_key: str) -> None:
        calls.append(run_key)
        return None

    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "not_claimed",
        "code": "one_shot_not_claimed",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]


def test_expired_one_shot_is_uncertain_with_fixed_code_and_no_retry() -> None:
    """Map uncertainty to one fixed receipt and call the injected SPI exactly once."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        error = RunExecutionUncertainError(run_key)
        error.args = ("credential=must-not-be-returned",)
        raise error

    code, receipt, text = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]
    assert "credential" not in text


def test_unclassified_one_shot_exception_is_uncertain_and_sanitized() -> None:
    """Treat any worker exception as potentially post-send and require reconciliation."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        raise RuntimeError("provider credential and response details")

    code, receipt, text = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]
    assert "credential" not in text
    assert "response details" not in text


def test_running_one_shot_outcome_is_reported_uncertain_without_retry() -> None:
    """Fail closed when a call returns a live lease after losing its durable fence."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome:
        calls.append(run_key)
        return _outcome("running", run_key=run_key)

    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "invalid"])
def test_non_successful_or_invalid_status_is_uncertain(status: str) -> None:
    """Avoid presenting non-success states as safe failures or clean cancellations."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome:
        calls.append(run_key)
        return _outcome(status, run_key=run_key)

    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]


@pytest.mark.parametrize(
    "outcome_factory",
    [
        lambda: RunOutcome(record=None, publication=None),
        lambda: RunOutcome(record=object(), publication=None),
        lambda: object(),
    ],
)
def test_invalid_or_missing_one_shot_outcome_is_uncertain(outcome_factory) -> None:
    """Fail closed when the worker cannot supply a typed durable result."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        return outcome_factory()  # type: ignore[return-value]

    code, receipt, _ = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]


def test_one_shot_run_key_mismatch_is_uncertain_without_leaking_returned_key() -> None:
    """Never report a result for a different run key as this request's outcome."""
    different_key = "b" * 64
    code, receipt, text = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(lambda _run_key: _outcome("succeeded", run_key=different_key)),
    )
    assert code == 1
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert different_key not in text


def test_keyboard_interrupt_during_one_shot_is_uncertain_and_not_retried() -> None:
    """Keep interruption exit code but disclose the possible post-send ambiguity."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        raise KeyboardInterrupt("provider may have accepted the request")

    code, receipt, text = _invoke(
        _body("one_shot_run", run_key=RUN_KEY),
        _executor_app(worker),
    )
    assert code == 130
    assert receipt == {
        "status": "uncertain",
        "code": "one_shot_outcome_uncertain",
        "run_key": RUN_KEY,
    }
    assert calls == [RUN_KEY]
    assert "provider may have accepted" not in text


def test_one_shot_run_requires_lowercase_sha256_key_and_exact_request_fields() -> None:
    """Reject alternate key spellings and forbidden worker or lease controls."""
    calls: list[str] = []

    def worker(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        return None

    app = _executor_app(worker)
    invalid_requests = [
        _body("one_shot_run", run_key=RUN_KEY.upper()),
        _body("one_shot_run", run_key="a" * 63),
        _body("one_shot_run", run_key=RUN_KEY, worker="caller-controlled"),
        _body("one_shot_run", run_key=RUN_KEY, lease_seconds=1),
    ]
    for body in invalid_requests:
        code, receipt, _ = _invoke(body, app)
        assert code == 2
        assert receipt == {"status": "rejected", "code": "invalid_request"}
    assert calls == []


def test_worker_role_cannot_run_one_shot() -> None:
    """Keep legacy broad workers outside the exact one-shot execution role."""
    app = ReviewedCLIApplication(
        runtime_plan=_role_plan("worker"),
        worker_once=lambda _worker, _ttl: None,
    )
    code, receipt, _ = _invoke(_body("one_shot_run", run_key=RUN_KEY), app)
    assert code == 78
    assert receipt == {"status": "rejected", "code": "role_not_enabled"}
