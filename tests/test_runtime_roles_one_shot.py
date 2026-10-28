"""Synthetic safety tests for dedicated reviewed one-shot process roles."""

from __future__ import annotations

import pytest

from opendata.pipeline.runtime_roles import (
    RuntimeRolePlan,
    build_runtime_role_plan,
)


@pytest.mark.parametrize("role", ["one_shot_submitter", "one_shot_executor"])
def test_one_shot_roles_require_reviewed_configuration_and_sinks(role: str) -> None:
    """Allow each one-shot role only with an explicit reviewed sink boundary."""
    plan = build_runtime_role_plan(
        profile="reviewed",
        roles={role},  # type: ignore[arg-type]
        reviewed_config_id="test.one-shot",
        reviewed_sink_ids={"market.daily"},
    )

    assert plan.profile == "reviewed"
    assert plan.roles == frozenset({role})
    assert plan.reviewed_config_id == "test.one-shot"
    assert plan.reviewed_sink_ids == frozenset({"market.daily"})
    assert plan.enables(role)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("roles", "profile"),
    [
        ({"one_shot_submitter", "api"}, "reviewed"),
        ({"one_shot_submitter", "scheduler"}, "reviewed"),
        ({"one_shot_executor", "worker"}, "reviewed"),
        ({"one_shot_submitter", "one_shot_executor"}, "reviewed"),
        ({"one_shot_submitter", "legacy"}, "reviewed"),
        ({"one_shot_executor"}, "legacy_compatible"),
    ],
)
def test_one_shot_roles_cannot_share_a_process_plan(roles: set[str], profile: str) -> None:
    """Reject role combinations that could mix one-shot and persistent work."""
    with pytest.raises(ValueError):
        build_runtime_role_plan(
            profile=profile,  # type: ignore[arg-type]
            roles=roles,  # type: ignore[arg-type]
            reviewed_config_id="test.one-shot",
            reviewed_sink_ids={"market.daily"},
        )


@pytest.mark.parametrize("role", ["one_shot_submitter", "one_shot_executor"])
def test_one_shot_roles_reject_missing_or_invalid_reviewed_config(role: str) -> None:
    """Fail closed when one-shot plans lack a valid reviewed config id."""
    for config_id in (None, "bad config", 7):
        with pytest.raises(ValueError, match="explicit reviewed config id"):
            build_runtime_role_plan(
                profile="reviewed",
                roles={role},  # type: ignore[arg-type]
                reviewed_config_id=config_id,  # type: ignore[arg-type]
                reviewed_sink_ids={"market.daily"},
            )


@pytest.mark.parametrize("role", ["one_shot_submitter", "one_shot_executor"])
def test_one_shot_roles_reject_missing_or_invalid_sink_scope(role: str) -> None:
    """Require at least one syntactically valid sink for both one-shot roles."""
    for sinks in (set(), {"bad sink"}):
        with pytest.raises(ValueError, match="sink"):
            build_runtime_role_plan(
                profile="reviewed",
                roles={role},  # type: ignore[arg-type]
                reviewed_config_id="test.one-shot",
                reviewed_sink_ids=sinks,
            )


def test_existing_reviewed_and_legacy_role_plans_remain_compatible() -> None:
    """Keep the established API, scheduler, worker, and legacy plans intact."""
    default_plan = build_runtime_role_plan()
    api_plan = build_runtime_role_plan(
        profile="reviewed",
        roles={"api"},
        reviewed_config_id="test.api",
    )
    writer_plan = build_runtime_role_plan(
        profile="reviewed",
        roles={"api", "scheduler", "worker"},
        reviewed_config_id="test.writer",
        reviewed_sink_ids={"market.daily"},
    )

    assert default_plan.roles == frozenset({"api", "legacy"})
    assert api_plan.roles == frozenset({"api"})
    assert writer_plan.enables("scheduler")
    assert writer_plan.enables("worker")
    assert not writer_plan.enables("one_shot_submitter")


