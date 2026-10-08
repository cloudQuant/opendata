"""Finite request budgets and trusted-code grants for template fetches.

This module deliberately contains no provider registry, credential, or
network-discovery logic. A grant is a trusted-code declaration that must be
constructed explicitly; a request budget with no grants authorizes nothing.
"""

from __future__ import annotations

import math
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
    from collections.abc import Iterable, Iterator
    from threading import Event


MAX_TASK_ATTEMPTS = 10
MAX_SOURCE_ATTEMPTS = 3
# These are local project guardrails for explicitly bounded requests. They are
# not provider-published quotas or evidence that a source permits this volume.
MAX_BOUNDED_TASK_ATTEMPTS = 256
MAX_BOUNDED_SOURCE_ATTEMPTS = 64
_HOST_LOCK_POLL_SECONDS = 0.05


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
    """One Fetcher template's source, model, operation, budget, and liveness."""

    source: str
    canonical_model: str | None
    operation: RequestOperation
    budget: RequestBudget
    deadline: float | None = None
    cancellation: Event | None = field(default=None, repr=False, compare=False)

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
    ) -> Iterator[None]:
        """Serialize one host across clients sharing this budget."""
        if not scopes:
            yield
            return
        policy = authorize_request(scopes, source, host)
        if policy is None:  # pragma: no cover - guarded by nonempty scopes
            yield
            return
        with self._lock:
            gate = self._host_gates.setdefault(host, threading.Lock())
        while not gate.acquire(timeout=_HOST_LOCK_POLL_SECONDS):
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            authorize_request(scopes, source, host)
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
) -> Iterator[None]:
    """Acquire the shared budget's host gate when a request is scoped."""
    if not scopes:
        yield
        return
    budget = _effective_budget(scopes)
    with budget.host_slot(scopes, source, host):
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
) -> Iterator[RequestExecutionScope | None]:
    """Establish a nested scope without permitting budget replacement.

    Canonical models with no explicit budget receive a shared immutable zero
    budget. Legacy Fetchers without a canonical model and without a budget
    leave the current behavior unchanged.
    """
    current = _copy_scope_stack(_CURRENT_SCOPES.get(), inherited_scopes)
    if not isinstance(operation, RequestOperation):
        raise ValueError("operation must be an explicit RequestOperation")
    effective_budget = budget
    if effective_budget is None:
        if current:
            effective_budget = current[-1].budget
        elif canonical_model is not None:
            effective_budget = _ZERO_BUDGET
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
    )
    token: Token[tuple[RequestExecutionScope, ...]] = _CURRENT_SCOPES.set(current + (scope,))
    try:
        yield scope
    finally:
        _CURRENT_SCOPES.reset(token)


_ZERO_BUDGET = RequestBudget()
