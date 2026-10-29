"""Pure functions for a non-executable Fuyao deployment identity declaration.

The functions in this module perform no settings, credential, filesystem,
database, or network access. Importing it by its normal package path currently
runs the pre-existing ``opendata.pipeline`` initializer, which imports the
runner and ORM model; ``opendata.core.database`` then loads application
settings and constructs SQLAlchemy engines. The contract records only a
closed set of caller-supplied, non-secret deployment declarations. Its
fingerprint does not bind a request, run, capture intent, entity, authorization,
or data completeness. Provenance labels are caller assertions: this module
does not verify that an upstream loader, policy parser, gate, or operator did
the work named by a label. The local source-grant record remains an operator
self-declaration and is not independent evidence of supplier rights.

Known credential-marker strings are rejected in identifier fields by a limited
denylist. The denylist cannot identify every possible credential or detect an
arbitrary credential disguised as an otherwise valid identifier. A trusted
caller must supply non-secret values for every identity field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping

_SNAPSHOT_VERSION = 1
_TASK_VERSION = "fuyao_financial_observation_one_shot.v1"
_FINGERPRINT_DOMAIN = b"opendata.fuyao-one-shot-contract.v1\0"
_MAX_CANONICAL_BYTES = 16 * 1024
_MAX_REVISION = 2_147_483_647
# These fixed values match the existing THS provider source, financial
# observation fetcher model/QUERY scope, and its default Fuyao HTTPS origin.
# Keep this non-executable module import-independent; bump/version this
# declaration and its tests if those reviewed transport constants change.
_FIXED_PROVIDER_ID = "ths"
_FIXED_TRANSPORT_ID = "fuyao"
_FIXED_MODEL_ID = "financial_statement"
_FIXED_HOST = "fuyao.aicubes.cn"
_FIXED_OPERATION = "query"
_FIXED_TRANSPORT_MAX_ATTEMPTS = 1
_FIXED_BUDGET_BINDING_VERSION = 2
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_CREDENTIAL_MARKER_DENYLIST = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\S+|"
    r"\bbearer\s+\S+)"
)

_BUNDLE_VALIDATION_VALUES = frozenset({"loader_validated", "self_declared"})
_POLICY_VALIDATION_VALUES = frozenset({"parser_validated", "self_declared"})
_BUDGET_BINDING_VALIDATION_VALUES = frozenset({"gate_validated", "self_declared"})
_BUDGET_POLICY_VALIDATION_VALUES = frozenset({"loader_validated", "self_declared"})
_REQUIRED_INPUT_FIELDS = frozenset(
    {
        "provider_id",
        "transport_id",
        "canonical_model",
        "host",
        "operation",
        "transport_max_attempts",
        "financial_bundle_sha256",
        "financial_bundle_validation",
        "source_policy_id",
        "source_policy_revision",
        "source_policy_sha256",
        "source_policy_validation",
        "grant_id",
        "grant_revision",
        "grant_principal",
        "grant_validation",
        "budget_binding_version",
        "budget_binding_sha256",
        "budget_quota_key",
        "budget_policy_revision",
        "budget_policy_sha256",
        "budget_binding_validation",
        "budget_policy_validation",
        "credential_revision",
        "credential_revision_validation",
    }
)


def _invalid() -> NoReturn:
    """Raise one content-free error for every invalid contract input."""
    raise ValueError("fuyao_one_shot_contract_invalid") from None


def _is_exact_int(value: object, *, minimum: int = 1) -> bool:
    """Return whether a value is an exact bounded positive revision integer."""
    return type(value) is int and minimum <= value <= _MAX_REVISION


def _valid_identifier(value: object) -> bool:
    """Check a bounded ASCII identifier without echoing it into diagnostics."""
    return (
        type(value) is str
        and _IDENTIFIER_PATTERN.fullmatch(value) is not None
        and _KNOWN_CREDENTIAL_MARKER_DENYLIST.search(value) is None
        and value.casefold() != "auto"
    )


def _valid_sha256(value: object) -> bool:
    """Check the exact lowercase textual representation of a SHA-256 digest."""
    return type(value) is str and _SHA256_PATTERN.fullmatch(value) is not None


def _require_identifier(value: object) -> str:
    """Return one valid exact identifier or raise the content-free contract error."""
    if type(value) is not str:
        _invalid()
    if (
        _IDENTIFIER_PATTERN.fullmatch(value) is None
        or _KNOWN_CREDENTIAL_MARKER_DENYLIST.search(value) is not None
        or value.casefold() == "auto"
    ):
        _invalid()
    return value


def _require_sha256(value: object) -> str:
    """Return one valid exact digest or raise the content-free contract error."""
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        _invalid()
    return value


def _require_revision(value: object) -> int:
    """Return one bounded exact integer revision or reject bools and coercions."""
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        _invalid()
    return value


def _valid_choice(value: object, choices: frozenset[str]) -> bool:
    """Check membership only for exact strings, avoiding coercion or unhashable input."""
    return type(value) is str and value in choices


def _require_bundle_validation(
    value: object,
) -> Literal["loader_validated", "self_declared"]:
    """Return one of the explicit bundle pin provenance labels."""
    if type(value) is str and value == "loader_validated":
        return "loader_validated"
    if type(value) is str and value == "self_declared":
        return "self_declared"
    _invalid()


def _require_policy_validation(value: object) -> Literal["parser_validated", "self_declared"]:
    """Return one of the explicit source policy provenance labels."""
    if type(value) is str and value == "parser_validated":
        return "parser_validated"
    if type(value) is str and value == "self_declared":
        return "self_declared"
    _invalid()


def _require_grant_validation(value: object) -> Literal["local_self_declaration"]:
    """Record that the local source grant is only an operator self-declaration."""
    if type(value) is str and value == "local_self_declaration":
        return "local_self_declaration"
    _invalid()


def _require_budget_binding_validation(
    value: object,
) -> Literal["gate_validated", "self_declared"]:
    """Return one of the explicit budget binding provenance labels."""
    if type(value) is str and value == "gate_validated":
        return "gate_validated"
    if type(value) is str and value == "self_declared":
        return "self_declared"
    _invalid()


def _require_budget_policy_validation(
    value: object,
) -> Literal["loader_validated", "self_declared"]:
    """Return one of the explicit budget policy provenance labels."""
    if type(value) is str and value == "loader_validated":
        return "loader_validated"
    if type(value) is str and value == "self_declared":
        return "self_declared"
    _invalid()


def _require_credential_revision_validation(
    value: object,
) -> Literal["operator_declared"]:
    """Return the only permitted provenance for a non-secret key revision number."""
    if type(value) is str and value == "operator_declared":
        return "operator_declared"
    _invalid()


def _canonical_json(snapshot: FuyaoOneShotSnapshot) -> str:
    """Serialize every snapshot field with stable, versioned JSON encoding."""
    return json.dumps(
        asdict(snapshot),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True, repr=False)
class FuyaoOneShotSnapshot:
    """Frozen declaration of a single Fuyao deployment identity.

    ``*_validation`` fields preserve caller-reported provenance and are included
    in the fingerprint. The labels do not prove that validation occurred. This
    deployment snapshot is not a request, run, capture-intent, entity, rights,
    authorization, or completeness record. The local grant label is fixed to
    an operator self-declaration.
    """

    financial_bundle_sha256: str
    financial_bundle_validation: Literal["loader_validated", "self_declared"]
    source_policy_id: str
    source_policy_revision: str
    source_policy_sha256: str
    source_policy_validation: Literal["parser_validated", "self_declared"]
    grant_id: str
    grant_revision: int
    grant_principal: str
    grant_validation: Literal["local_self_declaration"]
    budget_binding_sha256: str
    budget_quota_key: str
    budget_policy_revision: int
    budget_policy_sha256: str
    budget_binding_validation: Literal["gate_validated", "self_declared"]
    budget_policy_validation: Literal["loader_validated", "self_declared"]
    credential_revision: int
    credential_revision_validation: Literal["operator_declared"]
    provider_id: str = field(default=_FIXED_PROVIDER_ID, init=False)
    transport_id: str = field(default=_FIXED_TRANSPORT_ID, init=False)
    canonical_model: str = field(default=_FIXED_MODEL_ID, init=False)
    host: str = field(default=_FIXED_HOST, init=False)
    operation: str = field(default=_FIXED_OPERATION, init=False)
    transport_max_attempts: int = field(default=_FIXED_TRANSPORT_MAX_ATTEMPTS, init=False)
    budget_binding_version: int = field(default=_FIXED_BUDGET_BINDING_VERSION, init=False)
    schema_version: int = field(default=_SNAPSHOT_VERSION, init=False)
    task_version: str = field(default=_TASK_VERSION, init=False)

    def __post_init__(self) -> None:
        """Reject directly constructed snapshots that violate this closed contract."""
        if not _valid_sha256(self.financial_bundle_sha256):
            _invalid()
        if not _valid_choice(self.financial_bundle_validation, _BUNDLE_VALIDATION_VALUES):
            _invalid()
        if not _valid_identifier(self.source_policy_id):
            _invalid()
        if not _valid_identifier(self.source_policy_revision):
            _invalid()
        if not _valid_sha256(self.source_policy_sha256):
            _invalid()
        if not _valid_choice(self.source_policy_validation, _POLICY_VALIDATION_VALUES):
            _invalid()
        if not _valid_identifier(self.grant_id):
            _invalid()
        if not _is_exact_int(self.grant_revision):
            _invalid()
        if not _valid_identifier(self.grant_principal):
            _invalid()
        if self.grant_validation != "local_self_declaration":
            _invalid()
        if not _valid_sha256(self.budget_binding_sha256):
            _invalid()
        if not _valid_identifier(self.budget_quota_key):
            _invalid()
        if not _is_exact_int(self.budget_policy_revision):
            _invalid()
        if not _valid_sha256(self.budget_policy_sha256):
            _invalid()
        if not _valid_choice(self.budget_binding_validation, _BUDGET_BINDING_VALIDATION_VALUES):
            _invalid()
        if not _valid_choice(self.budget_policy_validation, _BUDGET_POLICY_VALIDATION_VALUES):
            _invalid()
        if not _is_exact_int(self.credential_revision):
            _invalid()
        if self.credential_revision_validation != "operator_declared":
            _invalid()

        for field_name, expected in (
            ("provider_id", _FIXED_PROVIDER_ID),
            ("transport_id", _FIXED_TRANSPORT_ID),
            ("canonical_model", _FIXED_MODEL_ID),
            ("host", _FIXED_HOST),
            ("operation", _FIXED_OPERATION),
            ("schema_version", _SNAPSHOT_VERSION),
            ("task_version", _TASK_VERSION),
        ):
            if getattr(self, field_name) != expected:
                _invalid()
        if (
            type(self.transport_max_attempts) is not int
            or self.transport_max_attempts != _FIXED_TRANSPORT_MAX_ATTEMPTS
        ):
            _invalid()
        if (
            type(self.budget_binding_version) is not int
            or self.budget_binding_version != _FIXED_BUDGET_BINDING_VERSION
        ):
            _invalid()
        try:
            encoded = _canonical_json(self).encode("ascii", errors="strict")
        except (TypeError, ValueError, UnicodeError):
            _invalid()
        if not 1 <= len(encoded) <= _MAX_CANONICAL_BYTES:
            _invalid()

    def __repr__(self) -> str:
        """Keep deployment identifiers out of default repr-based logs."""
        return (
            "FuyaoOneShotSnapshot(schema_version="
            f"{self.schema_version}, task_version={self.task_version!r}, "
            "identity=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FuyaoOneShotContract:
    """Immutable deployment declaration and fingerprint, without execution rights.

    The fingerprint commits only to the snapshot's declared deployment fields.
    It does not prove their source, bind per-run inputs, authorize a fetch, or
    establish that fetched data is complete.
    """

    snapshot: FuyaoOneShotSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        """Require the exact validated frozen snapshot type."""
        if type(self.snapshot) is not FuyaoOneShotSnapshot:
            _invalid()

    @property
    def canonical_json(self) -> str:
        """Return the complete stable JSON representation of the snapshot."""
        return _canonical_json(self.snapshot)

    @property
    def fingerprint_sha256(self) -> str:
        """Return the domain-separated SHA-256 digest of the non-secret snapshot."""
        payload = _FINGERPRINT_DOMAIN + self.canonical_json.encode("ascii", errors="strict")
        return hashlib.sha256(payload).hexdigest()

    def __repr__(self) -> str:
        """Show only version metadata and the non-secret fingerprint."""
        return (
            "FuyaoOneShotContract(task_version="
            f"{self.snapshot.task_version!r}, fingerprint_sha256={self.fingerprint_sha256!r})"
        )


def build_fuyao_one_shot_contract(values: Mapping[str, object]) -> FuyaoOneShotContract:
    """Build a closed-set contract from synthetic or already validated declarations.

    The mapping accepts no credential fields and rejects every undeclared field,
    including conventional secret names. Known credential-marker strings are
    rejected in identifier fields, but this finite denylist cannot identify
    every possible credential; a trusted caller must supply non-secret values.
    The fingerprint binds only caller-supplied deployment declarations. It does
    not bind a request, run, capture intent, entity, authorization, or data
    completeness. The only hash produced is over the canonical snapshot;
    supplied digest fields are format-checked and no secret is hashed.

    Args:
        values: Exact mapping with all required identity fields and no extras.

    Returns:
        A frozen, non-executable contract with task version and fingerprint.

    Raises:
        ValueError: If any field is missing, extra, malformed, or out of scope.
    """
    if type(values) is not dict or set(values) != _REQUIRED_INPUT_FIELDS:
        _invalid()
    if (
        type(values["provider_id"]) is not str
        or values["provider_id"] != _FIXED_PROVIDER_ID
        or type(values["transport_id"]) is not str
        or values["transport_id"] != _FIXED_TRANSPORT_ID
        or type(values["canonical_model"]) is not str
        or values["canonical_model"] != _FIXED_MODEL_ID
        or type(values["host"]) is not str
        or values["host"] != _FIXED_HOST
        or type(values["operation"]) is not str
        or values["operation"] != _FIXED_OPERATION
        or type(values["transport_max_attempts"]) is not int
        or values["transport_max_attempts"] != _FIXED_TRANSPORT_MAX_ATTEMPTS
        or type(values["budget_binding_version"]) is not int
        or values["budget_binding_version"] != _FIXED_BUDGET_BINDING_VERSION
    ):
        _invalid()
    try:
        snapshot = FuyaoOneShotSnapshot(
            financial_bundle_sha256=_require_sha256(values["financial_bundle_sha256"]),
            financial_bundle_validation=_require_bundle_validation(
                values["financial_bundle_validation"]
            ),
            source_policy_id=_require_identifier(values["source_policy_id"]),
            source_policy_revision=_require_identifier(values["source_policy_revision"]),
            source_policy_sha256=_require_sha256(values["source_policy_sha256"]),
            source_policy_validation=_require_policy_validation(values["source_policy_validation"]),
            grant_id=_require_identifier(values["grant_id"]),
            grant_revision=_require_revision(values["grant_revision"]),
            grant_principal=_require_identifier(values["grant_principal"]),
            grant_validation=_require_grant_validation(values["grant_validation"]),
            budget_binding_sha256=_require_sha256(values["budget_binding_sha256"]),
            budget_quota_key=_require_identifier(values["budget_quota_key"]),
            budget_policy_revision=_require_revision(values["budget_policy_revision"]),
            budget_policy_sha256=_require_sha256(values["budget_policy_sha256"]),
            budget_binding_validation=_require_budget_binding_validation(
                values["budget_binding_validation"]
            ),
            budget_policy_validation=_require_budget_policy_validation(
                values["budget_policy_validation"]
            ),
            credential_revision=_require_revision(values["credential_revision"]),
            credential_revision_validation=_require_credential_revision_validation(
                values["credential_revision_validation"]
            ),
        )
    except (TypeError, ValueError):
        _invalid()
    return FuyaoOneShotContract(snapshot=snapshot)


__all__ = [
    "FuyaoOneShotContract",
    "FuyaoOneShotSnapshot",
    "build_fuyao_one_shot_contract",
]
