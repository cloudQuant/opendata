"""Fail-closed publication jobs and offline request identities for THS indexes.

The default builders remain disabled and unreviewed. The legacy CSI 500
V1/V3 paths are kept separate for compatibility and offline boundary tests.
The explicit V4 sample builder requires an already-locked, target-identity
matched one-shot gate and carries its exact request scope into transport; no
production schedule or composition CLI attaches it. Source history boundaries
and supplier terms remain UNKNOWN or user-declared.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from alembic_source_gate.path_contract import inspect_existing_database_path
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
)
from opendata.data.models.market import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.endpoints import (
    INDEX_PRICES_ENDPOINT,
    MAX_WINDOW,
    build_index_prices_request,
)
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_DATASET_ID_BY_SYMBOL,
    INDEX_JOB_ID_BY_SYMBOL,
    INDEX_SCHEMA_VERSION,
    INDEX_SEMANTIC_VERSION,
    SSE50_INDEX_SYMBOL,
    build_index_daily_contract,
)
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V3,
    SOURCE_ATTEMPT_SCOPE_V4,
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import (
    OneShotScopeInspectionStatus,
    SourceAttemptGate,
    SourceAttemptScopeV3,
    TransportAttempt,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.run_identity import (
    RunIdentity,
    RunRequest,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from pydantic import JsonValue
    from sqlalchemy.engine import Connection

SSE50_INDEX_JOB_ID = INDEX_JOB_ID_BY_SYMBOL[SSE50_INDEX_SYMBOL]
SSE50_INDEX_JOB_VERSION = "publication-v1"
_PROVIDER = "ths"
_MODEL = "index_daily"
_FETCH_TIMEOUT_SECONDS = 30
_CSI500_SYMBOL = "000905.SH"
_CSI500_DATASET_ID = INDEX_DATASET_ID_BY_SYMBOL[_CSI500_SYMBOL]
# The provider receives an inclusive caller range and converts its end to a
# half-open endpoint range. Its inclusive day count equals that resulting span.
_MAX_WINDOW_CALENDAR_DAYS = MAX_WINDOW.days
_MAX_TRANSPORT_ATTEMPTS = 1
_CSI500_V3_REQUEST_FIELDS = ("end", "interval", "start", "thscode")
_CSI500_V3_EXPECTED_HTTPS_HOST = "fuyao.aicubes.cn"
_CSI500_V3_EXPECTED_PATH = INDEX_PRICES_ENDPOINT
_CSI500_V3_EXPECTED_METHOD = "GET"
_SOURCE_RIGHTS_EVIDENCE = "docs/data-rights-registry.md#0.1"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HISTORY_BOUNDARY_UNKNOWN: Final[Literal["UNKNOWN"]] = "UNKNOWN"
_ALLOWED_INTERNAL_PURPOSES = (
    "automatic_collection",
    "storage_in_opendata",
    "internal_opendata_to_afi_api",
)
_QUALITY = {"status": "unknown", "report_id": None, "issue_count": 0}

__all__ = [
    "SSE50_INDEX_JOB_ID",
    "SSE50_INDEX_JOB_VERSION",
    "Csi500FuyaoSourceAccess",
    "Csi500FuyaoV3SampleAccess",
    "Csi500FuyaoV4SampleAccess",
    "Csi500FuyaoV4SampleSpec",
    "build_authorized_csi500_index_daily_job",
    "build_csi500_manual_backfill_payload",
    "build_csi500_v3_sample_index_daily_job",
    "build_csi500_v4_sample_index_daily_job",
    "build_csi500_v3_sample_payload",
    "build_csi500_v4_sample_spec",
    "build_csi500_v4_sample_task_definition",
    "build_csi500_v4_sample_payload",
    "build_index_daily_backfill_request",
    "build_index_daily_job",
    "build_sse50_index_daily_backfill_request",
    "build_sse50_index_daily_job",
]


class ThsIndexSourceAuthorizationUnknownError(PermissionError):
    """Raised while THS rights for production acquisition remain unreviewed."""

    def __init__(self) -> None:
        """Use a fixed error message that cannot disclose provider credentials."""
        super().__init__("THS index source authorization is UNKNOWN")


@dataclass(frozen=True, slots=True, repr=False)
class Csi500FuyaoSourceAccess:
    """Narrow CSI 500 binding to the user's declared Fuyao internal use.

    The rights basis is ``USER_DECLARED`` and is not independent supplier
    verification. The gate and binding must refer to an existing persistent
    THS ``index_daily`` QUERY grant for the Fuyao host; each physical send is
    still reserved by that gate. Its exact expected evidence reference is
    carried into the transport attempt and compared with the persisted grant
    in the reservation transaction. This value grants no external
    redistribution or completeness claim.
    """

    source_attempt_gate: SourceAttemptGate = field(repr=False, compare=False)
    source_attempt_binding: SourceAttemptBinding = field(repr=False, compare=False)
    operator_confirms_user_declared_internal_use: bool

    def __post_init__(self) -> None:
        """Require explicit confirmation and a trusted, unbound run identity."""
        if type(self.source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be an exact SourceAttemptGate")
        if type(self.source_attempt_binding) is not SourceAttemptBinding:
            raise TypeError("source_attempt_binding must be an exact SourceAttemptBinding")
        if self.source_attempt_binding.run_key is not None:
            raise ValueError("source_attempt_binding must leave run_key unset")
        if type(self.operator_confirms_user_declared_internal_use) is not bool:
            raise TypeError("operator confirmation must be an explicit boolean")
        if not self.operator_confirms_user_declared_internal_use:
            raise ValueError("Fuyao CSI 500 internal-use authorization was not confirmed")

    @property
    def evidence_reference(self) -> str:
        """Return the registry reference for the user's documented declaration."""
        return _SOURCE_RIGHTS_EVIDENCE

    @property
    def authorization_basis(self) -> Literal["USER_DECLARED"]:
        """Identify this as a user declaration, not verified supplier terms."""
        return "USER_DECLARED"

    @property
    def symbol(self) -> str:
        """Return the one index symbol bound by this access record."""
        return _CSI500_SYMBOL

    @property
    def dataset_id(self) -> str:
        """Return the one dataset bound by this access record."""
        return _CSI500_DATASET_ID

    @property
    def endpoint_path(self) -> str:
        """Return the one Fuyao endpoint bound by this access record."""
        return INDEX_PRICES_ENDPOINT

    @property
    def operation(self) -> Literal["query"]:
        """Return the only permitted source transport operation."""
        return "query"

    @property
    def purposes(self) -> tuple[str, ...]:
        """Return the three internal uses already declared by the user."""
        return _ALLOWED_INTERNAL_PURPOSES

    @property
    def maximum_window_calendar_days(self) -> int:
        """Return the endpoint's per-request technical ceiling, not a rights quota."""
        return _MAX_WINDOW_CALENDAR_DAYS

    @property
    def source_history_boundary(self) -> Literal["UNKNOWN"]:
        """Keep the earliest source-available date explicitly unknown."""
        return _HISTORY_BOUNDARY_UNKNOWN

    @property
    def external_distribution_allowed(self) -> Literal[False]:
        """The user's declaration does not authorize external redistribution."""
        return False

    def binding_for_run(self, run_key: str) -> SourceAttemptBinding:
        """Attach the trusted grant identity to this exact one-shot run."""
        if type(run_key) is not str or not run_key.strip():
            raise ValueError("run_key must be a non-empty string")
        return SourceAttemptBinding(
            grant_id=self.source_attempt_binding.grant_id,
            expected_revision=self.source_attempt_binding.expected_revision,
            principal_scope=self.source_attempt_binding.principal_scope,
            run_key=run_key,
            expected_rights_evidence_reference=self.evidence_reference,
        )

    def __repr__(self) -> str:
        """Keep grant identifiers out of routine logs and tracebacks."""
        return "Csi500FuyaoSourceAccess(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Csi500FuyaoV3SampleAccess:
    """Explicit opt-in V3 pins for one fixed CSI 500 sample date.

    This type is separate from :class:`Csi500FuyaoSourceAccess`, preserving
    the accepted V1 #129 call path. The sample composition must only populate
    budget fields from an exact target-bound quota receipt. It does not itself
    attest supplier rights or prove a persistent lifetime attempt cap.
    """

    source_attempt_gate: SourceAttemptGate = field(repr=False, compare=False)
    source_attempt_binding: SourceAttemptBinding = field(repr=False, compare=False)
    business_date: date
    capture_intent_id: str
    source_policy_id: str
    source_policy_revision: str
    source_policy_sha256: str
    budget_binding_sha256: str
    budget_quota_key: str
    budget_policy_revision: int
    budget_policy_sha256: str
    target_identity_sha256: str
    operator_confirms_user_declared_internal_use: bool

    def __post_init__(self) -> None:
        """Validate the exact one-date sample and all immutable budget pins."""
        if type(self.source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be an exact SourceAttemptGate")
        if type(self.source_attempt_binding) is not SourceAttemptBinding:
            raise TypeError("source_attempt_binding must be an exact SourceAttemptBinding")
        if self.source_attempt_binding.run_key is not None:
            raise ValueError("source_attempt_binding must leave run_key unset")
        self.source_attempt_binding.validate_integrity()
        if type(self.business_date) is not date:
            raise TypeError("business_date must be an exact date")
        if (
            type(self.operator_confirms_user_declared_internal_use) is not bool
            or not self.operator_confirms_user_declared_internal_use
        ):
            raise ValueError("Fuyao CSI 500 internal-use authorization was not confirmed")
        if type(self.budget_policy_revision) is not int or self.budget_policy_revision < 1:
            raise ValueError("budget_policy_revision must be positive")
        for field_name in (
            "source_policy_sha256",
            "budget_binding_sha256",
            "budget_policy_sha256",
            "target_identity_sha256",
        ):
            if (
                type(getattr(self, field_name)) is not str
                or _SHA256_RE.fullmatch(getattr(self, field_name)) is None
            ):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        self.pin_for_request(self.business_date, self.business_date)

    @property
    def evidence_reference(self) -> str:
        """Return the one documented user-declared rights evidence reference."""
        return _SOURCE_RIGHTS_EVIDENCE

    @property
    def authorization_basis(self) -> Literal["USER_DECLARED"]:
        """Identify this as a user declaration, not independently verified rights."""
        return "USER_DECLARED"

    @property
    def symbol(self) -> str:
        """Return the one benchmark fixed by this access record."""
        return _CSI500_SYMBOL

    @property
    def dataset_id(self) -> str:
        """Return the one dataset fixed by this access record."""
        return _CSI500_DATASET_ID

    @property
    def endpoint_path(self) -> str:
        """Return the exact index-daily endpoint pinned by this sample path."""
        return _CSI500_V3_EXPECTED_PATH

    @property
    def operation(self) -> Literal["query"]:
        """Return the only permitted operation."""
        return "query"

    @property
    def purposes(self) -> tuple[str, ...]:
        """Return the internal-use purposes declared by the user."""
        return _ALLOWED_INTERNAL_PURPOSES

    @property
    def maximum_window_calendar_days(self) -> int:
        """Return the request window this opt-in sample actually permits."""
        return 1

    @property
    def source_history_boundary(self) -> Literal["UNKNOWN"]:
        """Keep the source's earliest available date unknown."""
        return _HISTORY_BOUNDARY_UNKNOWN

    @property
    def external_distribution_allowed(self) -> Literal[False]:
        """The user's declaration does not authorize external redistribution."""
        return False

    def binding_for_run(self, run_key: str) -> SourceAttemptBinding:
        """Attach the durable grant and exact rights reference to one run."""
        if type(run_key) is not str or not run_key.strip():
            raise ValueError("run_key must be a non-empty string")
        return SourceAttemptBinding(
            grant_id=self.source_attempt_binding.grant_id,
            expected_revision=self.source_attempt_binding.expected_revision,
            principal_scope=self.source_attempt_binding.principal_scope,
            run_key=run_key,
            expected_rights_evidence_reference=self.evidence_reference,
        )

    def pin_for_request(self, start_date: date, end_date: date) -> SourceAttemptScopeV3Pin:
        """Build V3 pins for the single exact source query accepted by this sample."""
        if type(start_date) is not date or type(end_date) is not date:
            raise TypeError("sample request dates must be exact dates")
        if start_date != self.business_date or end_date != self.business_date:
            raise ValueError("V3 sample access is bound to one exact business date")
        endpoint_end = date.fromordinal(end_date.toordinal() + 1)
        query_parameters = build_index_prices_request(
            symbol=self.symbol,
            start=start_date,
            end=endpoint_end,
        )
        wire_parameters = {key: str(value) for key, value in query_parameters.items()}
        summary = request_summary_sha256(
            wire_parameters,
            allowed_fields=_CSI500_V3_REQUEST_FIELDS,
        )
        return SourceAttemptScopeV3Pin(
            capture_intent_id=self.capture_intent_id,
            request_summary_sha256=summary,
            request_summary_allowed_fields=_CSI500_V3_REQUEST_FIELDS,
            source_policy_id=self.source_policy_id,
            source_policy_revision=self.source_policy_revision,
            source_policy_sha256=self.source_policy_sha256,
            budget_binding_sha256=self.budget_binding_sha256,
            budget_quota_key=self.budget_quota_key,
            budget_policy_revision=self.budget_policy_revision,
            budget_policy_sha256=self.budget_policy_sha256,
            expected_https_host=_CSI500_V3_EXPECTED_HTTPS_HOST,
            expected_path=_CSI500_V3_EXPECTED_PATH,
            expected_method=_CSI500_V3_EXPECTED_METHOD,
        )

    def __repr__(self) -> str:
        """Keep grant, policy, quota and target identities out of repr output."""
        return "Csi500FuyaoV3SampleAccess(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Csi500FuyaoV4SampleSpec:
    """Offline candidate for one complete, stable V4 one-shot request scope.

    ``source_attempt_scope`` is only a candidate until the separate gate lock
    transaction confirms it. This spec cannot reserve, send, publish, or prove
    supplier rights. Source-policy fields are caller-declared pins derived
    from this fixed code path and remain ``USER_DECLARED_UNVERIFIED``.
    """

    business_date: date
    capture_intent_id: str
    intent_sha256: str
    task_definition: TaskDefinition
    request: RunRequest
    run_identity: RunIdentity
    source_attempt_binding: SourceAttemptBinding = field(repr=False)
    source_attempt_scope_v3_pin: SourceAttemptScopeV3Pin = field(repr=False)
    source_attempt_scope: SourceAttemptScopeV3 = field(repr=False)
    target_id: str
    target_identity_sha256: str
    calendar_manifest_sha256: str
    calendar_artifact_sha256: str
    authorization_actor: str
    grant_expires_at_utc: str
    rights_evidence_sha256: str
    source_policy_id: str
    source_policy_revision: str
    source_policy_sha256: str
    budget_binding_sha256: str
    budget_quota_key: str
    budget_policy_revision: int
    budget_policy_sha256: str

    def __post_init__(self) -> None:
        """Recheck that the immutable request, pin and gate scope agree."""
        if type(self.business_date) is not date:
            raise TypeError("business_date must be an exact date")
        if type(self.target_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", self.target_id):
            raise ValueError("target_id must be a lowercase UUID hex string")
        if (
            type(self.authorization_actor) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}", self.authorization_actor)
            is None
        ):
            raise ValueError("authorization_actor must be a bounded identifier")
        if (
            type(self.grant_expires_at_utc) is not str
            or len(self.grant_expires_at_utc) > 40
            or not self.grant_expires_at_utc.endswith("Z")
        ):
            raise ValueError("grant_expires_at_utc must be a canonical UTC timestamp")
        try:
            parsed_expiry = datetime.fromisoformat(self.grant_expires_at_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("grant_expires_at_utc must be a canonical UTC timestamp") from exc
        if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() != timedelta(0):
            raise ValueError("grant_expires_at_utc must be a canonical UTC timestamp")
        if type(self.task_definition) is not TaskDefinition:
            raise TypeError("task_definition must be a TaskDefinition")
        if type(self.request) is not RunRequest or type(self.run_identity) is not RunIdentity:
            raise TypeError("request and run_identity must be validated pipeline identities")
        if build_run_identity(self.task_definition, self.request) != self.run_identity:
            raise ValueError("V4 sample run identity does not match its task and request")
        if self.task_definition != build_csi500_v4_sample_task_definition(
            capture_intent_id=self.capture_intent_id
        ):
            raise ValueError("V4 sample task definition differs from its capture intent")
        if type(self.source_attempt_binding) is not SourceAttemptBinding:
            raise TypeError("source_attempt_binding must be a SourceAttemptBinding")
        if type(self.source_attempt_scope_v3_pin) is not SourceAttemptScopeV3Pin:
            raise TypeError("source_attempt_scope_v3_pin must be a SourceAttemptScopeV3Pin")
        if type(self.source_attempt_scope) is not SourceAttemptScopeV3:
            raise TypeError("source_attempt_scope must be a SourceAttemptScopeV3")
        if (
            self.request.run_type != "manual"
            or self.request.query
            != {
                "start_date": self.business_date.isoformat(),
                "end_date": self.business_date.isoformat(),
            }
            or self.request.partition != {"symbol": _CSI500_SYMBOL}
        ):
            raise ValueError("V4 sample request must target one exact CSI 500 date")
        if self.request.idempotency_key != self.capture_intent_id:
            raise ValueError("V4 sample request key must equal its capture intent")
        if self.run_identity.idempotency_key != self.source_attempt_scope.attempt.run_key:
            raise ValueError("V4 sample run identity differs from its gate scope")
        attempt = self.source_attempt_scope.attempt
        expected_request = _domain_sha256(
            "opendata.csi500.transport-request.v1",
            {"capture_intent_id": self.capture_intent_id, "run_key": self.run_key},
        )
        expected_attempt = _domain_sha256(
            "opendata.csi500.transport-attempt.v1",
            {"capture_intent_id": self.capture_intent_id, "run_key": self.run_key},
        )
        query_parameters = build_index_prices_request(
            symbol=_CSI500_SYMBOL,
            start=self.business_date,
            end=date.fromordinal(self.business_date.toordinal() + 1),
        )
        expected_request_summary = request_summary_sha256(
            {key: str(value) for key, value in query_parameters.items()},
            allowed_fields=_CSI500_V3_REQUEST_FIELDS,
        )
        self.source_attempt_binding.validate_integrity()
        self.source_attempt_scope_v3_pin.validate_integrity()
        if (
            attempt.attempt_no != 1
            or attempt.attempt_id != expected_attempt
            or attempt.request_id != expected_request
            or attempt.source != _PROVIDER
            or attempt.canonical_model != _MODEL
            or attempt.operation != RequestOperation.QUERY.value
            or attempt.grant_id != self.source_attempt_binding.grant_id
            or attempt.expected_revision != self.source_attempt_binding.expected_revision
            or attempt.principal_scope != self.source_attempt_binding.principal_scope
            or attempt.run_key != self.source_attempt_binding.run_key
            or attempt.host != _CSI500_V3_EXPECTED_HTTPS_HOST
            or attempt.expected_rights_evidence_reference != _SOURCE_RIGHTS_EVIDENCE
            or self.source_attempt_binding.run_key != self.run_identity.idempotency_key
            or self.source_attempt_binding.expected_rights_evidence_reference
            != _SOURCE_RIGHTS_EVIDENCE
            or self.source_attempt_scope.request_scheme != "https"
            or self.source_attempt_scope.request_method != _CSI500_V3_EXPECTED_METHOD
            or self.source_attempt_scope.request_host != _CSI500_V3_EXPECTED_HTTPS_HOST
            or self.source_attempt_scope.request_path != _CSI500_V3_EXPECTED_PATH
            or self.source_attempt_scope.capture_intent_id != self.capture_intent_id
            or self.source_attempt_scope.request_summary_sha256 != expected_request_summary
            or self.source_attempt_scope_v3_pin.capture_intent_id != self.capture_intent_id
            or self.source_attempt_scope_v3_pin.request_summary_sha256
            != self.source_attempt_scope.request_summary_sha256
            or self.source_attempt_scope_v3_pin.expected_https_host
            != _CSI500_V3_EXPECTED_HTTPS_HOST
            or self.source_attempt_scope_v3_pin.expected_path != _CSI500_V3_EXPECTED_PATH
            or self.source_attempt_scope_v3_pin.expected_method != _CSI500_V3_EXPECTED_METHOD
            or self.source_attempt_scope_v3_pin.request_summary_allowed_fields
            != _CSI500_V3_REQUEST_FIELDS
            or self.source_attempt_scope_v3_pin.source_policy_id
            != self.source_attempt_scope.source_policy_id
            or self.source_attempt_scope_v3_pin.source_policy_revision
            != self.source_attempt_scope.source_policy_revision
            or self.source_attempt_scope_v3_pin.source_policy_sha256
            != self.source_attempt_scope.source_policy_sha256
            or self.source_attempt_scope_v3_pin.budget_binding_sha256
            != self.source_attempt_scope.budget_binding_sha256
            or self.source_attempt_scope_v3_pin.budget_quota_key
            != self.source_attempt_scope.budget_quota_key
            or self.source_attempt_scope_v3_pin.budget_policy_revision
            != self.source_attempt_scope.budget_policy_revision
            or self.source_attempt_scope_v3_pin.budget_policy_sha256
            != self.source_attempt_scope.budget_policy_sha256
        ):
            raise ValueError("V4 sample request pins do not match its gate scope")
        for field_name in (
            "intent_sha256",
            "target_identity_sha256",
            "calendar_manifest_sha256",
            "calendar_artifact_sha256",
            "rights_evidence_sha256",
            "source_policy_sha256",
            "budget_binding_sha256",
            "budget_policy_sha256",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        expected_policy_sha256 = _csi500_v4_source_policy_sha256()
        if (
            self.source_policy_id != "csi500-fuyao-user-declared-internal-v1"
            or self.source_policy_revision != "1"
            or self.source_policy_sha256 != expected_policy_sha256
        ):
            raise ValueError("V4 sample source policy differs from its fixed code declaration")
        if (
            self.source_policy_id != self.source_attempt_scope.source_policy_id
            or self.source_policy_revision != self.source_attempt_scope.source_policy_revision
            or self.source_policy_sha256 != self.source_attempt_scope.source_policy_sha256
            or self.budget_binding_sha256 != self.source_attempt_scope.budget_binding_sha256
            or self.budget_quota_key != self.source_attempt_scope.budget_quota_key
            or self.budget_policy_revision != self.source_attempt_scope.budget_policy_revision
            or self.budget_policy_sha256 != self.source_attempt_scope.budget_policy_sha256
        ):
            raise ValueError("V4 sample spec values do not match its gate scope")
        context = {
            "business_date": self.business_date.isoformat(),
            "budget_binding_sha256": self.budget_binding_sha256,
            "budget_policy_revision": self.budget_policy_revision,
            "budget_policy_sha256": self.budget_policy_sha256,
            "budget_quota_key": self.budget_quota_key,
            "calendar_artifact_sha256": self.calendar_artifact_sha256,
            "calendar_manifest_sha256": self.calendar_manifest_sha256,
            "authorization_actor": self.authorization_actor,
            "grant_expires_at_utc": self.grant_expires_at_utc,
            "grant_id": attempt.grant_id,
            "grant_revision": attempt.expected_revision,
            "principal_scope": attempt.principal_scope,
            "request_summary_sha256": self.source_attempt_scope.request_summary_sha256,
            "rights_evidence_reference": _SOURCE_RIGHTS_EVIDENCE,
            "rights_evidence_sha256": self.rights_evidence_sha256,
            "source_policy_id": self.source_policy_id,
            "source_policy_revision": self.source_policy_revision,
            "source_policy_sha256": self.source_policy_sha256,
            "target_id": self.target_id,
            "target_identity_sha256": self.target_identity_sha256,
        }
        if self.capture_intent_id != _domain_sha256("opendata.csi500.capture-intent.v1", context):
            raise ValueError("V4 sample capture intent does not match its static context")
        expected_intent_sha256 = _domain_sha256(
            "opendata.csi500.one-shot-intent.v1",
            {
                **context,
                "attempt_id": attempt.attempt_id,
                "capture_intent_id": self.capture_intent_id,
                "job_version": self.task_definition.version,
                "request_id": attempt.request_id,
                "run_key": self.run_key,
                "run_payload_hash": self.run_identity.payload_hash,
            },
        )
        if self.intent_sha256 != expected_intent_sha256:
            raise ValueError("V4 sample intent digest does not match its full identity")

    @property
    def run_key(self) -> str:
        """Return the deterministic durable run key already bound into the scope."""
        return self.run_identity.idempotency_key

    @property
    def attempt_id(self) -> str:
        """Return the precomputed transport attempt identifier."""
        return self.source_attempt_scope.attempt.attempt_id

    @property
    def request_id(self) -> str:
        """Return the precomputed transport request identifier."""
        return self.source_attempt_scope.attempt.request_id

    def __repr__(self) -> str:
        """Keep request, grant, quota, and target identifiers out of repr output."""
        return "Csi500FuyaoV4SampleSpec(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Csi500FuyaoV4SampleAccess:
    """Bind one V4 CSI sample job to its already-locked isolated SQLite gate.

    Construction checks the current local target identity and performs a
    read-only inspection that must report the exact scope as UNUSED. That
    inspection is not a send permit: transport still has to atomically reserve
    the exact V4 scope and receive RESERVED_NEW immediately before its one
    HTTP send. The gate constrains the reserved request identity; it does not
    sandbox HTTPX transports or arbitrary callbacks, and a MockTransport type
    alone is not proof that no network side effects are possible. A gate lock
    and the user's declaration do not independently verify supplier rights.
    """

    source_attempt_gate: SourceAttemptGate = field(repr=False, compare=False)
    sample_spec: Csi500FuyaoV4SampleSpec = field(repr=False, compare=False)
    operator_confirms_user_declared_internal_use: bool

    def __post_init__(self) -> None:
        """Require the exact pre-locked scope and its original target file."""
        if type(self.source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be an exact SourceAttemptGate")
        if type(self.sample_spec) is not Csi500FuyaoV4SampleSpec:
            raise TypeError("sample_spec must be an exact Csi500FuyaoV4SampleSpec")
        if type(self.operator_confirms_user_declared_internal_use) is not bool:
            raise TypeError("operator confirmation must be an explicit boolean")
        if not self.operator_confirms_user_declared_internal_use:
            raise ValueError("Fuyao CSI 500 internal-use declaration was not confirmed")

        try:
            engine = object.__getattribute__(self.source_attempt_gate, "_engine")
            database_path = engine.url.database
            if type(database_path) is not str or not database_path:
                raise ValueError
            _resolved_path, identity = inspect_existing_database_path(database_path)
            payload = json.dumps(
                {"device": identity.device, "inode": identity.inode},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            identity_sha256 = hashlib.sha256(payload).hexdigest()
        except Exception:
            raise ValueError("V4 sample gate target identity is unavailable") from None
        if identity_sha256 != self.sample_spec.target_identity_sha256:
            raise ValueError("V4 sample gate does not match its reviewed target identity")

        inspection = self.source_attempt_gate.inspect_one_shot_scope(
            self.sample_spec.source_attempt_scope
        )
        if (
            type(inspection.status) is not OneShotScopeInspectionStatus
            or inspection.status is not OneShotScopeInspectionStatus.UNUSED
        ):
            raise ValueError("V4 sample gate does not contain this unused locked scope")

    @property
    def evidence_reference(self) -> str:
        """Return the exact user-declared evidence reference pinned in the scope."""
        return self.sample_spec.source_attempt_binding.expected_rights_evidence_reference or ""

    @property
    def authorization_basis(self) -> Literal["USER_DECLARED"]:
        """Identify the authorization basis as a user declaration only."""
        return "USER_DECLARED"

    @property
    def symbol(self) -> str:
        """Return the one index symbol fixed by the immutable V4 spec."""
        return _CSI500_SYMBOL

    @property
    def dataset_id(self) -> str:
        """Return the one dataset fixed by the immutable V4 spec."""
        return _CSI500_DATASET_ID

    @property
    def endpoint_path(self) -> str:
        """Return the exact historical index endpoint bound by the V4 pins."""
        return _CSI500_V3_EXPECTED_PATH

    @property
    def operation(self) -> Literal["query"]:
        """Return the only operation allowed by the immutable sample scope."""
        return "query"

    @property
    def purposes(self) -> tuple[str, ...]:
        """Return the internal-use purposes declared for this sample."""
        return _ALLOWED_INTERNAL_PURPOSES

    @property
    def maximum_window_calendar_days(self) -> int:
        """Return the sample's one-business-date request bound."""
        return 1

    @property
    def source_history_boundary(self) -> Literal["UNKNOWN"]:
        """Keep the earliest source-available date explicitly unknown."""
        return _HISTORY_BOUNDARY_UNKNOWN

    @property
    def external_distribution_allowed(self) -> Literal[False]:
        """The user's declaration does not authorize external redistribution."""
        return False

    def __repr__(self) -> str:
        """Keep gate, intent, grant, and target identifiers out of repr output."""
        return "Csi500FuyaoV4SampleAccess(<redacted>)"


def build_sse50_index_daily_backfill_request(
    *,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> RunRequest:
    """Build the first-series SSE 50 backfill request."""
    return build_index_daily_backfill_request(
        symbol=SSE50_INDEX_SYMBOL,
        start_date=start_date,
        end_date=end_date,
        idempotency_key=idempotency_key,
    )


def build_index_daily_backfill_request(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> RunRequest:
    """Build an inclusive backfill request for exactly one whitelisted index.

    Args:
        symbol: Exact supported symbol including its market suffix.
        start_date: First requested trading date, inclusive.
        end_date: Last requested trading date, inclusive.
        idempotency_key: Caller-supplied stable identifier for this backfill.

    Returns:
        A validated request pinned to the requested single-index dataset.
    """
    _validate_symbol(symbol)
    _validate_date_order(start_date, end_date)
    return RunRequest(
        run_type="backfill",
        query={},
        partition={"symbol": symbol},
        idempotency_key=idempotency_key,
        backfill_start=start_date,
        backfill_end=end_date,
    )


def build_csi500_manual_backfill_payload(
    *,
    source_access: Csi500FuyaoSourceAccess,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> dict[str, JsonValue]:
    """Build one dispatcher payload for a CSI 500 one-shot historical window.

    The inclusive window is capped so the provider's half-open conversion fits
    within one THS endpoint chunk. The cap is not evidence of the source's
    earliest available date, a retention promise, or a cross-execution quota.
    """
    _require_csi500_source_access(source_access)
    _validate_window(start_date, end_date)
    request = RunRequest(
        run_type="manual",
        query={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        partition={"symbol": _CSI500_SYMBOL},
        idempotency_key=idempotency_key,
    )
    return {
        "query": request.query,
        "partition": request.partition,
        "shard": request.shard,
        "idempotency_key": request.idempotency_key,
    }


def build_csi500_v3_sample_payload(
    *,
    source_access: Csi500FuyaoV3SampleAccess,
    idempotency_key: str,
) -> dict[str, JsonValue]:
    """Build the V3 path's exact one-business-date manual request."""
    _require_csi500_v3_sample_access(source_access)
    request = RunRequest(
        run_type="manual",
        query={
            "start_date": source_access.business_date.isoformat(),
            "end_date": source_access.business_date.isoformat(),
        },
        partition={"symbol": _CSI500_SYMBOL},
        idempotency_key=idempotency_key,
    )
    return {
        "query": request.query,
        "partition": request.partition,
        "shard": request.shard,
        "idempotency_key": request.idempotency_key,
    }


def build_csi500_v4_sample_spec(
    *,
    business_date: date,
    target_id: str,
    target_identity_sha256: str,
    calendar_manifest_sha256: str,
    calendar_artifact_sha256: str,
    authorization_actor: str,
    grant_id: str,
    grant_revision: int,
    principal_scope: str,
    grant_expires_at_utc: str,
    rights_evidence_sha256: str,
    budget_binding_sha256: str,
    budget_quota_key: str,
    budget_policy_revision: int,
    budget_policy_sha256: str,
) -> Csi500FuyaoV4SampleSpec:
    """Build a deterministic, offline V4 scope candidate for one sample date.

    Every ID is derived from the exact target, calendar, grant, and quota
    metadata. No database, environment, provider, or network access occurs.
    The returned gate scope is a candidate only; a separate reviewed operation
    must persist its one-shot lock before any V4 transport can consume it.
    """
    if type(business_date) is not date:
        raise TypeError("business_date must be an exact date")
    if type(target_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", target_id):
        raise ValueError("target_id must be a lowercase UUID hex string")
    if type(grant_revision) is not int or grant_revision < 1:
        raise ValueError("grant_revision must be a positive integer")
    if type(budget_policy_revision) is not int or budget_policy_revision < 1:
        raise ValueError("budget_policy_revision must be a positive integer")
    for name, value in (
        ("target_identity_sha256", target_identity_sha256),
        ("calendar_manifest_sha256", calendar_manifest_sha256),
        ("calendar_artifact_sha256", calendar_artifact_sha256),
        ("rights_evidence_sha256", rights_evidence_sha256),
        ("budget_binding_sha256", budget_binding_sha256),
        ("budget_policy_sha256", budget_policy_sha256),
    ):
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    if type(grant_expires_at_utc) is not str or not grant_expires_at_utc.strip():
        raise ValueError("grant_expires_at_utc must be a non-empty string")
    if len(grant_expires_at_utc) > 40:
        raise ValueError("grant_expires_at_utc is too long")
    try:
        expires_at = datetime.fromisoformat(grant_expires_at_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("grant_expires_at_utc must be an ISO timestamp") from exc
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("grant_expires_at_utc must be timezone-aware")
    grant_expires_at_utc = (
        expires_at.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if budget_quota_key != "csi500-fuyao-sample-204":
        raise ValueError("budget_quota_key does not match the fixed sample quota")

    query_parameters = build_index_prices_request(
        symbol=_CSI500_SYMBOL,
        start=business_date,
        end=date.fromordinal(business_date.toordinal() + 1),
    )
    wire_parameters = {key: str(value) for key, value in query_parameters.items()}
    request_summary = request_summary_sha256(
        wire_parameters,
        allowed_fields=_CSI500_V3_REQUEST_FIELDS,
    )
    source_policy_id = "csi500-fuyao-user-declared-internal-v1"
    source_policy_revision = "1"
    source_policy_sha256 = _csi500_v4_source_policy_sha256()
    intent_context = {
        "business_date": business_date.isoformat(),
        "budget_binding_sha256": budget_binding_sha256,
        "budget_policy_revision": budget_policy_revision,
        "budget_policy_sha256": budget_policy_sha256,
        "budget_quota_key": budget_quota_key,
        "calendar_artifact_sha256": calendar_artifact_sha256,
        "calendar_manifest_sha256": calendar_manifest_sha256,
        "authorization_actor": authorization_actor,
        "grant_expires_at_utc": grant_expires_at_utc,
        "grant_id": grant_id,
        "grant_revision": grant_revision,
        "principal_scope": principal_scope,
        "request_summary_sha256": request_summary,
        "rights_evidence_reference": _SOURCE_RIGHTS_EVIDENCE,
        "rights_evidence_sha256": rights_evidence_sha256,
        "source_policy_id": source_policy_id,
        "source_policy_revision": source_policy_revision,
        "source_policy_sha256": source_policy_sha256,
        "target_id": target_id,
        "target_identity_sha256": target_identity_sha256,
    }
    capture_intent_id = _domain_sha256("opendata.csi500.capture-intent.v1", intent_context)
    task = build_csi500_v4_sample_task_definition(capture_intent_id=capture_intent_id)
    request = RunRequest(
        run_type="manual",
        query={
            "start_date": business_date.isoformat(),
            "end_date": business_date.isoformat(),
        },
        partition={"symbol": _CSI500_SYMBOL},
        idempotency_key=capture_intent_id,
    )
    run_identity = build_run_identity(task, request)
    binding = SourceAttemptBinding(
        grant_id=grant_id,
        expected_revision=grant_revision,
        principal_scope=principal_scope,
        run_key=run_identity.idempotency_key,
        expected_rights_evidence_reference=_SOURCE_RIGHTS_EVIDENCE,
    )
    attempt = TransportAttempt(
        attempt_id=_domain_sha256(
            "opendata.csi500.transport-attempt.v1",
            {"capture_intent_id": capture_intent_id, "run_key": run_identity.idempotency_key},
        ),
        request_id=_domain_sha256(
            "opendata.csi500.transport-request.v1",
            {"capture_intent_id": capture_intent_id, "run_key": run_identity.idempotency_key},
        ),
        attempt_no=1,
        grant_id=grant_id,
        expected_revision=grant_revision,
        source=_PROVIDER,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY.value,
        principal_scope=principal_scope,
        host=_CSI500_V3_EXPECTED_HTTPS_HOST,
        run_key=run_identity.idempotency_key,
        expected_rights_evidence_reference=_SOURCE_RIGHTS_EVIDENCE,
    )
    pin = SourceAttemptScopeV3Pin(
        capture_intent_id=capture_intent_id,
        request_summary_sha256=request_summary,
        request_summary_allowed_fields=_CSI500_V3_REQUEST_FIELDS,
        expected_https_host=_CSI500_V3_EXPECTED_HTTPS_HOST,
        expected_path=_CSI500_V3_EXPECTED_PATH,
        expected_method=_CSI500_V3_EXPECTED_METHOD,
        source_policy_id=source_policy_id,
        source_policy_revision=source_policy_revision,
        source_policy_sha256=source_policy_sha256,
        budget_binding_sha256=budget_binding_sha256,
        budget_quota_key=budget_quota_key,
        budget_policy_revision=budget_policy_revision,
        budget_policy_sha256=budget_policy_sha256,
    )
    scope = SourceAttemptScopeV3(
        attempt=attempt,
        request_scheme="https",
        request_method=_CSI500_V3_EXPECTED_METHOD,
        request_host=_CSI500_V3_EXPECTED_HTTPS_HOST,
        request_path=_CSI500_V3_EXPECTED_PATH,
        capture_intent_id=capture_intent_id,
        request_summary_sha256=request_summary,
        source_policy_id=source_policy_id,
        source_policy_revision=source_policy_revision,
        source_policy_sha256=source_policy_sha256,
        budget_binding_sha256=budget_binding_sha256,
        budget_quota_key=budget_quota_key,
        budget_policy_revision=budget_policy_revision,
        budget_policy_sha256=budget_policy_sha256,
    )
    intent_sha256 = _domain_sha256(
        "opendata.csi500.one-shot-intent.v1",
        {
            **intent_context,
            "attempt_id": attempt.attempt_id,
            "capture_intent_id": capture_intent_id,
            "job_version": task.version,
            "request_id": attempt.request_id,
            "run_key": run_identity.idempotency_key,
            "run_payload_hash": run_identity.payload_hash,
        },
    )
    return Csi500FuyaoV4SampleSpec(
        business_date=business_date,
        capture_intent_id=capture_intent_id,
        intent_sha256=intent_sha256,
        task_definition=task,
        request=request,
        run_identity=run_identity,
        source_attempt_binding=binding,
        source_attempt_scope_v3_pin=pin,
        source_attempt_scope=scope,
        target_id=target_id,
        target_identity_sha256=target_identity_sha256,
        calendar_manifest_sha256=calendar_manifest_sha256,
        calendar_artifact_sha256=calendar_artifact_sha256,
        authorization_actor=authorization_actor,
        grant_expires_at_utc=grant_expires_at_utc,
        rights_evidence_sha256=rights_evidence_sha256,
        source_policy_id=source_policy_id,
        source_policy_revision=source_policy_revision,
        source_policy_sha256=source_policy_sha256,
        budget_binding_sha256=budget_binding_sha256,
        budget_quota_key=budget_quota_key,
        budget_policy_revision=budget_policy_revision,
        budget_policy_sha256=budget_policy_sha256,
    )


def build_csi500_v4_sample_task_definition(*, capture_intent_id: str) -> TaskDefinition:
    """Build a disabled/manual-only task identity for one V4 sample intent."""
    if type(capture_intent_id) is not str or _SHA256_RE.fullmatch(capture_intent_id) is None:
        raise ValueError("capture_intent_id must be a lowercase SHA-256 digest")
    return TaskDefinition(
        job_id=INDEX_JOB_ID_BY_SYMBOL[_CSI500_SYMBOL],
        version=f"{SSE50_INDEX_JOB_VERSION}.fuyao-v4-sample-{capture_intent_id[:16]}",
        dataset_id=_CSI500_DATASET_ID,
        provider=_PROVIDER,
        model=_MODEL,
        timezone="Asia/Shanghai",
        calendar=None,
        schedule=None,
        enabled=False,
    )


def build_csi500_v4_sample_payload(*, spec: Csi500FuyaoV4SampleSpec) -> dict[str, JsonValue]:
    """Serialize the exact manual request represented by an offline spec."""
    if type(spec) is not Csi500FuyaoV4SampleSpec:
        raise TypeError("spec must be a Csi500FuyaoV4SampleSpec")
    request = spec.request
    return {
        "query": dict(request.query),
        "partition": dict(request.partition),
        "shard": request.shard,
        "idempotency_key": request.idempotency_key,
    }


def _domain_sha256(domain: str, payload: Mapping[str, object]) -> str:
    """Hash canonical JSON under a separate domain for deterministic IDs."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded).hexdigest()


def _csi500_v4_source_policy_sha256() -> str:
    """Return the digest of this code path's caller-declared fixed policy."""
    policy_document = {
        "authorization_basis": "USER_DECLARED_UNVERIFIED",
        "canonical_model": _MODEL,
        "dataset_id": _CSI500_DATASET_ID,
        "external_distribution_allowed": False,
        "host": _CSI500_V3_EXPECTED_HTTPS_HOST,
        "method": _CSI500_V3_EXPECTED_METHOD,
        "operation": "query",
        "path": _CSI500_V3_EXPECTED_PATH,
        "purposes": list(_ALLOWED_INTERNAL_PURPOSES),
        "source": _PROVIDER,
        "symbol": _CSI500_SYMBOL,
        "window_calendar_days": 1,
    }
    return _domain_sha256("opendata.csi500.source-policy.v1", policy_document)


def build_sse50_index_daily_job(
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled, unreviewed THS SSE 50 publication job."""
    return build_index_daily_job(SSE50_INDEX_SYMBOL, contract_registry)


def build_authorized_csi500_index_daily_job(
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoSourceAccess,
) -> ReviewedJob:
    """Build an explicitly injected, unscheduled CSI 500 one-shot job.

    The returned job is enabled only for the dedicated one-shot dispatcher; it
    stays disabled and unscheduled, and cannot enter ordinary backfill or
    scheduled dispatch. The caller must inject an existing persistent source
    gate binding. Its send-time reservations, not this builder, authorize
    physical requests.
    """
    _require_csi500_source_access(source_access)
    return _build_index_daily_job(
        _CSI500_SYMBOL,
        contract_registry,
        source_access=source_access,
    )


def build_csi500_v3_sample_index_daily_job(
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoV3SampleAccess,
) -> ReviewedJob:
    """Build the separate V3 CSI 500 sample job without V1 fallback.

    This opt-in path is intentionally distinct from the accepted V1 #129 job.
    Its durable quota and target-identity receipt still require independent
    sample-composition review before it can be exposed to a live executor.
    """
    _require_csi500_v3_sample_access(source_access)
    return _build_index_daily_job(
        _CSI500_SYMBOL,
        contract_registry,
        source_access=None,
        source_access_v3=source_access,
    )


def build_csi500_v4_sample_index_daily_job(
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoV4SampleAccess,
) -> ReviewedJob:
    """Build the isolated V4 job from one already-locked sample access.

    The task remains disabled and unscheduled. This builder only carries the
    immutable V4 scope, request pins, and stable IDs into the normal fetcher
    scope; it does not configure the gate, obtain credentials, or run a
    provider call.
    """
    _require_csi500_v4_sample_access(source_access)
    return _build_index_daily_job(
        _CSI500_SYMBOL,
        contract_registry,
        source_access=None,
        source_access_v4=source_access,
    )


def build_index_daily_job(
    symbol: str,
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled publication job for one whitelisted THS index.

    The job uses the registered THS provider, normalizer, and immutable v2
    publication writer. Its fixed task is intentionally not scheduled or
    enabled until source rights and dataset-specific validation are reviewed.
    Each symbol maps to its own dataset and job identity, so the records route
    never combines index series that lack a request-time symbol filter.

    Args:
        symbol: Exact supported index symbol including its market suffix.
        contract_registry: Explicit registry containing the exact per-symbol
            OHLCV contract produced by :func:`build_index_daily_contract`.

    Returns:
        A disabled and unreviewed job bound to one exact symbol and dataset.

    Raises:
        ValueError: If the symbol is not whitelisted or its contract mismatches.
        TypeError: If the supplied dependency is not a contract registry.
    """
    return _build_index_daily_job(symbol, contract_registry, source_access=None)


def _build_index_daily_job(
    symbol: str,
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoSourceAccess | None,
    source_access_v3: Csi500FuyaoV3SampleAccess | None = None,
    source_access_v4: Csi500FuyaoV4SampleAccess | None = None,
) -> ReviewedJob:
    """Build the shared per-symbol job, with opt-in one-shot access if bound."""
    access_count = sum(
        access is not None for access in (source_access, source_access_v3, source_access_v4)
    )
    if access_count > 1:
        raise ValueError("V1, V3, and V4 source access cannot be combined")
    dataset_id = _validate_symbol(symbol)
    if source_access is not None:
        _require_csi500_source_access(source_access)
        if symbol != _CSI500_SYMBOL or dataset_id != source_access.dataset_id:
            raise ValueError("source access is bound only to the CSI 500 dataset")
    if source_access_v3 is not None:
        _require_csi500_v3_sample_access(source_access_v3)
        if symbol != _CSI500_SYMBOL or dataset_id != source_access_v3.dataset_id:
            raise ValueError("V3 sample access is bound only to the CSI 500 dataset")
    if source_access_v4 is not None:
        _require_csi500_v4_sample_access(source_access_v4)
        if symbol != _CSI500_SYMBOL or dataset_id != source_access_v4.dataset_id:
            raise ValueError("V4 sample access is bound only to the CSI 500 dataset")
    sample_source_access = source_access or source_access_v3 or source_access_v4
    if not isinstance(contract_registry, DatasetContractRegistry):
        raise TypeError("contract_registry must be a DatasetContractRegistry")
    expected_contract = build_index_daily_contract(symbol)
    try:
        registered_contract = contract_registry.resolve(
            dataset_id,
            INDEX_SCHEMA_VERSION,
            INDEX_SEMANTIC_VERSION,
        )
    except DatasetContractRegistryError as exc:
        raise ValueError("the exact single-index contract is not registered") from exc
    if canonical_sha256(registered_contract) != canonical_sha256(expected_contract):
        raise ValueError("the registered single-index contract does not match this job")

    if source_access_v4 is not None:
        job_task = source_access_v4.sample_spec.task_definition
    else:
        job_task = TaskDefinition(
            job_id=INDEX_JOB_ID_BY_SYMBOL[symbol],
            version=_job_version_for(source_access or source_access_v3),
            dataset_id=dataset_id,
            provider=_PROVIDER,
            model=_MODEL,
            timezone="Asia/Shanghai",
            calendar=None,
            schedule=None,
            enabled=False,
        )
    fetcher = ThsIndexDailyFetcher(
        source_attempt_gate=(
            source_access.source_attempt_gate
            if source_access is not None
            else source_access_v3.source_attempt_gate
            if source_access_v3 is not None
            else source_access_v4.source_attempt_gate
            if source_access_v4 is not None
            else None
        ),
        max_attempts=(_MAX_TRANSPORT_ATTEMPTS if sample_source_access is not None else None),
    )
    writer = ValidatedPublicationWriter(contract_registry)
    one_shot_only = sample_source_access is not None

    def fetch(request: RunRequest, context: RunExecutionContext) -> object:
        """Fetch one explicitly requested range through the registered THS provider."""
        if source_access_v4 is not None:
            start, end = _validate_v4_sample_request(request, source_access_v4)
            if context.run_key != source_access_v4.sample_spec.run_key:
                raise ValueError("V4 sample run key differs from its locked transport identity")
        elif source_access_v3 is not None:
            start, end = _validate_v3_sample_request(request, source_access_v3)
        else:
            start, end = _validate_request(request, symbol, one_shot_only=one_shot_only)
        context.checkpoint()
        if source_access is not None:
            budget = _csi500_one_attempt_budget()
            binding = source_access.binding_for_run(context.run_key)
            with request_execution_scope(
                source=_PROVIDER,
                canonical_model=_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=binding,
            ):
                result = fetcher.fetch(
                    ctx=FetchContext(
                        timeout=_FETCH_TIMEOUT_SECONDS,
                        request_budget=budget,
                    ),
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                )
        elif source_access_v3 is not None:
            if start != source_access_v3.business_date or end != source_access_v3.business_date:
                raise ValueError("V3 sample request differs from its authorized business date")
            budget = _csi500_one_attempt_budget()
            binding = source_access_v3.binding_for_run(context.run_key)
            v3_pin = source_access_v3.pin_for_request(start, end)
            with request_execution_scope(
                source=_PROVIDER,
                canonical_model=_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=binding,
                source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
                source_attempt_scope_v3_pin=v3_pin,
            ):
                result = fetcher.fetch(
                    ctx=FetchContext(
                        timeout=_FETCH_TIMEOUT_SECONDS,
                        request_budget=budget,
                    ),
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                )
        elif source_access_v4 is not None:
            spec = source_access_v4.sample_spec
            if start != spec.business_date or end != spec.business_date:
                raise ValueError("V4 sample request differs from its locked business date")
            if context.run_key != spec.run_key:
                raise ValueError("V4 sample run key differs from its locked transport identity")
            budget = _csi500_one_attempt_budget()
            with request_execution_scope(
                source=_PROVIDER,
                canonical_model=_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=spec.source_attempt_binding,
                source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V4,
                source_attempt_scope_v3_pin=spec.source_attempt_scope_v3_pin,
                source_attempt_one_shot_scope_v4=spec.source_attempt_scope,
            ):
                result = fetcher.fetch(
                    ctx=FetchContext(
                        timeout=_FETCH_TIMEOUT_SECONDS,
                        request_budget=budget,
                    ),
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                )
        else:
            _require_ths_source_authorization()
        if sample_source_access is None:
            result = fetcher.fetch(
                ctx=FetchContext(timeout=_FETCH_TIMEOUT_SECONDS),
                symbol=symbol,
                start_date=start,
                end_date=end,
            )
        context.checkpoint()
        return result

    def normalize(
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        """Validate one provider result and serialize only this index's OHLCV."""
        if source_access_v4 is not None:
            start, end = _validate_v4_sample_request(request, source_access_v4)
            if context.run_key != source_access_v4.sample_spec.run_key:
                raise ValueError("V4 sample run key differs from its locked transport identity")
        elif source_access_v3 is not None:
            start, end = _validate_v3_sample_request(request, source_access_v3)
        else:
            start, end = _validate_request(request, symbol, one_shot_only=one_shot_only)
        context.checkpoint()
        if type(raw) is not tuple or not raw:
            raise ValueError("THS index daily result must be a non-empty tuple")
        bars: list[Bar] = []
        for row in raw:
            if not isinstance(row, Bar):
                raise ValueError("THS index daily result contains an invalid row")
            _validate_bar(row, start, end, symbol)
            bars.append(row)
        trade_dates = [bar.trade_date for bar in bars]
        if trade_dates != sorted(set(trade_dates)):
            raise ValueError("THS index daily rows must be unique and date ordered")

        records = tuple(_record_for_bar(bar, symbol) for bar in bars)
        return NormalizedBatch(
            records=records,
            schema_version=INDEX_SCHEMA_VERSION,
            semantic_version=INDEX_SEMANTIC_VERSION,
            unit="per-field",
            adjustment_basis="not_applicable",
            quality=dict(_QUALITY),
            provenance={
                "provider": _PROVIDER,
                "model": _MODEL,
                "dataset_id": dataset_id,
                "source_rights_status": (
                    "USER_DECLARED_INTERNAL_USE_UNVERIFIED"
                    if sample_source_access is not None
                    else "UNKNOWN"
                ),
                "source_rights_basis": (
                    sample_source_access.authorization_basis
                    if sample_source_access is not None
                    else "UNKNOWN"
                ),
                "source_rights_evidence_reference": (
                    sample_source_access.evidence_reference
                    if sample_source_access is not None
                    else None
                ),
                "source_endpoint_path": (
                    sample_source_access.endpoint_path if sample_source_access is not None else None
                ),
                "source_operation": (
                    sample_source_access.operation
                    if sample_source_access is not None
                    else "UNKNOWN"
                ),
                "source_allowed_uses": (
                    list(sample_source_access.purposes) if sample_source_access is not None else []
                ),
                "source_external_distribution_allowed": False,
                "source_comparison_status": "unknown",
                "source_data_origin_status": "unknown",
                "coverage_status": "unknown",
                "source_history_boundary": _HISTORY_BOUNDARY_UNKNOWN,
                "source_history_boundary_evidence": None,
                "source_cross_execution_attempt_limit": "UNKNOWN",
                "source_per_request_calendar_day_limit": (
                    sample_source_access.maximum_window_calendar_days
                    if sample_source_access is not None
                    else _MAX_WINDOW_CALENDAR_DAYS
                ),
                "source_per_run_transport_attempt_limit": (
                    _MAX_TRANSPORT_ATTEMPTS if sample_source_access is not None else None
                ),
                "requested_start_inclusive": start.isoformat(),
                "requested_end_inclusive": end.isoformat(),
            },
        )

    def publish(
        connection: Connection,
        task: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        idempotency_key: str,
    ) -> PublicationReceipt:
        """Enforce the fixed dataset/symbol boundary before the shared writer."""
        if source_access_v4 is not None:
            _validate_v4_sample_request(request, source_access_v4)
        elif source_access_v3 is not None:
            _validate_v3_sample_request(request, source_access_v3)
        else:
            _validate_request(request, symbol, one_shot_only=one_shot_only)
        if (
            task.job_id != job_task.job_id
            or task.version != job_task.version
            or task.dataset_id != dataset_id
            or task.provider != _PROVIDER
            or task.model != _MODEL
        ):
            raise ValueError("publication task does not match this single-index job")
        if not batch.records:
            raise ValueError("empty index batches cannot be published")
        for record in batch.records:
            if not isinstance(record, NormalizedRecord) or not isinstance(record.payload, Mapping):
                raise ValueError("publication batch contains an invalid index record")
            if record.payload.get("symbol") != symbol:
                raise ValueError("publication batch contains a different index symbol")
        if (
            batch.schema_version != INDEX_SCHEMA_VERSION
            or batch.semantic_version != INDEX_SEMANTIC_VERSION
        ):
            raise ValueError("publication batch does not match this single-index contract")
        return writer(connection, task, request, batch, idempotency_key)

    return ReviewedJob(
        task=job_task,
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=one_shot_only,
        one_shot_only=one_shot_only,
    )


def _require_ths_source_authorization() -> None:
    """Refuse real source acquisition until a reviewed rights decision exists."""
    raise ThsIndexSourceAuthorizationUnknownError


def _require_csi500_source_access(value: object) -> Csi500FuyaoSourceAccess:
    """Require the exact CSI 500 user-declared source access binding."""
    if type(value) is not Csi500FuyaoSourceAccess:
        raise TypeError("source_access must be an exact Csi500FuyaoSourceAccess")
    return value


def _require_csi500_v3_sample_access(value: object) -> Csi500FuyaoV3SampleAccess:
    """Require the explicit V3 sample access type; never upgrade V1 access."""
    if type(value) is not Csi500FuyaoV3SampleAccess:
        raise TypeError("source_access must be an exact Csi500FuyaoV3SampleAccess")
    return value


def _require_csi500_v4_sample_access(value: object) -> Csi500FuyaoV4SampleAccess:
    """Require the exact locked V4 sample access type; never fall back to V1/V3."""
    if type(value) is not Csi500FuyaoV4SampleAccess:
        raise TypeError("source_access must be an exact Csi500FuyaoV4SampleAccess")
    return value


def _validate_window(start_date: object, end_date: object) -> tuple[date, date]:
    """Validate one inclusive date range against the endpoint's ten-year cap."""
    start, end = _validate_date_order(start_date, end_date)
    inclusive_days = (end - start).days + 1
    if inclusive_days > _MAX_WINDOW_CALENDAR_DAYS:
        raise ValueError("requested window exceeds the THS ten-year per-request limit")
    return start, end


def _validate_date_order(start_date: object, end_date: object) -> tuple[date, date]:
    """Validate date types and inclusive ordering without imposing a span cap."""
    if type(start_date) is not date:
        raise TypeError("start_date must be a date")
    if type(end_date) is not date:
        raise TypeError("end_date must be a date")
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")
    return start_date, end_date


def _csi500_one_attempt_budget() -> RequestBudget:
    """Build the fixed in-memory grant for one exact CSI 500 source send."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    if not host:
        raise ValueError("Fuyao default API host is unavailable")
    grant = RequestGrant(
        source=_PROVIDER,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=_SOURCE_RIGHTS_EVIDENCE,
        task_attempts=_MAX_TRANSPORT_ATTEMPTS,
        source_attempts=_MAX_TRANSPORT_ATTEMPTS,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return RequestBudget(
        task_attempts=_MAX_TRANSPORT_ATTEMPTS,
        source_attempts=_MAX_TRANSPORT_ATTEMPTS,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )


def _job_version_for(
    source_access: Csi500FuyaoSourceAccess | Csi500FuyaoV3SampleAccess | None,
) -> str:
    """Pin one-shot run snapshots to the exact grant revision and fixed scope."""
    if source_access is None:
        return SSE50_INDEX_JOB_VERSION
    binding = source_access.source_attempt_binding
    if type(source_access) is Csi500FuyaoV3SampleAccess:
        scope = "\x00".join(
            (
                "source-attempt-scope-v3",
                source_access.evidence_reference,
                source_access.dataset_id,
                source_access.symbol,
                source_access.endpoint_path,
                source_access.operation,
                *source_access.purposes,
                source_access.business_date.isoformat(),
                source_access.capture_intent_id,
                source_access.source_policy_id,
                source_access.source_policy_revision,
                source_access.source_policy_sha256,
                source_access.budget_binding_sha256,
                source_access.budget_quota_key,
                str(source_access.budget_policy_revision),
                source_access.budget_policy_sha256,
                source_access.target_identity_sha256,
                binding.grant_id,
                str(binding.expected_revision),
                binding.principal_scope,
                str(_MAX_TRANSPORT_ATTEMPTS),
            )
        )
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        return f"{SSE50_INDEX_JOB_VERSION}.fuyao-v3-sample-{digest}"
    scope = "\x00".join(
        (
            source_access.evidence_reference,
            source_access.dataset_id,
            source_access.symbol,
            source_access.endpoint_path,
            source_access.operation,
            *source_access.purposes,
            str(source_access.maximum_window_calendar_days),
            str(_MAX_TRANSPORT_ATTEMPTS),
            binding.grant_id,
            str(binding.expected_revision),
            binding.principal_scope,
        )
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return f"{SSE50_INDEX_JOB_VERSION}.fuyao-{digest}"


def _validate_symbol(symbol: str) -> str:
    """Resolve one exact whitelisted symbol to its dedicated dataset identity."""
    if not isinstance(symbol, str) or symbol not in INDEX_DATASET_ID_BY_SYMBOL:
        raise ValueError("symbol must be one of the four reviewed benchmark codes")
    return INDEX_DATASET_ID_BY_SYMBOL[symbol]


def _validate_request(
    request: RunRequest,
    symbol: str,
    *,
    one_shot_only: bool = False,
) -> tuple[date, date]:
    """Require the exact fixed-symbol backfill or one-shot window shape."""
    if not isinstance(request, RunRequest):
        raise TypeError("request must be a RunRequest")
    if request.partition != {"symbol": symbol} or request.shard is not None:
        raise ValueError("request must be a fixed-symbol index backfill")
    if one_shot_only:
        if (
            request.run_type != "manual"
            or set(request.query) != {"start_date", "end_date"}
            or request.backfill_start is not None
            or request.backfill_end is not None
        ):
            raise ValueError("one-shot request must contain only one explicit date window")
        start = _parse_iso_date(request.query["start_date"], "start_date")
        end = _parse_iso_date(request.query["end_date"], "end_date")
        return _validate_window(start, end)
    if (
        request.run_type != "backfill"
        or request.query != {}
        or request.backfill_start is None
        or request.backfill_end is None
    ):
        raise ValueError("request must be a fixed-symbol index backfill")
    return _validate_date_order(request.backfill_start, request.backfill_end)


def _validate_v3_sample_request(
    request: RunRequest,
    source_access: Csi500FuyaoV3SampleAccess,
) -> tuple[date, date]:
    """Require a manual request for exactly the authorized sample date."""
    start, end = _validate_request(request, _CSI500_SYMBOL, one_shot_only=True)
    if start != source_access.business_date or end != source_access.business_date:
        raise ValueError("V3 sample request differs from its authorized business date")
    return start, end


def _validate_v4_sample_request(
    request: RunRequest,
    source_access: Csi500FuyaoV4SampleAccess,
) -> tuple[date, date]:
    """Require the exact RunRequest identity sealed into the V4 scope."""
    spec = source_access.sample_spec
    if type(request) is not RunRequest or request != spec.request:
        raise ValueError("V4 sample request differs from its locked request identity")
    start, end = _validate_request(request, _CSI500_SYMBOL, one_shot_only=True)
    if start != spec.business_date or end != spec.business_date:
        raise ValueError("V4 sample request differs from its locked business date")
    return start, end


def _parse_iso_date(value: object, name: str) -> date:
    """Parse an exact canonical ISO calendar date from the manual query."""
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date string") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date string")
    return parsed


def _validate_bar(bar: Bar, start: date, end: date, symbol: str) -> None:
    """Reject rows outside the requested index or with invalid numeric values."""
    if bar.symbol != symbol:
        raise ValueError("THS provider returned a different index symbol")
    if not start <= bar.trade_date <= end:
        raise ValueError("THS provider returned a date outside the requested window")
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise ValueError("THS provider returned a non-finite OHLCV value")
    if any(value < 0 for value in values):
        raise ValueError("THS provider returned a negative OHLCV value")
    if (
        bar.high < bar.low
        or bar.high < bar.open
        or bar.high < bar.close
        or bar.low > bar.open
        or bar.low > bar.close
    ):
        raise ValueError("THS provider returned inconsistent OHLC values")


def _record_for_bar(bar: Bar, symbol: str) -> NormalizedRecord:
    """Create a business-date row without inventing source timestamps."""
    trade_date = bar.trade_date.isoformat()
    payload: dict[str, JsonValue] = {
        "symbol": symbol,
        "trade_date": trade_date,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }
    return NormalizedRecord(
        business_key=json.dumps(
            [symbol, trade_date],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        payload=payload,
        business_date=bar.trade_date,
    )
