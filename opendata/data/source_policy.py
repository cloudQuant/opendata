"""Pure validation for operator-declared source-use policies.

This module parses supplied bytes and creates finite per-request contexts. It
never loads a policy file, checks rights evidence, discovers sources, or grants
access on its own.
"""

from __future__ import annotations

import json
import keyword
import math
import unicodedata
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    MAX_BOUNDED_SOURCE_ATTEMPTS,
    MAX_BOUNDED_TASK_ATTEMPTS,
    MAX_SOURCE_ATTEMPTS,
    MAX_TASK_ATTEMPTS,
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

_MAX_DOCUMENT_BYTES = 1_048_576
_MAX_JSON_DEPTH = 64
_ERROR_MESSAGE = "invalid source policy configuration"


class SourcePolicyConfigurationError(ValueError):
    """A supplied policy document or factory input is invalid."""


def _raise_configuration_error() -> NoReturn:
    raise SourcePolicyConfigurationError(_ERROR_MESSAGE) from None


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validated_text(value: object, *, identifier: bool = False) -> str:
    if type(value) is not str or not value or not value.strip() or _has_control(value):
        raise ValueError("text must be nonempty and contain no control characters")
    if identifier and (
        value != value.strip()
        or value.casefold() == "auto"
        or not value.isidentifier()
        or keyword.iskeyword(value)
        or keyword.issoftkeyword(value)
    ):
        raise ValueError("identifier must be exact and non-wildcard")
    return value


