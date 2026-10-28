"""Build a fail-closed, non-executable preflight for one manual Fuyao query.

This module validates explicitly supplied configuration for one instrument. It
does not load dotenv, construct a ReviewedJob, promote a disabled job, enqueue a
run, create a worker or scheduler, reserve an attempt, or call Fuyao. It only
reads the source-attempt database when an inspector is explicitly injected,
and then only for an advisory snapshot. A successful result is preflight
evidence only; the transport gate remains the final authority for a real
request.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.request_budget import GrantDecision, RequestOperation
from opendata.data.source_attempt_gate import (
    SourceAttemptGate,
    SourceGrantInspectionRequest,
    SourceGrantInspectionResult,
    SourceGrantInspectionStatus,
)
from opendata.data.source_policy import (
    SourcePolicyConfigurationError,
    SourcePolicyDocument,
    SourceUsePolicy,
    build_source_policy_context,
    parse_source_policy_document,
)
from opendata.pipeline.fuyao_financial_bundle import (
    FuyaoFinancialBundleError,
    _read_atomic_regular_file,
    load_fuyao_financial_bundle,
)
from opendata.pipeline.fuyao_financial_run_job import build_fuyao_financial_manual_payload
from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding

_SOURCE = "ths"
_CANONICAL_MODEL = "financial_statement"
_FIXED_ERROR = "fuyao_manual_preflight_invalid"
_GateGrantStatus = Literal["not_checked", "matched", "rejected", "indeterminate"]


class FuyaoManualCompositionError(ValueError):
    """Fixed, content-free refusal for an invalid or disabled preflight."""

    def __init__(self, code: str = _FIXED_ERROR) -> None:
        """Create an error without including key, path, policy, or source details."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FuyaoManualQuery:
    """One bounded request description for a single explicit instrument."""

    instrument_id: str
    statement_type: Literal["income", "balance", "cashflow"]
    periodicity: Literal["annual", "quarterly"]
    idempotency_key: str = field(repr=False)
    start_date: str | None = None
    end_date: str | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class FuyaoManualCompositionConfig:
    """Opt-in inputs for preflight; inspection accepts only an injected gate."""

    preflight_enabled: bool = False
    deployment_binding: FuyaoProcessDeploymentBinding | None = field(
        default=None,
        repr=False,
    )
    query: FuyaoManualQuery | None = None
    source_attempt_gate: SourceAttemptGate | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class FuyaoManualPreflightReport:
    """Safe summary of validated inputs with execution permanently disabled."""

    status: Literal["disabled", "preflight_only"]
    dispatch_allowed: Literal[False] = False
    scheduler_started: Literal[False] = False
    worker_started: Literal[False] = False
    gate_grant_status: _GateGrantStatus = "not_checked"
    instrument_id: str | None = None
    statement_type: str | None = None
    periodicity: str | None = None
    source_policy_id: str | None = None
    bundle_sha256: str | None = None
    source_policy_sha256: str | None = None


