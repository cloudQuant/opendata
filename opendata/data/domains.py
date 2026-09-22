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
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict

from opendata.data import models as _models

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opendata.data.models import ContractModel

_DOMAINS_PATH = Path(__file__).parent / "domains.yaml"
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_REST_PATH_RE = re.compile(r"^[a-z0-9]+(?:/[a-z0-9_-]+)+$")
#: Longest MySQL identifier (ods_/dwd_ prefixes plus domain/source).
_TABLE_NAME_MAX = 64
_CONTRACT_MODELS: Mapping[str, type[ContractModel]] = {
    name: getattr(_models, name) for name in _models.__all__
}


class DomainSpec(BaseModel):
    """One ``domains.yaml`` entry.

    Attributes:
        display_name: Frontend label (data catalog, FR-20).
        rest_path: REST path segments below ``/api/v1/data``.
        contract: Name of the contract model that is the dwd schema
            upstream for this domain (design §8.3).
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str
    rest_path: str
    contract: str


def _parse_entry(domain: object, entry: object) -> DomainSpec:
    """Validate one registry entry.

    Args:
        domain: The domain identifier key from the YAML mapping.
        entry: The entry mapping.

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
    if not spec.display_name.strip():
        raise ValueError(f"domain {domain!r}: display_name must not be empty")
    if not _REST_PATH_RE.match(spec.rest_path):
        raise ValueError(f"domain {domain!r}: invalid rest_path {spec.rest_path!r}")
    if spec.contract not in _CONTRACT_MODELS:
        raise ValueError(f"domain {domain!r}: unknown contract model {spec.contract!r}")
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
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"domains registry is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or "domains" not in data:
        raise ValueError("domains registry must be a mapping with a 'domains' key")
    raw = data["domains"]
    if not isinstance(raw, dict) or not raw:
        raise ValueError("domains registry must contain a non-empty 'domains' mapping")

    specs: dict[str, DomainSpec] = {}
    for domain, entry in raw.items():
        specs[domain] = _parse_entry(domain, entry)

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