def _freeze_strings(value: object, *, identifier: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError("expected a collection of strings")
    if isinstance(value, frozenset):
        values = tuple(sorted(value))
    elif isinstance(value, (tuple, list)):
        values = tuple(value)
    else:
        raise ValueError("expected a tuple or list of strings")
    return tuple(_validated_text(item, identifier=identifier) for item in values)


def _policy_snapshot(policy: SourceUsePolicy) -> tuple[object, ...]:
    return (
        policy.policy_id,
        policy.source,
        policy.canonical_model,
        policy.operation,
        policy.principal_user_id,
        policy.product,
        policy.purpose,
        policy.decision,
        policy.rights_evidence,
        policy.allowed_hosts,
        policy.expires_at,
        policy.conditions,
        policy.task_attempts,
        policy.source_attempts,
        policy.profile,
    )


def _build_request_grant(policy: SourceUsePolicy) -> RequestGrant:
    return RequestGrant(
        source=policy.source,
        canonical_model=policy.canonical_model,
        operation=policy.operation,
        decision=policy.decision,
        rights_evidence=policy.rights_evidence,
        task_attempts=policy.task_attempts,
        source_attempts=policy.source_attempts,
        allowed_hosts=policy.allowed_hosts,
        expires_at=policy.expires_at,
        conditions=policy.conditions,
        profile=policy.profile,
    )


class SourceUsePolicy(BaseModel):
    """One exact operator declaration; it is not proof that rights were checked."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    policy_id: str
    source: str
    canonical_model: str
    operation: RequestOperation
    principal_user_id: int = Field(gt=0, strict=True)
    product: str
    purpose: str
    decision: GrantDecision
    profile: RequestBudgetProfile = RequestBudgetProfile.LIVE_SMOKE
    rights_evidence: str = Field(repr=False)
    allowed_hosts: tuple[str, ...] = Field(repr=False)
    expires_at: datetime
    conditions: tuple[str, ...] = ()
    task_attempts: int = Field(ge=0, le=MAX_BOUNDED_TASK_ATTEMPTS, strict=True)
    source_attempts: int = Field(ge=0, le=MAX_BOUNDED_SOURCE_ATTEMPTS, strict=True)

    _integrity_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("policy_id", "product", "purpose", "rights_evidence", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: object) -> str:
        return _validated_text(value)

    @field_validator("source", "canonical_model", mode="before")
    @classmethod
    def _validate_identifiers(cls, value: object) -> str:
        return _validated_text(value, identifier=True)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _freeze_hosts(cls, value: object) -> tuple[str, ...]:
        return _freeze_strings(value)

    @field_validator("conditions", mode="before")
    @classmethod
    def _freeze_conditions(cls, value: object) -> tuple[str, ...]:
        return _freeze_strings(value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _validate_aware_datetime(cls, value: object) -> datetime:
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError("expires_at must be an aware datetime")
        try:
            if value.utcoffset() is None:
                raise ValueError("expires_at must have a UTC offset")
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("expires_at must be an aware datetime") from exc
        return value

    @field_validator("principal_user_id", mode="before")
    @classmethod
    def _validate_principal_id(cls, value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("principal_user_id must be a positive integer")
        return value

    @field_validator("task_attempts", "source_attempts", mode="before")
    @classmethod
    def _validate_exact_quotas(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("attempt quotas must be exact integers")
        return value

    @field_validator("operation", mode="before")
    @classmethod
    def _validate_exact_operation(cls, value: object) -> RequestOperation:
        if type(value) is not RequestOperation:
            raise ValueError("operation must be an exact RequestOperation")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def _validate_exact_decision(cls, value: object) -> GrantDecision:
        if type(value) is not GrantDecision:
            raise ValueError("decision must be an exact GrantDecision")
        return value

    @field_validator("profile", mode="before")
    @classmethod
    def _validate_exact_profile(cls, value: object) -> RequestBudgetProfile:
        if type(value) is not RequestBudgetProfile:
            raise ValueError("profile must be an exact RequestBudgetProfile")
        return value

    @model_validator(mode="after")
    def _validate_grant_contract_and_seal(self) -> SourceUsePolicy:
        try:
            _build_request_grant(self)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("source policy does not satisfy request grant rules") from exc
        self._integrity_seal = _policy_snapshot(self)
        return self

    def _validate_integrity(self) -> None:
        if type(self) is not SourceUsePolicy:
            raise ValueError("source policy integrity check failed")
        if type(self.profile) is not RequestBudgetProfile:
            raise ValueError("source policy integrity check failed")
        if type(self.task_attempts) is not int or type(self.source_attempts) is not int:
            raise ValueError("source policy integrity check failed")
        task_limit, source_limit = (
            (MAX_TASK_ATTEMPTS, MAX_SOURCE_ATTEMPTS)
            if self.profile is RequestBudgetProfile.LIVE_SMOKE
            else (MAX_BOUNDED_TASK_ATTEMPTS, MAX_BOUNDED_SOURCE_ATTEMPTS)
        )
        if (
            not 0 <= self.task_attempts <= task_limit
            or not 0 <= self.source_attempts <= source_limit
        ):
            raise ValueError("source policy integrity check failed")
        seal = self._integrity_seal
        if seal is None or _policy_snapshot(self) != seal:
            raise ValueError("source policy integrity check failed")
        try:
            revalidated = SourceUsePolicy.model_validate(
                dict(self.__dict__),
                strict=True,
            )
        except Exception:
            raise ValueError("source policy integrity check failed") from None
        if _policy_snapshot(revalidated) != seal:
            raise ValueError("source policy integrity check failed")
        _build_request_grant(revalidated)


class SourcePolicyDocument(BaseModel):
    """Immutable, versioned set of exact source-use declarations."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    version: Literal[1]
    policies: tuple[SourceUsePolicy, ...]

    _integrity_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("version", mode="before")
    @classmethod
    def _validate_exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("version must be integer 1")
        return value

    @field_validator("policies", mode="before")
    @classmethod
    def _freeze_policies(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("policies must be a tuple or list")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_unique_policies_and_seal(self) -> SourcePolicyDocument:
        self._validate_policy_collection()
        self._integrity_seal = self._snapshot()
        return self

    def _snapshot(self) -> tuple[object, ...]:
        policy_snapshots: list[tuple[object, ...]] = []
        for policy in self.policies:
            if type(policy) is not SourceUsePolicy:
                raise ValueError("source policy integrity check failed")
            policy_snapshots.append(_policy_snapshot(policy))
        return (self.version, tuple(policy_snapshots))

    def _validate_policy_collection(self) -> None:
        if type(self.version) is not int or self.version != 1 or type(self.policies) is not tuple:
            raise ValueError("source policy document integrity check failed")
        policy_ids: set[str] = set()
        identities: set[tuple[str, str, RequestOperation, int]] = set()
        for policy in self.policies:
            policy._validate_integrity()
            if policy.policy_id in policy_ids:
                raise ValueError("duplicate policy identifier")
            policy_ids.add(policy.policy_id)
            identity = (
                policy.source,
                policy.canonical_model,
                policy.operation,
                policy.principal_user_id,
            )
            if identity in identities:
                raise ValueError("duplicate source policy identity")
            identities.add(identity)

    def _validate_integrity(self) -> None:
        if type(self) is not SourcePolicyDocument:
            raise ValueError("source policy document integrity check failed")
        seal = self._integrity_seal
        if seal is None or self._snapshot() != seal:
            raise ValueError("source policy document integrity check failed")
        self._validate_policy_collection()
        try:
            revalidated = SourcePolicyDocument.model_validate(
                dict(self.__dict__),
                strict=True,
            )
        except Exception:
            raise ValueError("source policy document integrity check failed") from None
        if revalidated._snapshot() != seal:
            raise ValueError("source policy document integrity check failed")


def _check_json_depth(text: str) -> None:
    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            maximum = max(maximum, depth)
            if maximum > _MAX_JSON_DEPTH:
                raise ValueError("JSON nesting is too deep")
        elif character in "]}":
            depth -= 1


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _parse_canonical_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    canonical = parsed.isoformat()
    if value.endswith("Z") and canonical.endswith("+00:00"):
        canonical = canonical[:-6] + "Z"
    if parsed.tzinfo is None or parsed.utcoffset() is None or canonical != value:
        raise ValueError("datetime must be canonical ISO with an offset")
    return parsed


def _convert_json_policy_fields(raw_document: object) -> object:
    if type(raw_document) is not dict:
        return raw_document
    copied_document = dict(raw_document)
    raw_policies = copied_document.get("policies")
    if not isinstance(raw_policies, list):
        return copied_document
    converted_policies: list[object] = []
    for raw_policy in raw_policies:
        if type(raw_policy) is not dict:
            converted_policies.append(raw_policy)
            continue
        converted = dict(raw_policy)
        operation = converted.get("operation")
        if type(operation) is str:
            with suppress(ValueError):
                converted["operation"] = RequestOperation(operation)
        decision = converted.get("decision")
        if type(decision) is str:
            with suppress(ValueError):
                converted["decision"] = GrantDecision(decision)
        profile = converted.get("profile")
        if type(profile) is str:
            with suppress(ValueError):
                converted["profile"] = RequestBudgetProfile(profile)
        expires_at = converted.get("expires_at")
        if type(expires_at) is str:
            converted["expires_at"] = _parse_canonical_aware_datetime(expires_at)
        converted_policies.append(converted)
    copied_document["policies"] = converted_policies
    return copied_document


def parse_source_policy_document(body: bytes) -> SourcePolicyDocument:
    """Parse bounded UTF-8 JSON bytes without reading files or contacting services."""
    try:
        if type(body) is not bytes or len(body) > _MAX_DOCUMENT_BYTES:
            raise ValueError("invalid document bytes")
        text = body.decode("utf-8", errors="strict")
        _check_json_depth(text)
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
        converted = _convert_json_policy_fields(parsed)
        return SourcePolicyDocument.model_validate(converted, strict=True)
    except Exception:
        _raise_configuration_error()


def _validate_factory_identity(
    source: object,
    canonical_model: object,
    operation: object,
    principal_user_id: object,
) -> tuple[str, str, RequestOperation, int]:
    try:
        exact_source = _validated_text(source, identifier=True)
        exact_model = _validated_text(canonical_model, identifier=True)
        if type(operation) is not RequestOperation:
            raise ValueError("operation must be an exact RequestOperation")
        if type(principal_user_id) is not int or principal_user_id <= 0:
            raise ValueError("principal_user_id must be a positive integer")
        return exact_source, exact_model, operation, principal_user_id
    except (TypeError, ValueError):
        _raise_configuration_error()


def _validate_factory_timeout(timeout: object) -> int | float:
    if type(timeout) is int:
        exact_timeout: int | float = timeout
    elif type(timeout) is float:
        exact_timeout = timeout
    else:
        _raise_configuration_error()
    try:
        is_valid = math.isfinite(float(exact_timeout)) and exact_timeout > 0
    except (OverflowError, TypeError, ValueError):
        is_valid = False
    if not is_valid:
        _raise_configuration_error()
    return exact_timeout


def build_source_policy_context(
    document: SourcePolicyDocument,
    *,
    source: str,
    canonical_model: str,
    operation: RequestOperation,
    principal_user_id: int,
    timeout: float = 30.0,
) -> FetchContext:
    """Build one fresh finite budget from the exact matching declaration."""
    exact_timeout = _validate_factory_timeout(timeout)
    try:
        if type(document) is not SourcePolicyDocument:
            raise ValueError("source policy document type is invalid")
        document._validate_integrity()
    except Exception:
        _raise_configuration_error()

    exact_source, exact_model, exact_operation, exact_principal = _validate_factory_identity(
        source,
        canonical_model,
        operation,
        principal_user_id,
    )
    matching = [
        policy
        for policy in document.policies
        if policy.source == exact_source
        and policy.canonical_model == exact_model
        and policy.operation is exact_operation
        and policy.principal_user_id == exact_principal
    ]
    if len(matching) > 1:
        _raise_configuration_error()
    if not matching:
        return FetchContext(
            timeout=exact_timeout,
            operation=exact_operation,
            request_budget=RequestBudget(),
        )

    policy = matching[0]
    try:
        grant = _build_request_grant(policy)
        budget = RequestBudget(
            task_attempts=policy.task_attempts,
            source_attempts=policy.source_attempts,
            grants=(grant,),
            profile=policy.profile,
        )
        return FetchContext(
            timeout=exact_timeout,
            operation=exact_operation,
            request_budget=budget,
        )
    except Exception:
        _raise_configuration_error()
