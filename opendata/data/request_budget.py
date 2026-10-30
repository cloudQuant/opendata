"""Finite request budgets and trusted-code grants for template fetches.

This module deliberately contains no provider registry, credential, or
network-discovery logic. A grant is a trusted-code declaration that must be
constructed explicitly; a request budget with no grants authorizes nothing.
"""

from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from threading import Event


MAX_TASK_ATTEMPTS = 10
MAX_SOURCE_ATTEMPTS = 3
# These are local project guardrails for explicitly bounded requests. They are
# not provider-published quotas or evidence that a source permits this volume.
MAX_BOUNDED_TASK_ATTEMPTS = 256
MAX_BOUNDED_SOURCE_ATTEMPTS = 64
SOURCE_ATTEMPT_SCOPE_V1 = 1
SOURCE_ATTEMPT_SCOPE_V3 = 3
_HOST_LOCK_POLL_SECONDS = 0.05
_SOURCE_ATTEMPT_BINDING_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_SOURCE_ATTEMPT_BINDING_REVISION = 2_147_483_647
_SOURCE_ATTEMPT_SCOPE_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REQUEST_SUMMARY_FIELD_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SOURCE_ATTEMPT_HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SOURCE_ATTEMPT_METHOD_PATTERN = re.compile(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}")
_SOURCE_ATTEMPT_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/"
)


class RequestBudgetError(RuntimeError):
    """Base class for safe, non-transport request-budget failures."""


class RequestAuthorizationError(RequestBudgetError):
    """No current trusted-code grant authorizes this exact request."""


class RequestAttemptLimitError(RequestBudgetError):
    """The finite task or source attempt allowance is exhausted."""


class RequestExecutionCancelledError(RequestBudgetError):
    """The owning Fetcher call was cancelled before transport."""


class RequestExecutionDeadlineError(RequestBudgetError):
    """The owning Fetcher call's deadline expired before transport."""


class RequestBudgetScopeError(RequestBudgetError):
    """A nested execution tried to replace a stricter active budget."""


class RequestRedirectError(RequestBudgetError):
    """A scoped request received a redirect that could hide another host."""


class RequestAdapterRetryError(RequestAuthorizationError):
    """A requests adapter could perform unaccounted hidden retries."""


class RequestOperation(str, Enum):
    """Operations that a trusted-code request grant may authorize."""

    QUERY = "query"
    STORE = "store"
    EXPORT = "export"
    SUBSCRIBE = "subscribe"


class RequestBudgetProfile(str, Enum):
    """Named local ceilings for conservative and explicitly bounded requests."""

    LIVE_SMOKE = "live_smoke"
    BOUNDED_REQUEST = "bounded_request"


