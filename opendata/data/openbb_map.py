"""OpenBB compatibility map loader (FR-7 / AC-10, design §10.4).

The map is data: OpenBB interface names on one side, our provider/domain
pairs on the other, contract-only. Loading is fail-closed so a malformed or
drifting table cannot silently weaken the acceptance rule that every
**enabled** provider appears in it with a consuming scenario.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

#: The map file shipped with the package.
OPENBB_MAP_PATH = Path(__file__).parent / "openbb_map.yaml"

#: Placeholder for an upstream model name that has not been confirmed yet.
TBD_MODEL = "TBD"

#: Lifecycle states of an ``ours`` entry.
STATUSES = frozenset({"verified", "registered", "pending"})


class OpenBBMapError(RuntimeError):
    """Raised when the compatibility map is malformed (fail closed)."""


@dataclass(frozen=True)
class OursCapability:
    """One of our provider/domain pairs under an OpenBB interface.

    Attributes:
        provider: Our routing label (the provider package name).
        domain: Registered domain identifier (pending entries may name a
            domain that is not registered yet).
        contract: Contract model name.
        status: ``verified`` (live-checked), ``registered`` (enabled) or
            ``pending`` (planned, not implemented).
        batch: Implementation batch (P0/P1/P2), pending entries only.
    """

    provider: str
    domain: str
    contract: str
    status: str
    batch: str | None = None


@dataclass(frozen=True)
class OpenBBMapEntry:
    """One OpenBB interface and the capabilities that serve it here.

    Attributes:
        model: OpenBB standard-model name, or ``TBD`` when unconfirmed.
        scenario: The consuming scenario (acceptance rule: non-empty).
        ours: Our provider/domain pairs.
    """

    model: str
    scenario: str
    ours: tuple[OursCapability, ...]


@lru_cache(maxsize=1)
def load_openbb_map(path: str | None = None) -> tuple[OpenBBMapEntry, ...]:
    """Load and validate the compatibility map (fail closed).

    Args:
        path: Override for tests; defaults to the shipped file.

    Returns:
        The entries in file order.

    Raises:
        OpenBBMapError: The file is unreadable, malformed, or an entry
            violates the schema or the domain rule (a non-pending entry
            names a domain the registry does not know).
    """
    source = Path(path) if path else OPENBB_MAP_PATH
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OpenBBMapError(f"openbb map {source} is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise OpenBBMapError(f"openbb map {source} must declare version 1")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise OpenBBMapError(f"openbb map {source} must declare a non-empty entries list")
    entries = [_parse_entry(entry, source) for entry in raw_entries]
    return tuple(entries)


def covered_capabilities() -> frozenset[tuple[str, str]]:
    """Return the enabled ``(provider, domain)`` pairs the map covers.

    Pending entries are excluded: they are plans, not enabled capabilities.
    """
    return frozenset(
        (ours.provider, ours.domain)
        for entry in load_openbb_map()
        for ours in entry.ours
        if ours.status in {"verified", "registered"}
    )


def _parse_entry(entry: object, source: Path) -> OpenBBMapEntry:
    """Parse and validate one map entry (fail closed).

    Args:
        entry: Raw YAML mapping.
        source: File being loaded (for error messages).

    Returns:
        The parsed entry.

    Raises:
        OpenBBMapError: The entry or any of its ``ours`` rows is malformed.
    """
    if not isinstance(entry, Mapping):
        raise OpenBBMapError(f"openbb map entry {entry!r} in {source} is not a mapping")
    model = _text(entry.get("openbb", {}), "model", source, context="entry")
    if not model:
        raise OpenBBMapError(f"openbb map entry in {source} needs a model name")
    scenario = _text(entry, "scenario", source, context="entry")
    if not scenario:
        raise OpenBBMapError(f"openbb map entry {model!r} needs a consuming scenario")
    raw_ours = entry.get("ours")
    if not isinstance(raw_ours, list) or not raw_ours:
        raise OpenBBMapError(f"openbb map entry {model!r} needs at least one ours row")
    parsed = tuple(_parse_ours(row, model, source) for row in raw_ours)
    return OpenBBMapEntry(model=model, scenario=scenario, ours=parsed)


def _parse_ours(row: object, model: str, source: Path) -> OursCapability:
    """Parse one ``ours`` row and enforce the domain rule."""
    if not isinstance(row, Mapping):
        raise OpenBBMapError(f"ours row {row!r} of {model!r} in {source} is not a mapping")
    provider = _text(row, "provider", source, context=f"ours of {model!r}")
    domain = _text(row, "domain", source, context=f"ours of {model!r}")
    contract = _text(row, "contract", source, context=f"ours of {model!r}")
    status = _text(row, "status", source, context=f"ours of {model!r}")
    if not provider or not domain or not contract:
        raise OpenBBMapError(f"ours row of {model!r} in {source} has blank required fields")
    if status not in STATUSES:
        raise OpenBBMapError(
            f"ours row of {model!r} in {source} has unknown status {status!r}; "
            f"expected one of {sorted(STATUSES)}"
        )
    batch = _text(row, "batch", source, context=f"ours of {model!r}")
    if status != "pending" and batch:
        raise OpenBBMapError(f"ours row of {model!r} in {source} sets a batch but is not pending")
    if status != "pending":
        _require_registered_domain(domain, model, source)
    return OursCapability(
        provider=provider, domain=domain, contract=contract, status=status, batch=batch or None
    )


def _require_registered_domain(domain: str, model: str, source: Path) -> None:
    """Non-pending rows must name a domain the registry knows.

    Args:
        domain: Domain identifier.
        model: Owning entry (for error messages).
        source: File being loaded.

    Raises:
        OpenBBMapError: The domain is not registered (fail closed).
    """
    from opendata.data.domains import require_domain

    try:
        require_domain(domain)
    except LookupError as exc:
        raise OpenBBMapError(
            f"ours row of {model!r} in {source} names unregistered domain {domain!r}; "
            "only pending rows may do that"
        ) from exc


def _text(value: object, key: str, source: Path, *, context: str) -> str:
    """Extract a non-blank trimmed string field (empty string when absent)."""
    raw = value.get(key) if isinstance(value, Mapping) else None
    return raw.strip() if isinstance(raw, str) else ""


__all__ = [
    "OPENBB_MAP_PATH",
    "STATUSES",
    "TBD_MODEL",
    "OpenBBMapEntry",
    "OpenBBMapError",
    "OursCapability",
    "covered_capabilities",
    "load_openbb_map",
]
