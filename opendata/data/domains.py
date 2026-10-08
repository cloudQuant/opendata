"""Domain registry and name derivation (design §4.3, FR-2).

``domains.yaml`` is the single source of the domain identifiers; every
other name in the platform is derived from it and must never be
hand-written elsewhere:

* ods tables: ``ods_<domain>_<source>``
* dwd tables: ``dwd_<domain>``
* REST paths: ``/api/v1/data/<rest_path>``
* WS push events: ``data.<domain>``
* frontend labels: ``display_name``

The loader is fail-closed: malformed YAML, unknown fields, bad
identifiers, duplicate derived names or unknown contract models all
raise instead of being silently skipped (quality spec §0).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StrictStr, model_validator
from yaml.resolver import BaseResolver

from opendata.data import models as _models

if TYPE_CHECKING:
    from collections.abc import Generator

    from opendata.data.models import ContractModel

_DOMAINS_PATH = Path(__file__).parent / "domains.yaml"
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_REST_PATH_RE = re.compile(r"^[a-z0-9]+(?:/[a-z0-9_-]+)+$")
_CONTRACT_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: Longest MySQL identifier (ods_/dwd_ prefixes plus domain/source).
_TABLE_NAME_MAX = 64
_SEMANTICS_FIELDS = frozenset(
    {
        "temporal_kind",
        "time_field",
        "natural_key",
        "filter_dims",
        "storage_mode",
        "permissions",
    }
)
_CONTRACT_MODELS: Mapping[str, type[ContractModel]] = {
    name: model
    for name in _models.__all__
    if isinstance((model := getattr(_models, name)), type)
    and model is not _models.ContractModel
    and issubclass(model, _models.ContractModel)
}


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Local safe loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> Generator[dict[object, object], None, None]:
    """Construct one mapping while rejecting duplicate or unhashable keys."""
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    yield mapping
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class DomainSpec(BaseModel):
    """One ``domains.yaml`` entry.

    Attributes:
        display_name: Frontend label (data catalog, FR-20).
        rest_path: REST path segments below ``/api/v1/data``.
        contract: Name of the contract model that is the dwd schema
            upstream for this domain (design §8.3).
        priority: Explicit delivery priority from requirement §6.2. None
            means unknown and must not be inferred as P2.
        temporal_kind: Explicit time semantics when the domain has been
            reviewed; ``None`` marks a legacy, unreviewed declaration.
        time_field: A real contract field or explicit ``None`` for a
            snapshot/document whose time is unknown or inapplicable.
        natural_key: Stable contract-field identity for this domain.
        filter_dims: Contract fields that may be used as local filters.
        storage_mode: Whether data is transient, upserted, or appended.
        permissions: Locally declared operations; these do not grant
            caller authorization or source-data rights.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str
    rest_path: str
    contract: str
    priority: Literal["P0", "P1", "P2"] | None = None
    temporal_kind: Literal["series", "snapshot", "event", "document"] | None = None
    time_field: StrictStr | None = None
    natural_key: tuple[StrictStr, ...] = ()
    filter_dims: tuple[StrictStr, ...] = ()
    storage_mode: Literal["transient", "upsert", "append"] | None = None
    permissions: tuple[Literal["query", "store", "export", "subscribe"], ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _require_complete_semantics_group(cls, value: object) -> object:
        """Preserve the distinction between omitted fields and explicit null."""
        if not isinstance(value, Mapping):
            return value
        provided = _SEMANTICS_FIELDS.intersection(value)
        if provided and provided != _SEMANTICS_FIELDS:
            missing = sorted(_SEMANTICS_FIELDS - provided)
            raise ValueError(f"semantics fields must be declared together; missing {missing}")
        if provided and value.get("temporal_kind") is None:
            raise ValueError("temporal_kind must be explicitly declared")
        return value

    @model_validator(mode="after")
    def _validate_declared_semantics(self) -> DomainSpec:
        """Validate semantic rules that do not depend on the contract model."""
        if not self.semantics_declared:
            return self
        if self.storage_mode is None:
            raise ValueError("storage_mode must be explicitly declared")
        if not self.natural_key:
            raise ValueError("natural_key must not be empty")
        for field_name, values in (
            ("natural_key", self.natural_key),
            ("filter_dims", self.filter_dims),
            ("permissions", self.permissions),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        if self.temporal_kind in {"series", "event"} and not self.time_field:
            raise ValueError(f"{self.temporal_kind} domains require a time_field")
        if self.storage_mode == "transient" and "store" in self.permissions:
            raise ValueError("transient storage_mode cannot declare store permission")
        return self

    @property
    def semantics_declared(self) -> bool:
        """Whether all six domain semantics were explicitly declared."""
        return self.temporal_kind is not None


def _parse_entry(domain: object, entry: object, *, version: int) -> DomainSpec:
    """Validate one registry entry.

    Args:
        domain: The domain identifier key from the YAML mapping.
        entry: The entry mapping.
        version: The registry format version, either 1 or 2.

    Returns:
        The validated domain spec.

    Raises:
        ValueError: If the identifier, entry fields, rest path,
            contract model or derived table names are invalid.
    """
    if not isinstance(domain, str) or not _DOMAIN_ID_RE.match(domain):
        raise ValueError(f"invalid domain identifier {domain!r}")
    if not isinstance(entry, dict):
        raise ValueError(f"domain {domain!r} must be a mapping")
    spec = DomainSpec(**entry)
    if version == 2 and not spec.semantics_declared:
        raise ValueError(f"domain {domain!r}: version 2 requires all six semantics fields")
    if not spec.display_name.strip():
        raise ValueError(f"domain {domain!r}: display_name must not be empty")
    if not _REST_PATH_RE.match(spec.rest_path):
        raise ValueError(f"domain {domain!r}: invalid rest_path {spec.rest_path!r}")
    if spec.contract not in _CONTRACT_MODELS:
        raise ValueError(f"domain {domain!r}: unknown contract model {spec.contract!r}")
    if spec.semantics_declared:
        contract_fields = _CONTRACT_MODELS[spec.contract].model_fields
        semantic_fields = [
            ("time_field", spec.time_field),
            *(("natural_key", field_name) for field_name in spec.natural_key),
            *(("filter_dims", field_name) for field_name in spec.filter_dims),
        ]
        for role, field_name in semantic_fields:
            if field_name is None:
                continue
            if not _CONTRACT_FIELD_RE.fullmatch(field_name):
                raise ValueError(f"domain {domain!r}: unsafe {role} contract field {field_name!r}")
            if field_name not in contract_fields:
                raise ValueError(
                    f"domain {domain!r}: {role} references unknown contract field {field_name!r}"
                )
    for prefix in (f"ods_{domain}_<source>", f"dwd_{domain}"):
        if len(prefix) > _TABLE_NAME_MAX:
            raise ValueError(f"domain {domain!r}: derived table name exceeds {_TABLE_NAME_MAX}")
    return spec


def parse_domains(text: str) -> dict[str, DomainSpec]:
    """Parse and validate the domain registry contents.

    Args:
        text: Raw ``domains.yaml`` contents.

    Returns:
        Domain identifier to validated spec.

    Raises:
        ValueError: On malformed YAML or any rule violation.
    """
    loader = _UniqueKeySafeLoader(text)
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise ValueError(f"domains registry is not valid YAML: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(data, dict) or "domains" not in data:
        raise ValueError("domains registry must be a mapping with a 'domains' key")
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        raise ValueError("domains registry version must be integer 1 or 2")
    raw = data["domains"]
    if not isinstance(raw, dict) or not raw:
        raise ValueError("domains registry must contain a non-empty 'domains' mapping")

    specs: dict[str, DomainSpec] = {}
    for domain, entry in raw.items():
        specs[domain] = _parse_entry(domain, entry, version=version)

    # dwd tables and WS events derive from the ids, so mapping keys
    # already guarantee their uniqueness; REST paths need a check.
    seen: dict[str, str] = {}
    for domain, spec in specs.items():
        clash = seen.get(spec.rest_path)
        if clash is not None:
            raise ValueError(f"rest_path {spec.rest_path!r} used by {clash!r} and {domain!r}")
        seen[spec.rest_path] = domain
    return specs


@lru_cache(maxsize=1)
def load_domains() -> dict[str, DomainSpec]:
    """Load and validate the domain registry from ``domains.yaml``.

    Returns:
        Domain identifier to validated spec.

    Raises:
        RuntimeError: If the file is missing or invalid (fail closed).
    """
    try:
        text = _DOMAINS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"domain registry {_DOMAINS_PATH} is unreadable: {exc}") from exc
    try:
        return parse_domains(text)
    except ValueError as exc:
        raise RuntimeError(f"domain registry {_DOMAINS_PATH} is invalid: {exc}") from exc


def require_domain(domain: str) -> DomainSpec:
    """Return the spec of a registered domain.

    Args:
        domain: Domain identifier.

    Returns:
        The validated spec.

    Raises:
        LookupError: If the identifier is not registered.
    """
    specs = load_domains()
    if domain not in specs:
        raise LookupError(f"unknown domain {domain!r}; registered: {sorted(specs)}")
    return specs[domain]


def require_domain_semantics(domain: str) -> DomainSpec:
    """Return a domain only when its six semantic fields are reviewed."""
    spec = require_domain(domain)
    if not spec.semantics_declared:
        raise LookupError(f"domain {domain!r} semantics are not declared")
    return spec


def ods_table(domain: str, source: str) -> str:
    """Derive the ods table name of one source for a domain.

    Args:
        domain: Registered domain identifier.
        source: Source identifier (e.g. ``akshare``).

    Returns:
        The ``ods_<domain>_<source>`` table name.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If the source identifier is invalid.
    """
    require_domain(domain)
    if not isinstance(source, str) or not _SOURCE_ID_RE.match(source):
        raise ValueError(f"invalid source identifier {source!r}")
    return f"ods_{domain}_{source}"


def dwd_table(domain: str) -> str:
    """Derive the dwd table name of a domain (``dwd_<domain>``).

    Raises:
        LookupError: If the domain is unknown.
    """
    require_domain(domain)
    return f"dwd_{domain}"


def rest_path(domain: str) -> str:
    """Derive the canonical REST path of a domain.

    Raises:
        LookupError: If the domain is unknown.
    """
    return f"/api/v1/data/{require_domain(domain).rest_path}"


def ws_push_event(domain: str) -> str:
    """Derive the WS push event name of a domain (``data.<domain>``).

    Follows the design's ``data.*`` WS event namespace (``data.diff_alert``,
    FR-12). The subscribe protocol identifies domains by their id
    directly (FR-16), so no separate topic derivation is needed.

    Raises:
        LookupError: If the domain is unknown.
    """
    require_domain(domain)
    return f"data.{domain}"


def display_name(domain: str) -> str:
    """Return the frontend display name of a domain.

    Raises:
        LookupError: If the domain is unknown.
    """
    return require_domain(domain).display_name


def contract_model(domain: str) -> type[ContractModel]:
    """Return the contract model class that is a domain's dwd schema.

    Raises:
        LookupError: If the domain is unknown.
    """
    return _CONTRACT_MODELS[require_domain(domain).contract]