def prepare_fuyao_manual_preflight(
    config: FuyaoManualCompositionConfig | None = None,
    *,
    now: datetime | None = None,
) -> FuyaoManualPreflightReport:
    """Validate a single manual Fuyao request without making it executable.

    The default configuration is disabled and returns before reading files or
    inspecting credentials. When explicitly enabled, the function validates
    the API key, exact bundle and policy pins, instrument identity, query
    bounds, and the operator-declared source policy. An explicitly injected
    gate may inspect a persisted grant snapshot for advisory status only.
    Matching never reserves an attempt or enables dispatch.

    Args:
        config: Deployment binding and one explicit query. Omission is a
            disabled preflight.
        now: Optional aware time used to reject an already expired policy.
            The current UTC time is used when omitted.

    Returns:
        A safe disabled or preflight-only report. Both states forbid dispatch,
        worker startup, and scheduler startup. An enabled preflight without an
        injected gate reports an indeterminate grant status; a disabled
        preflight performs no gate I/O.

    Raises:
        FuyaoManualCompositionError: If opt-in inputs, pins, key, bundle,
            policy, or query fail closed validation.
    """
    selected = config if config is not None else FuyaoManualCompositionConfig()
    if type(selected) is not FuyaoManualCompositionConfig:
        raise FuyaoManualCompositionError()
    if type(selected.preflight_enabled) is not bool:
        raise FuyaoManualCompositionError()
    if not selected.preflight_enabled:
        return FuyaoManualPreflightReport(status="disabled")

    binding = _validated_binding(selected.deployment_binding)
    query = selected.query
    if type(query) is not FuyaoManualQuery:
        raise FuyaoManualCompositionError()

    effective_now = _validated_now(now)
    _validate_key(binding)
    try:
        bundle = load_fuyao_financial_bundle(
            path=binding.financial_bundle_path,
            expected_sha256=binding.financial_bundle_sha256,
        )
        bundle_sha256 = bundle.bundle_sha256
        bundle_instrument_id = bundle.instrument_id
    except Exception:
        raise FuyaoManualCompositionError("bundle_pin_or_schema_invalid") from None
    if (
        type(bundle_sha256) is not str
        or not hmac.compare_digest(bundle_sha256, binding.financial_bundle_sha256)
        or bundle_instrument_id != query.instrument_id
    ):
        raise FuyaoManualCompositionError("bundle_pin_or_scope_invalid")

    document, policy = _load_policy(binding)
    _validate_policy(document, policy, effective_now)
    try:
        build_fuyao_financial_manual_payload(
            query.instrument_id,
            statement_type=query.statement_type,
            periodicity=query.periodicity,
            start_date=query.start_date,
            end_date=query.end_date,
            limit=query.limit,
            idempotency_key=query.idempotency_key,
        )
    except (TypeError, ValueError):
        raise FuyaoManualCompositionError("manual_query_invalid") from None

    return FuyaoManualPreflightReport(
        status="preflight_only",
        gate_grant_status=_inspect_grant_advisory(
            selected.source_attempt_gate,
            binding,
        ),
        instrument_id=bundle_instrument_id,
        statement_type=query.statement_type,
        periodicity=query.periodicity,
        source_policy_id=policy.policy_id,
        bundle_sha256=bundle_sha256,
        source_policy_sha256=binding.source_policy_sha256,
    )


def _inspect_grant_advisory(
    gate: SourceAttemptGate | None,
    binding: FuyaoProcessDeploymentBinding,
) -> _GateGrantStatus:
    """Inspect an explicitly injected grant and collapse details to an enum.

    The request uses identity values from the validated deployment binding and
    fixed Fuyao scope constants. The result is a human-review hint only;
    transport must reserve every physical request attempt independently.
    Exceptions and unknown return shapes collapse to one content-free value.
    """
    if gate is None or type(gate) is not SourceAttemptGate:
        return "indeterminate"
    try:
        if not _gate_matches_binding_path(gate, binding):
            return "indeterminate"
        host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
        if type(host) is not str or not host:
            return "indeterminate"
        request = SourceGrantInspectionRequest(
            grant_id=binding.source_attempt_grant_id,
            revision=binding.source_attempt_expected_revision,
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=RequestOperation.QUERY.value,
            principal_scope=binding.source_attempt_principal_scope,
            host=host,
        )
        result = gate.inspect_grant(request)
        if type(result) is not SourceGrantInspectionResult:
            return "indeterminate"
        if result.status is SourceGrantInspectionStatus.MATCHED:
            return "matched"
        if result.status is SourceGrantInspectionStatus.REJECTED:
            return "rejected"
        if result.status is SourceGrantInspectionStatus.INDETERMINATE:
            return "indeterminate"
    except Exception:
        return "indeterminate"
    return "indeterminate"


