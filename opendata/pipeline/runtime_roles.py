"""Explicit process-role plans for the reviewed and legacy runtimes.

The default plan preserves the current API plus legacy collector deployment.
Activating reviewed scheduling or workers requires a named reviewed
application and switches the whole deployment plan into reviewed mode. This
prevents a default rollout from starting a second scheduler against the same
warehouse sinks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable
    from datetime import timedelta

    from opendata.pipeline.run_service import RunOutcome

RuntimeRole = Literal[
    "api",
    "scheduler",
    "worker",
    "legacy",
    "one_shot_submitter",
    "one_shot_executor",
]
RuntimeProfile = Literal["legacy_compatible", "reviewed"]

_ROLE_VALUES = frozenset(
    {
        "api",
        "scheduler",
        "worker",
        "legacy",
        "one_shot_submitter",
        "one_shot_executor",
    }
)
_ONE_SHOT_ROLES = frozenset({"one_shot_submitter", "one_shot_executor"})
_ONE_SHOT_SINK_ROLES = frozenset({"scheduler", "worker", *_ONE_SHOT_ROLES})
_CONFIG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


class WorkerOnceSPI(Protocol):  # pragma: no cover
    """Injected one-run worker boundary used by command-line composition roots."""

    def __call__(self, worker: str, ttl: timedelta) -> RunOutcome | None:
        """Claim and execute at most one already-queued reviewed run."""
        ...


class OneShotWorkerSPI(Protocol):  # pragma: no cover
    """Injected one-shot worker boundary keyed by an already reviewed run id."""

    def __call__(self, run_key: str) -> RunOutcome | None:
        """Execute only the specified one-shot run and return its safe outcome."""
        ...


@dataclass(frozen=True, slots=True)
class RuntimeRolePlan:
    """Immutable deployment-wide selection of compatible process roles."""

    profile: RuntimeProfile
    roles: frozenset[RuntimeRole]
    reviewed_config_id: str | None = None
    reviewed_sink_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Reject implicit reviewed activation and legacy/reviewed writer overlap."""
        if self.profile not in {"legacy_compatible", "reviewed"}:
            raise ValueError("runtime profile is unsupported")
        if not isinstance(self.roles, frozenset) or not self.roles:
            raise ValueError("runtime roles must be a non-empty frozen set")
        if any(role not in _ROLE_VALUES for role in self.roles):
            raise ValueError("runtime roles contain an unsupported value")
        if not isinstance(self.reviewed_sink_ids, frozenset):
            raise ValueError("reviewed sink ids must be a frozen set")
        if any(not isinstance(sink, str) for sink in self.reviewed_sink_ids):
            raise ValueError("reviewed sink id must be a string")
        if any(not _CONFIG_ID.fullmatch(sink) for sink in self.reviewed_sink_ids):
            raise ValueError("reviewed sink id is invalid")

        if self.profile == "legacy_compatible":
            if self.roles - {"api", "legacy"}:
                raise ValueError("legacy-compatible plans cannot enable reviewed roles")
            if self.reviewed_config_id is not None or self.reviewed_sink_ids:
                raise ValueError("legacy-compatible plans cannot select reviewed configuration")
            return

        if "legacy" in self.roles:
            raise ValueError("legacy and reviewed runtime roles are mutually exclusive")
        selected_one_shot_roles = self.roles & _ONE_SHOT_ROLES
        if selected_one_shot_roles and self.roles not in {
            frozenset({"one_shot_submitter"}),
            frozenset({"one_shot_executor"}),
        }:
            raise ValueError("one-shot runtime roles must run as a dedicated process role")
        if not isinstance(self.reviewed_config_id, str) or not _CONFIG_ID.fullmatch(
            self.reviewed_config_id
        ):
            raise ValueError("reviewed roles require an explicit reviewed config id")
        if (self.roles & _ONE_SHOT_SINK_ROLES) and not self.reviewed_sink_ids:
            raise ValueError("reviewed writer and one-shot roles require declared sink ids")

    def enables(self, role: RuntimeRole) -> bool:
        """Return whether the selected deployment plan enables one role."""
        if role not in _ROLE_VALUES:
            raise ValueError("runtime role is unsupported")
        return role in self.roles


def build_runtime_role_plan(
    *,
    profile: RuntimeProfile = "legacy_compatible",
    roles: Iterable[RuntimeRole] | None = None,
    reviewed_config_id: str | None = None,
    reviewed_sink_ids: Iterable[str] = (),
) -> RuntimeRolePlan:
    """Build a checked role plan without reading environment or application state.

    With no arguments, the result preserves the existing API and legacy
    collector roles. Reviewed scheduler, worker, and dedicated one-shot roles
    require an explicit ``profile="reviewed"``, config id, and declared
    write-sink set. One-shot submitter and executor roles each run alone.

    Args:
        profile: Mutually exclusive compatibility or reviewed deployment mode.
        roles: Enabled process roles. Omission uses the legacy-compatible set.
        reviewed_config_id: Stable identifier of the caller's reviewed app config.
        reviewed_sink_ids: Dataset or sink identifiers this reviewed app may write.

    Returns:
        A validated immutable deployment plan.

    Raises:
        TypeError: If a role or sink collection is not iterable or is a string.
        ValueError: If the role selection could start an unreviewed writer or
            combine a one-shot role with another runtime role.
    """
    if isinstance(roles, (str, bytes)):
        raise TypeError("roles must be an iterable of role names, not a string or bytes")
    if isinstance(reviewed_sink_ids, (str, bytes)):
        raise TypeError("reviewed_sink_ids must be an iterable of sink ids, not a string or bytes")
    if roles is None:
        if profile != "legacy_compatible":
            raise ValueError("reviewed profile requires an explicit role selection")
        role_set: frozenset[RuntimeRole] = frozenset({"api", "legacy"})
    else:
        role_set = frozenset(roles)
    sinks = frozenset(reviewed_sink_ids)
    return RuntimeRolePlan(
        profile=profile,
        roles=role_set,
        reviewed_config_id=reviewed_config_id,
        reviewed_sink_ids=sinks,
    )
