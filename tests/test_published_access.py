"""Behavior tests for the v2 published-data authorization boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.api.dependencies import Principal
from opendata.api.published_access import (
    DatasetGrant,
    DatasetGrantRegistry,
    GrantConfigurationError,
    PublishedAccessError,
    list_permitted_datasets,
    load_dataset_grant_registry,
    require_published_access,
)
from opendata.models.user import User, UserRole

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def make_user(user_id: int = 17, role: UserRole = UserRole.USER) -> User:
    """Build an authenticated-looking ORM user for pure access checks."""
    return User(
        id=user_id,
        username=f"user-{user_id}",
        email=f"user-{user_id}@example.test",
        hashed_password="not-a-real-password-hash",
        role=role,
        is_active=True,
    )


def make_grant(**updates: object) -> DatasetGrant:
    """Return one valid reviewed grant, with explicit test overrides."""
    values: dict[str, object] = {
        "dataset_id": "cn.equity.daily",
        "domain": "equity",
        "source_id": "provider-a",
        "permitted_operations": ["data.read", "snapshot.create", "snapshot.read"],
        "purposes": ["research"],
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "review_evidence": "OD-204-REVIEW-17",
    }
    values.update(updates)
    return DatasetGrant.model_validate(values)


def make_key(
    scopes: tuple[str, ...],
    *,
    user_id: int = 17,
    api_key_id: int = 31,
    role: UserRole = UserRole.USER,
) -> Principal:
    """Build a principal authenticated by one API key."""
    return Principal(user=make_user(user_id, role), api_key_id=api_key_id, scopes=scopes)


def authorize(
    principal: Principal,
    *,
    dataset_id: str = "cn.equity.daily",
    operation: str = "data.read",
    registry: DatasetGrantRegistry | None = None,
    purpose: str = "research",
    now: datetime = NOW,
) -> object:
    """Run one access decision against a one-grant default registry."""
    selected_registry = registry or DatasetGrantRegistry([make_grant()])
    return require_published_access(
        principal,
        dataset_id,
        operation,
        selected_registry,
        now=now,
        purpose=purpose,
    )


def test_api_key_needs_operation_and_domain_scopes_independently() -> None:
    """Legacy domain access remains valid, but does not grant new operations."""
    with pytest.raises(PublishedAccessError):
        authorize(make_key(("equity",)))

    with pytest.raises(PublishedAccessError):
        authorize(make_key(("*",)))

    context = authorize(make_key(("*", "data.read")))
    assert context.domain == "equity"
    assert context.access_scope == ("*", "data.read")


def test_api_key_read_permission_does_not_grant_snapshot_creation() -> None:
    """The operation scope is checked independently for every action."""
    principal = make_key(("equity", "data.read", "snapshot.read"))
    assert authorize(principal).operation == "data.read"
    with pytest.raises(PublishedAccessError):
        authorize(principal, operation="snapshot.create")


def test_key_domain_permission_must_match_the_reviewed_dataset_domain() -> None:
    """An operation scope cannot bypass the legacy domain boundary."""
    with pytest.raises(PublishedAccessError):
        authorize(make_key(("bonds", "data.read")))


def test_jwt_user_can_read_and_create_snapshots_but_cannot_manage_jobs() -> None:
    """Ordinary JWT users get the bounded published-data operation set."""
    principal = Principal(user=make_user())
    registry = DatasetGrantRegistry(
        [make_grant(permitted_operations=["data.read", "job.manage", "snapshot.create"])]
    )
    assert authorize(principal, registry=registry).operation == "data.read"
    assert authorize(principal, operation="snapshot.create", registry=registry).operation == (
        "snapshot.create"
    )
    with pytest.raises(PublishedAccessError):
        authorize(principal, operation="job.manage", registry=registry)


def test_job_manage_requires_admin_even_with_an_api_key_wildcard() -> None:
    """The domain wildcard does not imply the privileged operation scope."""
    registry = DatasetGrantRegistry([make_grant(permitted_operations=["job.manage"])])
    with pytest.raises(PublishedAccessError):
        authorize(make_key(("*",), role=UserRole.ADMIN), operation="job.manage", registry=registry)
    assert (
        authorize(
            make_key(("*", "job.manage"), role=UserRole.ADMIN),
            operation="job.manage",
            registry=registry,
        ).operation
        == "job.manage"
    )
    with pytest.raises(PublishedAccessError):
        authorize(make_key(("*", "job.manage")), operation="job.manage", registry=registry)


def test_grant_window_is_start_inclusive_and_end_exclusive() -> None:
    """The grant is valid at valid_from and rejected at valid_until."""
    grant = make_grant(valid_from=NOW, valid_until=NOW + timedelta(hours=1))
    registry = DatasetGrantRegistry([grant])
    principal = Principal(user=make_user())
    assert authorize(principal, registry=registry, now=NOW).dataset_id == grant.dataset_id
    with pytest.raises(PublishedAccessError):
        authorize(
            principal,
            registry=registry,
            now=NOW + timedelta(hours=1),
        )


def test_subject_and_purpose_constraints_are_exact_and_sealed() -> None:
    """A caller cannot broaden a subject list or substitute another purpose."""
    grant = make_grant(subject_ids=[17], purposes=["research", "backtest"])
    registry = DatasetGrantRegistry([grant])
    assert authorize(Principal(user=make_user()), registry=registry).purpose == "research"
    assert (
        authorize(Principal(user=make_user()), registry=registry, purpose="backtest").purpose
        == "backtest"
    )
    with pytest.raises(PublishedAccessError):
        authorize(Principal(user=make_user(18)), registry=registry)
    with pytest.raises(PublishedAccessError):
        authorize(Principal(user=make_user()), registry=registry, purpose="trading")


def test_missing_and_unauthorized_datasets_have_the_same_public_error() -> None:
    """A caller cannot distinguish an absent grant from a restricted dataset."""
    principal = make_key(("equity", "data.read"))
    with pytest.raises(PublishedAccessError) as restricted:
        authorize(principal, operation="snapshot.create")
    with pytest.raises(PublishedAccessError) as missing:
        authorize(principal, dataset_id="unknown.dataset")
    assert restricted.value.status_code == missing.value.status_code == 403
    assert str(restricted.value) == str(missing.value) == "Dataset is unavailable"


def test_binding_changes_when_user_key_scope_or_reviewed_grant_changes() -> None:
    """Cursor identity is bound to the principal, full scope set, and grant digest."""
    base_scopes = ("equity", "data.read", "snapshot.read")
    base = authorize(make_key(base_scopes))
    reordered = authorize(make_key(("snapshot.read", "equity", "data.read")))
    assert base.binding_sha256 == reordered.binding_sha256

    changed_user = authorize(make_key(base_scopes, user_id=18))
    changed_key = authorize(make_key(base_scopes, api_key_id=32))
    changed_scopes = authorize(make_key(("equity", "data.read")))
    changed_grant = authorize(
        make_key(base_scopes),
        registry=DatasetGrantRegistry([make_grant(purposes=["research", "backtest"])]),
    )
    assert (
        len(
            {
                base.binding_sha256,
                changed_user.binding_sha256,
                changed_key.binding_sha256,
                changed_scopes.binding_sha256,
                changed_grant.binding_sha256,
            }
        )
        == 5
    )
    assert base.grant_sha256 != changed_grant.grant_sha256
    with pytest.raises(PublishedAccessError):
        authorize(make_key(("equity", "data.read")), operation="snapshot.read")


def test_jwt_binding_is_also_stable_and_bound_to_user_and_grant() -> None:
    """JWT access contexts do not collapse to a shared anonymous cursor identity."""
    first = authorize(Principal(user=make_user()))
    other_user = authorize(Principal(user=make_user(18)))
    assert first.access_scope is None
    assert first.binding_sha256 != other_user.binding_sha256


def test_binding_can_be_reused_between_snapshot_and_record_read_operations() -> None:
    """A fixed snapshot can be read across route operations under one access scope."""
    principal = make_key(("equity", "data.read", "snapshot.create", "snapshot.read"))
    grant = make_grant()
    registry = DatasetGrantRegistry([grant])
    created = authorize(principal, operation="snapshot.create", registry=registry)
    records = authorize(principal, operation="data.read", registry=registry)
    snapshot = authorize(principal, operation="snapshot.read", registry=registry)
    assert created.binding_sha256 == records.binding_sha256 == snapshot.binding_sha256


def test_catalog_only_returns_datasets_permitted_for_this_key() -> None:
    """Catalog filtering reveals authorized grants only."""
    other = make_grant(
        dataset_id="cn.bonds.daily",
        domain="bonds",
        source_id="provider-b",
        review_evidence="OD-204-REVIEW-18",
    )
    registry = DatasetGrantRegistry([make_grant(), other])
    contexts = list_permitted_datasets(make_key(("equity", "data.read")), registry, now=NOW)
    assert tuple(context.dataset_id for context in contexts) == ("cn.equity.daily",)


def test_registry_is_immutable_and_rejects_duplicate_dataset_ids() -> None:
    """Registry consumers cannot mutate review state and duplicate ids fail closed."""
    registry = DatasetGrantRegistry([make_grant()])
    with pytest.raises(TypeError):
        registry._grants["cn.other.daily"] = make_grant(dataset_id="cn.other.daily")
    with pytest.raises(GrantConfigurationError):
        DatasetGrantRegistry([make_grant(), make_grant()])


def test_invalid_grant_shapes_fail_closed() -> None:
    """Wildcard grants, missing evidence, and naive time bounds are rejected."""
    with pytest.raises(ValidationError):
        make_grant(domain="*")
    with pytest.raises(ValidationError):
        make_grant(permitted_operations=["*"])
    with pytest.raises(ValidationError):
        make_grant(purposes=["*"])
    with pytest.raises(ValidationError):
        make_grant(review_evidence="")
    with pytest.raises(ValidationError):
        make_grant(valid_from=datetime(2026, 10, 25))
    with pytest.raises(ValidationError):
        make_grant(valid_until=NOW - timedelta(days=1))
    with pytest.raises(ValidationError):
        make_grant(subject_ids=[0])


def test_config_loader_rejects_duplicate_unknown_secret_and_oversize_content(
    tmp_path: Path,
) -> None:
    """The explicit loader accepts valid data and rejects unsafe or ambiguous JSON."""
    config_path = tmp_path / "dataset-grants.json"
    valid = {
        "datasets": [
            {
                "dataset_id": "cn.equity.daily",
                "domain": "equity",
                "source_id": "provider-a",
                "permitted_operations": ["data.read"],
                "purposes": ["research"],
                "valid_from": (NOW - timedelta(days=1)).isoformat(),
                "valid_until": (NOW + timedelta(days=1)).isoformat(),
                "review_evidence": "OD-204-REVIEW-17",
            }
        ]
    }
    config_path.write_text(json.dumps(valid), encoding="utf-8")
    assert load_dataset_grant_registry(config_path).get("cn.equity.daily") is not None

    config_path.write_text('{"datasets":[],"datasets":[]}', encoding="utf-8")
    with pytest.raises(GrantConfigurationError):
        load_dataset_grant_registry(config_path)

    secret_field = {"datasets": [{**valid["datasets"][0], "api_key": "secret-value"}]}
    config_path.write_text(json.dumps(secret_field), encoding="utf-8")
    with pytest.raises(GrantConfigurationError):
        load_dataset_grant_registry(config_path)

    unsafe_reference = {"datasets": [{**valid["datasets"][0], "review_evidence": "sk-secret"}]}
    config_path.write_text(json.dumps(unsafe_reference), encoding="utf-8")
    with pytest.raises(GrantConfigurationError):
        load_dataset_grant_registry(config_path)

    config_path.write_bytes(b" " * (1_048_577))
    with pytest.raises(GrantConfigurationError):
        load_dataset_grant_registry(config_path)


def test_access_rejects_naive_clock_and_malformed_principal() -> None:
    """A malformed clock or caller object cannot fail open."""
    with pytest.raises(PublishedAccessError):
        require_published_access(
            Principal(user=make_user()),
            "cn.equity.daily",
            "data.read",
            DatasetGrantRegistry([make_grant()]),
            now=datetime(2026, 10, 26),
        )
    with pytest.raises(PublishedAccessError):
        require_published_access(
            object(),
            "cn.equity.daily",
            "data.read",
            DatasetGrantRegistry([make_grant()]),
            now=NOW,
        )  # type: ignore[arg-type]