def test_existing_reviewed_writer_roles_still_require_sink_scope() -> None:
    """Preserve the existing sink guard for scheduler and worker roles."""
    for role in ("scheduler", "worker"):
        with pytest.raises(ValueError, match="declared sink ids"):
            build_runtime_role_plan(
                profile="reviewed",
                roles={role},  # type: ignore[arg-type]
                reviewed_config_id="test.writer",
            )


def test_runtime_role_plan_rejects_malformed_collections_and_values() -> None:
    """Keep direct immutable-plan construction guarded against malformed input."""
    with pytest.raises(ValueError, match="profile is unsupported"):
        RuntimeRolePlan("unknown", frozenset({"api"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty frozen set"):
        RuntimeRolePlan("reviewed", frozenset())
    with pytest.raises(ValueError, match="unsupported value"):
        RuntimeRolePlan("reviewed", frozenset({"unknown"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="frozen set"):
        RuntimeRolePlan("reviewed", frozenset({"api"}), reviewed_sink_ids=set())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sink id is invalid"):
        RuntimeRolePlan("reviewed", frozenset({"api"}), reviewed_sink_ids=frozenset({"bad sink"}))
    with pytest.raises(ValueError, match="cannot select reviewed configuration"):
        RuntimeRolePlan(
            "legacy_compatible",
            frozenset({"api"}),
            reviewed_config_id="test.api",
        )
    with pytest.raises(ValueError, match="cannot select reviewed configuration"):
        RuntimeRolePlan(
            "legacy_compatible",
            frozenset({"api"}),
            reviewed_sink_ids=frozenset({"market.daily"}),
        )


def test_builder_requires_explicit_roles_for_reviewed_profile_and_rejects_unknown_role() -> None:
    """Reject implicit reviewed startup and arbitrary process role values."""
    with pytest.raises(ValueError, match="explicit role selection"):
        build_runtime_role_plan(profile="reviewed")
    with pytest.raises(ValueError, match="unsupported value"):
        build_runtime_role_plan(
            profile="reviewed",
            roles={"unknown"},  # type: ignore[arg-type]
            reviewed_config_id="test.api",
        )
    with pytest.raises(TypeError):
        build_runtime_role_plan(roles=7)  # type: ignore[arg-type]


@pytest.mark.parametrize("roles", ["api", b"api"])
def test_builder_rejects_string_role_collections(roles: str | bytes) -> None:
    """Reject text values instead of splitting them into role characters."""
    with pytest.raises(TypeError, match="roles must be an iterable"):
        build_runtime_role_plan(roles=roles)  # type: ignore[arg-type]


@pytest.mark.parametrize("sink_ids", ["market.daily", b"market.daily"])
def test_builder_rejects_string_sink_collections(sink_ids: str | bytes) -> None:
    """Reject text values instead of treating each character as a sink id."""
    with pytest.raises(TypeError, match="reviewed_sink_ids must be an iterable"):
        build_runtime_role_plan(
            profile="reviewed",
            roles={"one_shot_executor"},  # type: ignore[arg-type]
            reviewed_config_id="test.one-shot",
            reviewed_sink_ids=sink_ids,  # type: ignore[arg-type]
        )


def test_builder_rejects_non_string_sink_members() -> None:
    """Reject non-string sink entries with a clear validation error."""
    with pytest.raises(ValueError, match="sink id must be a string"):
        build_runtime_role_plan(
            profile="reviewed",
            roles={"one_shot_executor"},  # type: ignore[arg-type]
            reviewed_config_id="test.one-shot",
            reviewed_sink_ids={7},  # type: ignore[arg-type]
        )


def test_one_shot_role_plan_rejects_invalid_enable_query() -> None:
    """Do not treat unknown role queries as disabled one-shot roles."""
    plan = build_runtime_role_plan(
        profile="reviewed",
        roles={"one_shot_executor"},  # type: ignore[arg-type]
        reviewed_config_id="test.one-shot",
        reviewed_sink_ids={"market.daily"},
    )
    with pytest.raises(ValueError, match="unsupported"):
        plan.enables("shell")  # type: ignore[arg-type]
