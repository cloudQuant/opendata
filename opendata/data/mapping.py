"""Source field mappings (design §8.2 口径映射表).

Each source ships one ``mappings/<source>.yaml`` declaring, per
domain, how its raw columns map to the contract fields: the column
rename, the unit conversion the source needs (``手 -> 股``), the
business-key normalization (``600519.SH -> 600519``) and the
per-field tolerances the cross-check applies.

The loader is fail-closed in both directions:

* a malformed file or an unknown source/domain raises;
* :func:`normalize_frame` raises when a **mapped** column is missing
  from the frame - the design's counterexample: a source whose field
  names do not match must error, never silently compare nothing.

Columns the mapping does not list are dropped from the contract view
(the ods table keeps them; the contract view is only the comparison
and merge surface).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

_MAPPINGS_DIR = Path(__file__).parent / "mappings"

#: Relative tolerance applied when a field declares none.
DEFAULT_TOLERANCE = 1e-6

_FIELD_KEYS = frozenset({"from", "scale", "normalize"})
_NORMALIZERS = frozenset({"plain"})


@dataclass(frozen=True)
class FieldMapping:
    """One contract field and how the source delivers it.

    Attributes:
        source_column: Column name in the source frame.
        scale: Multiplier applied to numeric values (unit conversion).
        normalize: Value normalization recipe (``plain`` strips an
            exchange suffix); None keeps the value as delivered.
    """

    source_column: str
    scale: float = 1.0
    normalize: str | None = None


@dataclass(frozen=True)
class DomainMapping:
    """The mapping of one domain for one source.

    Attributes:
        domain: Registered domain identifier.
        key: Contract key fields, in key order.
        fields: Contract field to :class:`FieldMapping`.
        tolerances: Relative tolerance per field (numeric compare).
    """

    domain: str
    key: tuple[str, ...]
    fields: Mapping[str, FieldMapping]
    tolerances: Mapping[str, float] = field(default_factory=dict)

    def tolerance(self, contract_field: str) -> float:
        """Relative tolerance of one field.

        Args:
            contract_field: Contract field name.

        Returns:
            The declared tolerance, or :data:`DEFAULT_TOLERANCE`.
        """
        return self.tolerances.get(contract_field, DEFAULT_TOLERANCE)


@dataclass(frozen=True)
class SourceMapping:
    """Every domain mapping of one source.

    Attributes:
        source: Source identifier.
        domains: Domain identifier to :class:`DomainMapping`.
    """

    source: str
    domains: Mapping[str, DomainMapping]


@cache
def load_mapping(source: str) -> SourceMapping:
    """Load and validate one source's mapping file.

    Args:
        source: Source identifier (file stem under ``data/mappings``).

    Returns:
        The parsed mapping.

    Raises:
        LookupError: If no mapping file exists for the source.
        RuntimeError: If the file is unreadable or malformed (fail
            closed: a bad mapping must not degrade into no comparison).
    """
    path = _MAPPINGS_DIR / f"{source}.yaml"
    if not path.exists():
        known = sorted(item.stem for item in _MAPPINGS_DIR.glob("*.yaml"))
        raise LookupError(f"unknown source {source!r}; mappings available: {known}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"mapping file {path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"mapping file {path} must be a mapping")
    declared_source = payload.get("source")
    if declared_source != source:
        raise RuntimeError(f"mapping file {path} declares source {declared_source!r}")
    raw_domains = payload.get("domains")
    if not isinstance(raw_domains, dict) or not raw_domains:
        raise RuntimeError(f"mapping file {path} declares no domains")
    domains = {name: _parse_domain(name, entry, path) for name, entry in raw_domains.items()}
    return SourceMapping(source=source, domains=domains)


def require_domain_mapping(source: str, domain: str) -> DomainMapping:
    """Return one domain's mapping, or fail closed.

    Args:
        source: Source identifier.
        domain: Domain identifier.

    Returns:
        The domain mapping.

    Raises:
        LookupError: If the source or the domain is not mapped.
    """
    mapping = load_mapping(source)
    if domain not in mapping.domains:
        raise LookupError(
            f"unknown domain {domain!r} for source {source!r}; mapped: {sorted(mapping.domains)}"
        )
    return mapping.domains[domain]


def normalize_frame(frame: pd.DataFrame, mapping: DomainMapping) -> pd.DataFrame:
    """Project a source frame onto the contract fields.

    Args:
        frame: Raw source frame.
        mapping: The domain mapping to apply.

    Returns:
        A frame with only the mapped contract columns, in mapping
        order, with unit conversion and key normalization applied.

    Raises:
        ValueError: If a mapped source column is missing from the
            frame (the field-name counterexample must fail closed).
    """
    missing = [
        field_mapping.source_column
        for field_mapping in mapping.fields.values()
        if field_mapping.source_column not in frame.columns
    ]
    if missing:
        raise ValueError(
            f"source frame is missing mapped columns {sorted(missing)} for domain "
            f"{mapping.domain!r}; the field mapping and the source disagree (fail closed)"
        )
    values: dict[str, pd.Series] = {}
    for contract_field, field_mapping in mapping.fields.items():
        series = frame[field_mapping.source_column]
        if field_mapping.scale != 1.0:
            series = pd.to_numeric(series, errors="coerce") * field_mapping.scale
        if field_mapping.normalize == "plain":
            series = series.map(_plain_value)
        values[contract_field] = series
    return pd.DataFrame(values, columns=list(mapping.fields))


def _plain_value(value: object) -> object:
    """Strip an exchange suffix from a symbol-like value.

    The same rule the fetchers use (``600519.SH`` -> ``600519``);
    kept local so the mapping layer does not depend on a provider.

    Args:
        value: Cell value.

    Returns:
        The bare code, or the value unchanged when it is not a string.
    """
    if isinstance(value, str):
        return value.split(".", maxsplit=1)[0].strip()
    return value


def _parse_domain(domain: str, entry: Any, path: Path) -> DomainMapping:  # noqa: ANN401  # parsed YAML
    """Parse and validate one domain entry."""
    if not isinstance(entry, dict):
        raise RuntimeError(f"malformed domain {domain!r} in {path}")
    key = entry.get("key")
    if not isinstance(key, list) or not key or not all(isinstance(item, str) for item in key):
        raise RuntimeError(f"domain {domain!r} in {path} needs a non-empty key list")
    raw_fields = entry.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise RuntimeError(f"domain {domain!r} in {path} declares no fields")
    fields: dict[str, FieldMapping] = {}
    for contract_field, spec in raw_fields.items():
        if not isinstance(spec, dict) or "from" not in spec:
            raise RuntimeError(f"field {contract_field!r} of {domain!r} needs a 'from' column")
        unknown = set(spec) - _FIELD_KEYS
        if unknown:
            raise RuntimeError(
                f"field {contract_field!r} of {domain!r} has unknown keys {sorted(unknown)}"
            )
        normalize = spec.get("normalize")
        if normalize is not None and normalize not in _NORMALIZERS:
            raise RuntimeError(
                f"field {contract_field!r} of {domain!r} has unknown normalizer {normalize!r}"
            )
        try:
            scale = float(spec.get("scale", 1.0))
            source_column = str(spec["from"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"field {contract_field!r} of {domain!r} is malformed: {exc}"
            ) from exc
        fields[contract_field] = FieldMapping(source_column, scale, normalize)
    for key_field in key:
        if key_field not in fields:
            raise RuntimeError(f"domain {domain!r} in {path} keys {key_field!r} without a mapping")
    raw_tolerances = entry.get("tolerances") or {}
    if not isinstance(raw_tolerances, dict):
        raise RuntimeError(f"domain {domain!r} in {path} tolerances must be a mapping")
    tolerances = {str(name): float(value) for name, value in raw_tolerances.items()}
    return DomainMapping(domain=domain, key=tuple(key), fields=fields, tolerances=tolerances)


def mapping_sources() -> list[str]:
    """List the sources that ship a mapping file."""
    return sorted(item.stem for item in _MAPPINGS_DIR.glob("*.yaml"))


def mapping_as_json(mapping: DomainMapping) -> str:
    """Render a mapping for logs and evidence.

    Args:
        mapping: The domain mapping.

    Returns:
        A compact JSON rendering.
    """
    return json.dumps(
        {
            "domain": mapping.domain,
            "key": list(mapping.key),
            "fields": {
                name: {"from": spec.source_column, "scale": spec.scale, "normalize": spec.normalize}
                for name, spec in mapping.fields.items()
            },
            "tolerances": dict(mapping.tolerances),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