def _gate_matches_binding_path(
    gate: SourceAttemptGate,
    binding: FuyaoProcessDeploymentBinding,
) -> bool:
    """Require a canonical, non-symlink binding to the gate's pinned file."""
    try:
        configured_path = Path(binding.gate_database_path)
        current_path = Path(configured_path.anchor)
        for component in configured_path.parts[1:]:
            current_path /= component
            if current_path.is_symlink():
                return False
        resolved_path = configured_path.resolve(strict=True)
        return resolved_path == configured_path and resolved_path == gate._expected_main_path
    except Exception:
        return False


def _validated_binding(
    value: FuyaoProcessDeploymentBinding | None,
) -> FuyaoProcessDeploymentBinding:
    """Re-run exact deployment-binding validation without exposing its key."""
    if type(value) is not FuyaoProcessDeploymentBinding:
        raise FuyaoManualCompositionError("deployment_binding_invalid")
    try:
        return replace(value)
    except (TypeError, ValueError):
        raise FuyaoManualCompositionError("deployment_binding_invalid") from None


def _validate_key(binding: FuyaoProcessDeploymentBinding) -> None:
    """Require a usable credential without retaining or returning it."""
    try:
        credentials = FuyaoCredentials.from_settings(api_key=binding.api_key)
    except Exception:
        raise FuyaoManualCompositionError("credential_invalid") from None
    if credentials is None:
        raise FuyaoManualCompositionError("credential_missing")


def _load_policy(
    binding: FuyaoProcessDeploymentBinding,
) -> tuple[SourcePolicyDocument, SourceUsePolicy]:
    """Read, pin, and select exactly one Fuyao financial query policy."""
    try:
        body = _read_atomic_regular_file(binding.source_policy_path)
        digest = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(digest, binding.source_policy_sha256):
            raise FuyaoManualCompositionError("source_policy_pin_mismatch")
        document = parse_source_policy_document(body)
        matching = tuple(
            policy
            for policy in document.policies
            if policy.source == _SOURCE
            and policy.canonical_model == _CANONICAL_MODEL
            and policy.operation is RequestOperation.QUERY
        )
    except FuyaoManualCompositionError:
        raise
    except (FuyaoFinancialBundleError, SourcePolicyConfigurationError, OSError, TypeError):
        raise FuyaoManualCompositionError("source_policy_invalid") from None
    if len(matching) != 1:
        raise FuyaoManualCompositionError("source_policy_scope_invalid")
    return document, matching[0]


def _validate_policy(
    document: SourcePolicyDocument,
    policy: SourceUsePolicy,
    now: datetime,
) -> None:
    """Check the declared query policy, expiry, host, and model integrity."""
    try:
        if (
            type(document) is not SourcePolicyDocument
            or type(policy) is not SourceUsePolicy
            or policy.decision is not GrantDecision.ALLOWED
            or policy.conditions
            or policy.expires_at <= now
        ):
            raise FuyaoManualCompositionError("source_policy_not_usable")
        host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
        if host is None or host not in policy.allowed_hosts:
            raise FuyaoManualCompositionError("source_policy_host_invalid")
        build_source_policy_context(
            document,
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            principal_user_id=policy.principal_user_id,
        )
    except FuyaoManualCompositionError:
        raise
    except Exception:
        raise FuyaoManualCompositionError("source_policy_invalid") from None


def _validated_now(value: datetime | None) -> datetime:
    """Return one timezone-aware UTC instant for policy freshness checks."""
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) is not datetime:
        raise FuyaoManualCompositionError("preflight_time_invalid")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise FuyaoManualCompositionError("preflight_time_invalid")
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise FuyaoManualCompositionError("preflight_time_invalid") from None


__all__ = [
    "FuyaoManualCompositionConfig",
    "FuyaoManualCompositionError",
    "FuyaoManualPreflightReport",
    "FuyaoManualQuery",
    "prepare_fuyao_manual_preflight",
]