class GrantDecision(str, Enum):
    """Explicit state of one trusted-code rights declaration."""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceAttemptBinding:
    """Trusted-code reference to one source-attempt gate grant revision.

    This value carries backend policy identity only. It does not prove rights,
    create a request grant, or enable a source-attempt gate. Callers must never
    populate it from request bodies, query parameters, or headers. An optional
    expected evidence reference is compared against the persisted grant in the
    same transaction that reserves a physical source attempt.
    """

    grant_id: str
    expected_revision: int
    principal_scope: str
    run_key: str | None = None
    expected_rights_evidence_reference: str | None = None
    _seal: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate exact field types and bounded gate identifiers."""
        self._validate_fields()
        object.__setattr__(self, "_seal", self._snapshot())

    def _validate_fields(self) -> None:
        """Validate current binding fields without changing the integrity seal."""
        _validate_source_attempt_binding_identifier(self.grant_id, "grant_id")
        _validate_source_attempt_binding_identifier(self.principal_scope, "principal_scope")
        if self.run_key is not None:
            _validate_source_attempt_binding_identifier(self.run_key, "run_key")
        evidence_reference = self.expected_rights_evidence_reference
        if evidence_reference is not None and (
            type(evidence_reference) is not str
            or not evidence_reference
            or evidence_reference != evidence_reference.strip()
            or len(evidence_reference) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in evidence_reference)
        ):
            raise ValueError("expected_rights_evidence_reference must be bounded plain text")
        if (
            type(self.expected_revision) is not int
            or not 1 <= self.expected_revision <= _MAX_SOURCE_ATTEMPT_BINDING_REVISION
        ):
            raise ValueError(
                "expected_revision must be an integer from 1 through "
                f"{_MAX_SOURCE_ATTEMPT_BINDING_REVISION}"
            )

    def _snapshot(self) -> tuple[object, ...]:
        """Return the immutable binding fields used by a V3 send scope."""
        return (
            self.grant_id,
            self.expected_revision,
            self.principal_scope,
            self.run_key,
            self.expected_rights_evidence_reference,
        )

    def validate_integrity(self) -> None:
        """Reject post-construction mutation of the grant and rights binding."""
        try:
            self._validate_fields()
        except Exception as exc:
            raise RequestAuthorizationError("source attempt binding is invalid") from exc
        if self._snapshot() != self._seal:
            raise RequestAuthorizationError("source attempt binding integrity check failed")


@dataclass(frozen=True, slots=True)
class SourceAttemptScopeV3Pin:
    """Trusted, stable pins used to build one V3 gate scope per HTTP attempt.

    The request digest uses the project's version 1 allowlisted scalar query
    summary. ``request_summary_allowed_fields`` is a fixed trusted-code
    declaration and must exactly match the keys in the prepared GET query.
    The separately pinned HTTPS host, path, and method are checked against the
    prepared request. All policy and route values remain caller assertions from
    trusted job code; this carrier does not authenticate its caller or consult
    a source-policy registry. Its integrity seal detects accidental mutation,
    not malicious code in the same process. It does not describe headers,
    network delivery, or provider receipt. Grant identity and the exact rights
    reference remain in :class:`SourceAttemptBinding`.
    """

    capture_intent_id: str
    request_summary_sha256: str
    request_summary_allowed_fields: tuple[str, ...]
    expected_https_host: str
    expected_path: str
    expected_method: str
    source_policy_id: str
    source_policy_revision: str
    source_policy_sha256: str
    budget_binding_sha256: str
    budget_quota_key: str
    budget_policy_revision: int
    budget_policy_sha256: str
    _seal: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate and seal bounded identity and policy pins."""
        self._validate_fields()
        object.__setattr__(self, "_seal", self._snapshot())

    def _validate_fields(self) -> None:
        """Validate current fields without changing the original integrity seal."""
        for field_name in (
            "capture_intent_id",
            "source_policy_id",
            "source_policy_revision",
            "budget_quota_key",
        ):
            _validate_source_attempt_binding_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "request_summary_sha256",
            "source_policy_sha256",
            "budget_binding_sha256",
            "budget_policy_sha256",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or _SOURCE_ATTEMPT_SCOPE_SHA256_PATTERN.fullmatch(value) is None
            ):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        if type(self.budget_policy_revision) is not int or not (
            1 <= self.budget_policy_revision <= _MAX_SOURCE_ATTEMPT_BINDING_REVISION
        ):
            raise ValueError("budget_policy_revision is outside the supported range")
        fields = self.request_summary_allowed_fields
        if type(fields) is not tuple or any(
            type(name) is not str or _REQUEST_SUMMARY_FIELD_PATTERN.fullmatch(name) is None
            for name in fields
        ):
            raise ValueError("request_summary_allowed_fields must be a tuple of exact field names")
        if len(fields) != len(set(fields)):
            raise ValueError("request_summary_allowed_fields must not contain duplicates")
        if tuple(sorted(fields)) != fields:
            raise ValueError("request_summary_allowed_fields must use sorted canonical order")
        _validate_source_attempt_scope_v3_host(self.expected_https_host)
        _validate_source_attempt_scope_v3_path(self.expected_path)
        if (
            type(self.expected_method) is not str
            or _SOURCE_ATTEMPT_METHOD_PATTERN.fullmatch(self.expected_method) is None
            or self.expected_method != "GET"
        ):
            raise ValueError("expected_method must be the supported exact GET method")

    def _snapshot(self) -> tuple[object, ...]:
        """Return the immutable fields that bind this V3 transport pin."""
        return (
            self.capture_intent_id,
            self.request_summary_sha256,
            self.request_summary_allowed_fields,
            self.expected_https_host,
            self.expected_path,
            self.expected_method,
            self.source_policy_id,
            self.source_policy_revision,
            self.source_policy_sha256,
            self.budget_binding_sha256,
            self.budget_quota_key,
            self.budget_policy_revision,
            self.budget_policy_sha256,
        )

    def validate_integrity(self) -> None:
        """Reject post-construction mutation of the frozen transport pins."""
        try:
            self._validate_fields()
        except Exception as exc:
            raise RequestAuthorizationError("source attempt V3 pin is invalid") from exc
        if self._snapshot() != self._seal:
            raise RequestAuthorizationError("source attempt V3 pin integrity check failed")


def _validate_source_attempt_scope_v3_host(host: object) -> None:
    """Require one canonical DNS-style HTTPS host without userinfo or port."""
    if type(host) is not str or not host or len(host) > 253 or host != host.lower():
        raise ValueError("expected_https_host must be a canonical lowercase host")
    labels = host.split(".")
    if any(
        len(label) > 63 or _SOURCE_ATTEMPT_HOST_LABEL_PATTERN.fullmatch(label) is None
        for label in labels
    ):
        raise ValueError("expected_https_host must be a canonical lowercase host")


