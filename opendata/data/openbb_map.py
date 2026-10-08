"""OpenBB compatibility map loader (FR-7 / AC-10, design §10.4).

The map is data: OpenBB interface names on one side, our provider/domain
pairs on the other, contract-only. Loading is fail-closed so a malformed or
drifting table cannot silently weaken the acceptance rule that every
**enabled** provider appears in it with a consuming scenario.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from opendata.data.domains import DomainSpec

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


def _read_map_file(source: Path) -> tuple[OpenBBMapEntry, ...]:
    """Read and validate one version-1 map file without registry access."""
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


def _project_default_map() -> tuple[OpenBBMapEntry, ...]:
    """Project current registry facts over the shipped legacy scenarios."""
    legacy_entries = _read_map_file(OPENBB_MAP_PATH)

    from opendata.data.providers.catalog import get_provider
    from opendata.data.registry import get_registry

    registry = get_registry()
    capabilities = registry.capabilities()
    descriptors = registry.list_model_descriptors()
    capabilities_by_pair: dict[tuple[str, str], list[bool]] = {}
    for capability in capabilities:
        pair = (capability.source, capability.domain)
        capabilities_by_pair.setdefault(pair, []).append(capability.verified)

    projected_entries: list[OpenBBMapEntry] = []
    covered: set[tuple[str, str]] = set()
    for entry in legacy_entries:
        projected_rows: list[OursCapability] = []
        for ours in entry.ours:
            if ours.status == "pending":
                projected_rows.append(ours)
                continue

            pair = (ours.provider, ours.domain)
            verified_values = capabilities_by_pair.get(pair)
            if not verified_values:
                raise OpenBBMapError(
                    f"enabled legacy capability {ours.provider}/{ours.domain} in "
                    f"{entry.model!r} is missing from the provider registry"
                )
            domain_spec = _require_domain_spec(ours.domain, entry.model, OPENBB_MAP_PATH)
            if ours.contract != domain_spec.contract:
                raise OpenBBMapError(
                    f"legacy capability {ours.provider}/{ours.domain} in {entry.model!r} "
                    f"declares contract {ours.contract!r}, but the domain registry declares "
                    f"{domain_spec.contract!r}"
                )
            status = "verified" if all(verified_values) else "registered"
            projected_rows.append(
                OursCapability(
                    provider=ours.provider,
                    domain=ours.domain,
                    contract=domain_spec.contract,
                    status=status,
                )
            )
            covered.add(pair)
        projected_entries.append(
            OpenBBMapEntry(model=entry.model, scenario=entry.scenario, ours=tuple(projected_rows))
        )

    for descriptor in descriptors:
        if descriptor.capability_identity[1] != descriptor.domain:
            raise OpenBBMapError(
                f"registry descriptor {descriptor.source}/{descriptor.model} has an "
                "inconsistent domain identity"
            )
        if descriptor.capability_identity[4] != descriptor.source:
            raise OpenBBMapError(
                f"registry descriptor {descriptor.source}/{descriptor.model} has an "
                "inconsistent source identity"
            )
        pair = (descriptor.source, descriptor.domain)
        if pair not in capabilities_by_pair:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} has no capability"
            )

        try:
            binding = get_provider(descriptor.source).fetcher_dict.get(descriptor.model)
        except KeyError as exc:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} has no local binding"
            ) from exc
        if binding is None:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} has no local binding"
            )
        if descriptor.model not in binding.canonical_model_ids:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} is not an exact "
                "canonical binding"
            )
        if binding.scenario is None:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} has no scenario"
            )
        try:
            fetcher = registry.resolve_model(descriptor.source, descriptor.model)
        except (LookupError, RuntimeError) as exc:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} cannot resolve its fetcher"
            ) from exc
        if (type(fetcher).__module__, type(fetcher).__name__) != (
            binding.module,
            binding.class_name,
        ):
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} does not match its "
                "exact local fetcher binding"
            )
        capability = fetcher.capability
        actual_identity = (
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        )
        if descriptor.capability_identity != actual_identity:
            raise OpenBBMapError(
                f"registry model {descriptor.source}/{descriptor.model} has a stale capability "
                "descriptor"
            )

        domain_spec = _require_domain_spec(descriptor.domain, descriptor.model, OPENBB_MAP_PATH)
        status = "verified" if descriptor.verified else "registered"
        canonical_row = OursCapability(
            provider=descriptor.source,
            domain=descriptor.domain,
            contract=domain_spec.contract,
            status=status,
        )
        matching_index = next(
            (
                index
                for index, entry in enumerate(projected_entries)
                if entry.model == descriptor.model and entry.scenario == binding.scenario
            ),
            None,
        )
        if matching_index is None:
            projected_entries.append(
                OpenBBMapEntry(
                    model=descriptor.model,
                    scenario=binding.scenario,
                    ours=(canonical_row,),
                )
            )
        else:
            entry = projected_entries[matching_index]
            if canonical_row not in entry.ours:
                projected_entries[matching_index] = OpenBBMapEntry(
                    model=entry.model,
                    scenario=entry.scenario,
                    ours=(*entry.ours, canonical_row),
                )
        covered.add(pair)

    registered_pairs = set(capabilities_by_pair)
    if covered != registered_pairs:
        missing = sorted(registered_pairs - covered)
        unexpected = sorted(covered - registered_pairs)
        raise OpenBBMapError(
            "projected compatibility map does not match the provider registry; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return tuple(projected_entries)


class _OpenBBMapLoader:
    """Callable loader retaining the historical ``cache_clear`` test API."""

    def __call__(self, path: str | None = None) -> tuple[OpenBBMapEntry, ...]:
        """Load the dynamic default projection or a strict custom v1 file."""
        if path is not None:
            return _read_map_file(Path(path))
        return _project_default_map()

    @staticmethod
    def cache_clear() -> None:
        """Keep compatibility with callers; map results are no longer cached."""


load_openbb_map = _OpenBBMapLoader()


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


def _require_domain_spec(domain: str, model: str, source: Path) -> DomainSpec:
    """Return a real DomainSpec or fail closed with map context."""
    from opendata.data.domains import require_domain

    try:
        return require_domain(domain)
    except LookupError as exc:
        raise OpenBBMapError(
            f"ours row of {model!r} in {source} names unregistered domain {domain!r}"
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