def _validate_source_attempt_scope_v3_path(path: object) -> None:
    """Require one canonical ASCII origin-form path without ambiguous segments."""
    if (
        type(path) is not str
        or len(path) > 2048
        or not path.startswith("/")
        or "//" in path
        or "?" in path
        or "#" in path
        or "\\" in path
        or any(character not in _SOURCE_ATTEMPT_PATH_CHARS for character in path)
    ):
        raise ValueError("expected_path must be an exact ASCII absolute path")
    segments = path[1:].split("/")
    if any(segment in (".", "..") for segment in segments):
        raise ValueError("expected_path must not contain dot segments")


def _validate_source_attempt_binding_identifier(value: object, field_name: str) -> None:
    """Validate one exact, bounded identifier used by a trusted gate binding."""
    if (
        type(value) is not str
        or _SOURCE_ATTEMPT_BINDING_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be an exact bounded identifier")


def _validate_profile(value: object) -> RequestBudgetProfile:
    if type(value) is not RequestBudgetProfile:
        raise ValueError("profile must be an exact RequestBudgetProfile")
    return value


def _profile_attempt_limits(profile: RequestBudgetProfile) -> tuple[int, int]:
    exact_profile = _validate_profile(profile)
    if exact_profile is RequestBudgetProfile.LIVE_SMOKE:
        return MAX_TASK_ATTEMPTS, MAX_SOURCE_ATTEMPTS
    return MAX_BOUNDED_TASK_ATTEMPTS, MAX_BOUNDED_SOURCE_ATTEMPTS


def _validate_quota(value: object, *, maximum: int, name: str) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError(f"{name} must be an integer from 0 through {maximum}")
    return value


def _canonical_host(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("allowed_hosts must contain exact HTTPS hostnames")
    if any(token in value for token in ("://", "/", "?", "#", "@", "*")):
        raise ValueError("allowed_hosts must contain exact HTTPS hostnames")
    try:
        parts = urlsplit(f"https://{value}")
        port = parts.port
    except ValueError as exc:
        raise ValueError("allowed_hosts must contain exact HTTPS hostnames") from exc
    host = parts.hostname
    if host is None or port is not None or parts.path or parts.query or parts.fragment:
        raise ValueError("allowed_hosts must contain exact HTTPS hostnames")
    return host.lower()


def _validate_identity(source: object, canonical_model: object) -> tuple[str, str]:
    if not isinstance(source, str) or not source or source != source.strip():
        raise ValueError("source must be a nonempty exact identifier")
    if (
        not isinstance(canonical_model, str)
        or not canonical_model
        or canonical_model != canonical_model.strip()
    ):
        raise ValueError("canonical_model must be a nonempty exact identifier")
    return source, canonical_model


def _grant_snapshot(grant: RequestGrant) -> tuple[object, ...]:
    return (
        grant.source,
        grant.canonical_model,
        grant.operation,
        grant.decision,
        grant.rights_evidence,
        grant.task_attempts,
        grant.source_attempts,
        grant.profile,
        grant.allowed_hosts,
        grant.expires_at,
        grant.conditions,
    )


@dataclass(frozen=True, slots=True)
class RequestGrant:
    """Immutable trusted-code authority for one source/model/operation.

    Host sets and conditions are copied into immutable tuples during
    construction. ``expires_at`` must be an aware datetime. Quotas are
    explicit even for DENIED or UNKNOWN declarations.
    """

    source: str
    canonical_model: str
    operation: RequestOperation
    decision: GrantDecision
    rights_evidence: str
    task_attempts: int
    source_attempts: int
    allowed_hosts: frozenset[str] | Iterable[str]
    expires_at: datetime
    conditions: tuple[str, ...] | Iterable[str] = ()
    profile: RequestBudgetProfile = field(
        default=RequestBudgetProfile.LIVE_SMOKE,
        kw_only=True,
    )
    _seal: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate and freeze all grant fields and mutable input iterables."""
        source, model = _validate_identity(self.source, self.canonical_model)
        if not isinstance(self.operation, RequestOperation):
            raise ValueError("operation must be an explicit RequestOperation")
        if not isinstance(self.decision, GrantDecision):
            raise ValueError("decision must be an explicit GrantDecision")
        profile = _validate_profile(self.profile)
        max_task_attempts, max_source_attempts = _profile_attempt_limits(profile)
        if not isinstance(self.rights_evidence, str) or not self.rights_evidence.strip():
            raise ValueError("rights_evidence must be a nonempty reference")
        task_attempts = _validate_quota(
            self.task_attempts,
            maximum=max_task_attempts,
            name="task_attempts",
        )
        source_attempts = _validate_quota(
            self.source_attempts,
            maximum=max_source_attempts,
            name="source_attempts",
        )
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime")
        try:
            if self.expires_at.utcoffset() is None:
                raise ValueError("expires_at has no UTC offset")
            expires_at = self.expires_at.astimezone(timezone.utc)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("expires_at must be a valid timezone-aware datetime") from exc

        raw_hosts = self.allowed_hosts
        if isinstance(raw_hosts, (str, bytes)):
            raise ValueError("allowed_hosts must be an iterable of exact hostnames")
        try:
            hosts = frozenset(_canonical_host(host) for host in tuple(raw_hosts))
        except (TypeError, ValueError) as exc:
            raise ValueError("allowed_hosts must contain exact HTTPS hostnames") from exc
        if self.decision is GrantDecision.ALLOWED and not hosts:
            raise ValueError("an ALLOWED grant must name at least one exact HTTPS host")

        raw_conditions = self.conditions
        if isinstance(raw_conditions, (str, bytes)):
            raise ValueError("conditions must be an iterable of strings")
        try:
            conditions = tuple(raw_conditions)
        except TypeError as exc:
            raise ValueError("conditions must be an iterable of strings") from exc
        if any(not isinstance(item, str) or not item.strip() for item in conditions):
            raise ValueError("conditions must contain nonempty strings")

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "canonical_model", model)
        object.__setattr__(self, "task_attempts", task_attempts)
        object.__setattr__(self, "source_attempts", source_attempts)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "_seal", _grant_snapshot(self))

    def _validate_integrity(self) -> None:
        try:
            profile = _validate_profile(self.profile)
            max_task_attempts, max_source_attempts = _profile_attempt_limits(profile)
            _validate_quota(
                self.task_attempts,
                maximum=max_task_attempts,
                name="task_attempts",
            )
            _validate_quota(
                self.source_attempts,
                maximum=max_source_attempts,
                name="source_attempts",
            )
            if type(self.operation) is not RequestOperation:
                raise ValueError("operation must be an exact RequestOperation")
            if type(self.decision) is not GrantDecision:
                raise ValueError("decision must be an exact GrantDecision")
        except (TypeError, ValueError) as exc:
            raise RequestAuthorizationError("request grant integrity check failed") from exc
        if _grant_snapshot(self) != self._seal:
            raise RequestAuthorizationError("request grant integrity check failed")


@dataclass(frozen=True, slots=True)
class RequestExecutionScope:
    """One Fetcher template's source, model, operation, budget, and liveness.

    ``source_attempt_binding`` is optional trusted backend context. Nested
    scopes inherit it and cannot replace or add one beneath an unbound scope.
    """

    source: str
    canonical_model: str | None
    operation: RequestOperation
    budget: RequestBudget
    deadline: float | None = None
    cancellation: Event | None = field(default=None, repr=False, compare=False)
    source_attempt_binding: SourceAttemptBinding | None = field(default=None, repr=False)
    source_attempt_scope_version: int = SOURCE_ATTEMPT_SCOPE_V1
    source_attempt_scope_v3_pin: SourceAttemptScopeV3Pin | None = field(default=None, repr=False)

    def check_live(self) -> None:
        """Raise when cancellation or the enclosing deadline has fired."""
        if self.cancellation is not None and self.cancellation.is_set():
            raise RequestExecutionCancelledError("request execution was cancelled")
        if self.deadline is not None:
            if (
                isinstance(self.deadline, bool)
                or not isinstance(self.deadline, (int, float))
                or not math.isfinite(float(self.deadline))
            ):
                raise RequestExecutionDeadlineError("request execution deadline is invalid")
            if time.monotonic() >= self.deadline:
                raise RequestExecutionDeadlineError("request execution deadline expired")


def validate_source_attempt_scope_v3(
    scopes: tuple[RequestExecutionScope, ...],
) -> tuple[SourceAttemptBinding, SourceAttemptScopeV3Pin, str, RequestOperation]:
    """Validate and return one complete V3 grant binding and policy pin set.

    All enclosing scopes must carry the same explicit V3 discriminator, grant
    binding, exact rights reference, and policy pins. This function validates
    in-memory caller pins only; the durable gate must still recheck database
    state in the same transaction as each attempt reservation.
    """
    if not scopes:
        raise RequestAuthorizationError("source attempt V3 scope is missing")
    if any(type(scope) is not RequestExecutionScope for scope in scopes):
        raise RequestAuthorizationError("source attempt V3 scope type is invalid")
    first = scopes[0]
    source = first.source
    canonical_model = first.canonical_model
    operation = first.operation
    binding = first.source_attempt_binding
    pin = first.source_attempt_scope_v3_pin
    if (
        type(first.source) is not str
        or not source
        or source != source.strip()
        or type(canonical_model) is not str
        or not canonical_model
        or canonical_model != canonical_model.strip()
        or type(operation) is not RequestOperation
        or type(first.source_attempt_scope_version) is not int
        or first.source_attempt_scope_version != SOURCE_ATTEMPT_SCOPE_V3
        or type(binding) is not SourceAttemptBinding
        or type(pin) is not SourceAttemptScopeV3Pin
    ):
        raise RequestAuthorizationError("source attempt V3 scope is incomplete or invalid")
    binding.validate_integrity()
    pin.validate_integrity()
    rights_reference = binding.expected_rights_evidence_reference
    if type(binding.run_key) is not str or not rights_reference:
        raise RequestAuthorizationError("source attempt V3 binding is incomplete")
    if rights_reference != rights_reference.strip():
        raise RequestAuthorizationError("source attempt V3 rights reference is invalid")
    for scope in scopes:
        if (
            scope.source != source
            or scope.canonical_model != canonical_model
            or scope.operation is not operation
            or type(scope.source_attempt_scope_version) is not int
            or scope.source_attempt_scope_version != SOURCE_ATTEMPT_SCOPE_V3
            or type(scope.source_attempt_binding) is not SourceAttemptBinding
            or scope.source_attempt_binding != binding
            or type(scope.source_attempt_scope_v3_pin) is not SourceAttemptScopeV3Pin
            or scope.source_attempt_scope_v3_pin != pin
        ):
            raise RequestAuthorizationError("source attempt V3 scopes are inconsistent")
        scope.source_attempt_binding.validate_integrity()
        scope.source_attempt_scope_v3_pin.validate_integrity()
    return binding, pin, canonical_model, operation


@dataclass(frozen=True, slots=True)
class EffectiveRequestPolicy:
    """Validated policy and minimum quota across all nested active scopes."""

    budget: RequestBudget
    task_attempts: int
    source_attempts: int


class RequestBudget:
    """Thread-safe finite attempt accounting shared by Fetcher calls.

    ``task_attempts`` and ``source_attempts`` are hard local ceilings. A
    request also needs one exact, unexpired ALLOWED grant, so increasing a
    ceiling alone never grants network permission.
    """

    _task_attempt_limit: int
    _source_attempt_limit: int
    _profile: RequestBudgetProfile
    _grants: tuple[RequestGrant, ...]
    _policy_seal: tuple[RequestBudgetProfile, int, int, tuple[RequestGrant, ...]]
    _lock: threading.Lock
    _attempts_used: int
    _source_attempts_used: dict[str, int]
    _host_gates: dict[str, threading.Lock]

    __slots__ = (
        "_task_attempt_limit",
        "_source_attempt_limit",
        "_profile",
        "_grants",
        "_policy_seal",
        "_lock",
        "_attempts_used",
        "_source_attempts_used",
        "_host_gates",
    )

    def __init__(
        self,
        *,
        task_attempts: int = 0,
        source_attempts: int = 0,
        grants: Iterable[RequestGrant] = (),
        profile: RequestBudgetProfile = RequestBudgetProfile.LIVE_SMOKE,
    ) -> None:
        """Create a finite local ceiling plus explicit trusted-code grants."""
        exact_profile = _validate_profile(profile)
        max_task_attempts, max_source_attempts = _profile_attempt_limits(exact_profile)
        task_limit = _validate_quota(
            task_attempts,
            maximum=max_task_attempts,
            name="task_attempts",
        )
        source_limit = _validate_quota(
            source_attempts,
            maximum=max_source_attempts,
            name="source_attempts",
        )
        if isinstance(grants, (str, bytes)):
            raise ValueError("grants must be an iterable of RequestGrant")
        try:
            copied_grants = tuple(grants)
        except TypeError as exc:
            raise ValueError("grants must be an iterable of RequestGrant") from exc
        if any(not isinstance(grant, RequestGrant) for grant in copied_grants):
            raise ValueError("grants must contain only RequestGrant values")
        keys: set[tuple[str, str, RequestOperation]] = set()
        source_quotas: dict[str, int] = {}
        for grant in copied_grants:
            grant._validate_integrity()
            if grant.profile is not exact_profile:
                raise ValueError("request budget and grant profiles must match")
            key = (grant.source, grant.canonical_model, grant.operation)
            if key in keys:
                raise ValueError("duplicate source/model/operation grants are not allowed")
            keys.add(key)
            if grant.decision is GrantDecision.ALLOWED and not grant.conditions:
                prior_quota = source_quotas.setdefault(grant.source, grant.source_attempts)
                if prior_quota != grant.source_attempts:
                    raise ValueError(
                        "ALLOWED grants for one source must share an aggregate source quota"
                    )

        object.__setattr__(self, "_task_attempt_limit", task_limit)
        object.__setattr__(self, "_source_attempt_limit", source_limit)
        object.__setattr__(self, "_profile", exact_profile)
        object.__setattr__(self, "_grants", copied_grants)
        object.__setattr__(
            self,
            "_policy_seal",
            (exact_profile, task_limit, source_limit, copied_grants),
        )
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_attempts_used", 0)
        object.__setattr__(self, "_source_attempts_used", {})
        object.__setattr__(self, "_host_gates", {})

    def __setattr__(self, name: str, value: object) -> None:
        """Prevent ordinary mutation of the immutable budget policy."""
        raise AttributeError("RequestBudget policy is immutable")

    @property
    def task_attempt_limit(self) -> int:
        """Return the immutable task-wide ceiling."""
        return self._task_attempt_limit

    @property
    def source_attempt_limit(self) -> int:
        """Return the immutable aggregate per-source ceiling."""
        return self._source_attempt_limit

    @property
    def profile(self) -> RequestBudgetProfile:
        """Return the immutable named request profile."""
        return self._profile

    @property
    def grants(self) -> tuple[RequestGrant, ...]:
        """Return the copied immutable grant tuple."""
        return self._grants

    @property
    def attempts_used(self) -> int:
        """Return the task-wide number of actual transport attempts."""
        with self._lock:
            return self._attempts_used

    def attempts_used_for(self, source: str) -> int:
        """Return the attempts spent against one source in this budget."""
        with self._lock:
            return self._source_attempts_used.get(source, 0)

    def _validate_integrity(self) -> None:
        try:
            profile = _validate_profile(self._profile)
            max_task_attempts, max_source_attempts = _profile_attempt_limits(profile)
            _validate_quota(
                self._task_attempt_limit,
                maximum=max_task_attempts,
                name="task_attempts",
            )
            _validate_quota(
                self._source_attempt_limit,
                maximum=max_source_attempts,
                name="source_attempts",
            )
        except (TypeError, ValueError) as exc:
            raise RequestAuthorizationError("request budget integrity check failed") from exc
        if (
            self._profile,
            self._task_attempt_limit,
            self._source_attempt_limit,
            self._grants,
        ) != self._policy_seal:
            raise RequestAuthorizationError("request budget integrity check failed")
        for grant in self._grants:
            grant._validate_integrity()
            if grant.profile is not profile:
                raise RequestAuthorizationError("request budget integrity check failed")

    def _matching_grant(
        self,
        source: str,
        canonical_model: str | None,
        operation: RequestOperation,
        host: str,
    ) -> RequestGrant:
        grant = self._matching_identity_grant(source, canonical_model, operation)
        if host not in grant.allowed_hosts:
            raise RequestAuthorizationError("HTTPS host is outside the request grant")
        return grant

    def _matching_identity_grant(
        self,
        source: str,
        canonical_model: str | None,
        operation: RequestOperation,
    ) -> RequestGrant:
        """Find and validate a grant before a Fetcher starts source work."""
        self._validate_integrity()
        if canonical_model is None:
            raise RequestAuthorizationError("canonical model identity is required")
        grant = next(
            (
                candidate
                for candidate in self._grants
                if candidate.source == source
                and candidate.canonical_model == canonical_model
                and candidate.operation is operation
            ),
            None,
        )
        if grant is None:
            raise RequestAuthorizationError("no exact request grant is configured")
        if grant.decision is not GrantDecision.ALLOWED:
            raise RequestAuthorizationError("request grant is not explicitly ALLOWED")
        if grant.conditions:
            raise RequestAuthorizationError("request grant has unresolved conditions")
        if datetime.now(timezone.utc) >= grant.expires_at:
            raise RequestAuthorizationError("request grant is expired")
        return grant

    @contextmanager
    def host_slot(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        source: str,
        host: str,
        *,
        check_wait_liveness: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Serialize one host across clients sharing this budget.

        Args:
            scopes: Active request scopes authorizing this host slot.
            source: Registered source name.
            host: Governed host name.
            check_wait_liveness: Optional caller-owned deadline check run while
                waiting and after acquisition. It must use the caller's own clock.
        """
        if not scopes:
            yield
            return
        policy = authorize_request(scopes, source, host)
        if policy is None:  # pragma: no cover - guarded by nonempty scopes
            yield
            return
        with self._lock:
            gate = self._host_gates.setdefault(host, threading.Lock())
        while True:
            if check_wait_liveness is not None:
                check_wait_liveness()
            if gate.acquire(timeout=_HOST_LOCK_POLL_SECONDS):
                break
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            authorize_request(scopes, source, host)
            if check_wait_liveness is not None:
                check_wait_liveness()
            yield
        finally:
            gate.release()

    def reserve_attempt(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        source: str,
        host: str,
    ) -> None:
        """Atomically consume one attempt immediately before transport."""
        policy = authorize_request(scopes, source, host)
        if policy is None:  # pragma: no cover - callers reserve only scoped calls
            return
        with self._lock:
            self._validate_integrity()
            task_limit = policy.task_attempts
            source_limit = policy.source_attempts
            source_used = self._source_attempts_used.get(source, 0)
            if self._attempts_used >= task_limit or source_used >= source_limit:
                raise RequestAttemptLimitError("request attempt allowance exhausted")
            object.__setattr__(self, "_attempts_used", self._attempts_used + 1)
            self._source_attempts_used[source] = source_used + 1


def _effective_budget(scopes: tuple[RequestExecutionScope, ...]) -> RequestBudget:
    if not scopes:
        raise RequestAuthorizationError("request execution scope is missing")
    budget = scopes[0].budget
    for scope in scopes:
        if scope.budget is not budget:
            raise RequestBudgetScopeError("nested request budget cannot replace active budget")
    return budget


def check_scope_liveness(scopes: tuple[RequestExecutionScope, ...]) -> None:
    """Check cancellation and deadline for every enclosing scope."""
    for scope in scopes:
        scope.check_live()


def validate_scope_grants(scopes: tuple[RequestExecutionScope, ...]) -> None:
    """Validate exact model grants before a Fetcher starts provider I/O."""
    if not scopes:
        return
    check_scope_liveness(scopes)
    budget = _effective_budget(scopes)
    for scope in scopes:
        budget._matching_identity_grant(scope.source, scope.canonical_model, scope.operation)


def authorize_request(
    scopes: tuple[RequestExecutionScope, ...],
    source: str,
    host: str,
) -> EffectiveRequestPolicy | None:
    """Validate source/model/operation/host and return the tightest quotas."""
    if not scopes:
        return None
    check_scope_liveness(scopes)
    budget = _effective_budget(scopes)
    task_limits = [budget.task_attempt_limit]
    source_limits = [budget.source_attempt_limit]
    for scope in scopes:
        if scope.source != source:
            raise RequestAuthorizationError("request source differs from Fetcher scope")
        task_scope_model = scope.canonical_model
        grant = budget._matching_grant(source, task_scope_model, scope.operation, host)
        task_limits.append(grant.task_attempts)
        source_limits.append(grant.source_attempts)
    return EffectiveRequestPolicy(
        budget=budget,
        task_attempts=min(task_limits),
        source_attempts=min(source_limits),
    )


@contextmanager
def scoped_host_slot(
    scopes: tuple[RequestExecutionScope, ...],
    source: str,
    host: str,
    *,
    check_wait_liveness: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Acquire the shared budget's host gate when a request is scoped.

    Args:
        scopes: Active request scopes authorizing this host slot.
        source: Registered source name.
        host: Governed host name.
        check_wait_liveness: Optional caller-owned deadline check to run while
            waiting for and immediately before yielding the shared slot.
    """
    if not scopes:
        yield
        return
    budget = _effective_budget(scopes)
    with budget.host_slot(scopes, source, host, check_wait_liveness=check_wait_liveness):
        yield


def reserve_scoped_attempt(
    scopes: tuple[RequestExecutionScope, ...],
    source: str,
    host: str,
) -> None:
    """Consume one request attempt if this is a scoped Fetcher call."""
    if not scopes:
        return
    _effective_budget(scopes).reserve_attempt(scopes, source, host)


def _copy_scope_stack(
    current: tuple[RequestExecutionScope, ...],
    inherited: tuple[RequestExecutionScope, ...] | None,
) -> tuple[RequestExecutionScope, ...]:
    if inherited is None:
        return current
    if current and current != inherited:
        raise RequestBudgetScopeError("request execution scope inheritance is inconsistent")
    return current or inherited


_CURRENT_SCOPES: ContextVar[tuple[RequestExecutionScope, ...]] = ContextVar(
    "opendata_request_execution_scopes",
    default=(),
)


def current_request_scopes() -> tuple[RequestExecutionScope, ...]:
    """Return the immutable scope stack for the current task/thread."""
    return _CURRENT_SCOPES.get()


def capture_request_scopes() -> tuple[RequestExecutionScope, ...]:
    """Capture the active stack for explicit installation in a worker thread."""
    return current_request_scopes()


@contextmanager
def request_execution_scope(
    *,
    source: str,
    canonical_model: str | None,
    operation: RequestOperation,
    budget: RequestBudget | None,
    deadline: float | None = None,
    cancellation: Event | None = None,
    inherited_scopes: tuple[RequestExecutionScope, ...] | None = None,
    source_attempt_binding: SourceAttemptBinding | None = None,
    source_attempt_scope_version: int | None = None,
    source_attempt_scope_v3_pin: SourceAttemptScopeV3Pin | None = None,
) -> Iterator[RequestExecutionScope | None]:
    """Establish a nested scope without permitting budget replacement.

    Canonical models with no explicit budget receive a shared immutable zero
    budget. Legacy Fetchers without a canonical model and without a budget
    leave the current behavior unchanged. A source-attempt binding and V3 pin
    may be set only at the outermost scope; nested scopes inherit them or repeat
    the exact same values. Version 1 is the legacy default. Version 3 must be
    selected explicitly and requires a complete binding and pin; neither
    version can silently downgrade to the other.
    """
    current = _copy_scope_stack(_CURRENT_SCOPES.get(), inherited_scopes)
    if not isinstance(operation, RequestOperation):
        raise ValueError("operation must be an explicit RequestOperation")
    if (
        source_attempt_binding is not None
        and type(source_attempt_binding) is not SourceAttemptBinding
    ):
        raise ValueError("source_attempt_binding must be a SourceAttemptBinding or None")
    if source_attempt_scope_version is not None and (
        type(source_attempt_scope_version) is not int
        or source_attempt_scope_version not in (SOURCE_ATTEMPT_SCOPE_V1, SOURCE_ATTEMPT_SCOPE_V3)
    ):
        raise ValueError("source_attempt_scope_version must be 1, 3, or None")
    if (
        source_attempt_scope_v3_pin is not None
        and type(source_attempt_scope_v3_pin) is not SourceAttemptScopeV3Pin
    ):
        raise ValueError("source_attempt_scope_v3_pin must be a SourceAttemptScopeV3Pin or None")

    effective_source_attempt_binding = source_attempt_binding
    effective_scope_version = (
        SOURCE_ATTEMPT_SCOPE_V1
        if source_attempt_scope_version is None
        else source_attempt_scope_version
    )
    effective_scope_v3_pin = source_attempt_scope_v3_pin
    if current:
        inherited_binding = current[0].source_attempt_binding
        if any(scope.source_attempt_binding != inherited_binding for scope in current):
            raise RequestBudgetScopeError("active source-attempt bindings are inconsistent")
        if source_attempt_binding is not None and source_attempt_binding != inherited_binding:
            raise RequestBudgetScopeError(
                "nested request scope cannot add or replace its source-attempt binding"
            )
        effective_source_attempt_binding = inherited_binding
        inherited_scope_version = current[0].source_attempt_scope_version
        inherited_scope_v3_pin = current[0].source_attempt_scope_v3_pin
        if any(
            scope.source_attempt_scope_version != inherited_scope_version
            or scope.source_attempt_scope_v3_pin != inherited_scope_v3_pin
            for scope in current
        ):
            raise RequestBudgetScopeError("active source-attempt scope pins are inconsistent")
        if (
            source_attempt_scope_version is not None
            and source_attempt_scope_version != inherited_scope_version
        ):
            raise RequestBudgetScopeError("nested request scope cannot replace its gate version")
        if (
            source_attempt_scope_v3_pin is not None
            and source_attempt_scope_v3_pin != inherited_scope_v3_pin
        ):
            raise RequestBudgetScopeError("nested request scope cannot replace its V3 gate pin")
        effective_scope_version = inherited_scope_version
        effective_scope_v3_pin = inherited_scope_v3_pin

    if effective_scope_version == SOURCE_ATTEMPT_SCOPE_V1:
        if effective_scope_v3_pin is not None:
            raise RequestBudgetScopeError("V3 gate pin requires the explicit V3 scope version")
    elif effective_scope_version == SOURCE_ATTEMPT_SCOPE_V3:
        if effective_scope_v3_pin is None or effective_source_attempt_binding is None:
            raise RequestAuthorizationError("V3 gate scope requires a binding and complete pin")
        effective_scope_v3_pin.validate_integrity()
        effective_source_attempt_binding.validate_integrity()
        if (
            effective_source_attempt_binding.run_key is None
            or not effective_source_attempt_binding.expected_rights_evidence_reference
        ):
            raise RequestAuthorizationError("V3 gate scope requires a run key and exact rights ref")
    else:  # pragma: no cover - guarded above and by inherited-scope consistency
        raise RequestBudgetScopeError("unsupported source-attempt scope version")

    effective_budget = budget
    if effective_budget is None:
        if current:
            effective_budget = current[-1].budget
        elif canonical_model is not None:
            effective_budget = _ZERO_BUDGET
        elif effective_source_attempt_binding is not None:
            raise RequestAuthorizationError(
                "source-attempt binding requires a canonical request scope"
            )
        else:
            yield None
            return
    if not isinstance(effective_budget, RequestBudget):
        raise ValueError("request_budget must be a RequestBudget")
    if current and any(scope.budget is not effective_budget for scope in current):
        raise RequestBudgetScopeError("nested request budget cannot replace active budget")
    scope = RequestExecutionScope(
        source=source,
        canonical_model=canonical_model,
        operation=operation,
        budget=effective_budget,
        deadline=deadline,
        cancellation=cancellation,
        source_attempt_binding=effective_source_attempt_binding,
        source_attempt_scope_version=effective_scope_version,
        source_attempt_scope_v3_pin=effective_scope_v3_pin,
    )
    token: Token[tuple[RequestExecutionScope, ...]] = _CURRENT_SCOPES.set(current + (scope,))
    try:
        yield scope
    finally:
        _CURRENT_SCOPES.reset(token)


_ZERO_BUDGET = RequestBudget()
