"""Explicit, immutable registry for versioned dataset contracts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn, TypeAlias

from pydantic import ValidationError

from opendata.data.platform_contracts import DatasetContract

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_CONTRACTS = 5000
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 100_000
ContractKey: TypeAlias = tuple[str, str, str]
StoredContract: TypeAlias = tuple[str, str]


class DatasetContractRegistryError(Exception):
    """Base exception for explicit dataset contract registry failures."""


class DatasetContractNotFoundError(DatasetContractRegistryError):
    """Raised when a dataset's exact schema and semantic versions are absent."""


class DatasetContractConflictError(DatasetContractRegistryError):
    """Raised when one version identity maps to different contract payloads."""


@dataclass(frozen=True, init=False)
class DatasetContractRegistry:
    """Immutable index of explicit dataset schema and semantic versions.

    The registry stores canonical serialized payloads rather than caller-owned
    model instances. Each resolution returns a newly validated model, so changes
    to an input model or a resolved model cannot mutate the registered version.
    """

    _payloads: Mapping[ContractKey, StoredContract]

    def __init__(self, contracts: Sequence[DatasetContract]) -> None:
        """Build a registry from an explicit sequence of validated contracts."""
        if isinstance(contracts, (str, bytes)):
            raise DatasetContractRegistryError("contracts must be a sequence of DatasetContract")
        payloads: dict[ContractKey, StoredContract] = {}
        for contract in contracts:
            if not isinstance(contract, DatasetContract):
                raise DatasetContractRegistryError(
                    "contracts must contain only DatasetContract instances"
                )
            try:
                payload = _canonical_payload(contract)
            except (TypeError, ValueError) as exc:
                raise DatasetContractRegistryError(
                    "a dataset contract cannot be serialized as strict JSON"
                ) from exc
            key = (contract.dataset_id, contract.schema_version, contract.semantic_version)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            previous = payloads.get(key)
            if previous is not None and previous[1] != digest:
                raise DatasetContractConflictError(
                    "conflicting contract payloads share one dataset/schema/semantic version"
                )
            payloads[key] = (payload, digest)
        object.__setattr__(self, "_payloads", MappingProxyType(payloads))

    def resolve(
        self,
        dataset_id: str,
        schema_version: str,
        semantic_version: str,
    ) -> DatasetContract:
        """Resolve one exact dataset/schema/semantic version triple."""
        key = (dataset_id, schema_version, semantic_version)
        try:
            payload, _ = self._payloads[key]
        except KeyError as exc:
            raise DatasetContractNotFoundError(
                "no contract is registered for the requested dataset and versions"
            ) from exc
        try:
            return DatasetContract.model_validate_json(payload)
        except ValidationError as exc:
            # Stored payloads were validated at construction, so this indicates
            # internal corruption rather than a caller's unknown-version error.
            raise DatasetContractRegistryError("stored dataset contract is invalid") from exc

    @property
    def contracts(self) -> Mapping[ContractKey, DatasetContract]:
        """Return an immutable mapping containing detached contract copies."""
        detached = {
            key: DatasetContract.model_validate_json(payload)
            for key, (payload, _) in self._payloads.items()
        }
        return MappingProxyType(detached)

    def __len__(self) -> int:
        """Return the number of distinct registered version triples."""
        return len(self._payloads)


def canonical_sha256(contract: DatasetContract) -> str:
    """Return the SHA-256 of a contract's normalized, sorted native JSON."""
    if not isinstance(contract, DatasetContract):
        raise TypeError("contract must be a DatasetContract")
    payload = _canonical_payload(contract)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_dataset_contract_registry(path: str | Path) -> DatasetContractRegistry:
    """Load a bounded strict-JSON contract registry from an explicit path.

    The only accepted document shape is ``{"registry_version": 1,
    "contracts": [...]}``. No default path, environment fallback, or registry
    inference is performed.
    """
    registry_path = Path(path)
    try:
        with registry_path.open("rb") as stream:
            raw = stream.read(_MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise DatasetContractRegistryError("dataset contract registry is unreadable") from exc
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise DatasetContractRegistryError("dataset contract registry exceeds the 1 MiB limit")
    try:
        text = raw.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except DatasetContractRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DatasetContractRegistryError(
            "dataset contract registry is not strict UTF-8 JSON"
        ) from exc
    _validate_json_tree(document)
    if not isinstance(document, dict):
        raise DatasetContractRegistryError("dataset contract registry root must be an object")
    if set(document) != {"registry_version", "contracts"}:
        raise DatasetContractRegistryError(
            "dataset contract registry root requires only registry_version and contracts"
        )
    version = document["registry_version"]
    if type(version) is not int or version != 1:
        raise DatasetContractRegistryError("dataset contract registry version must be integer 1")
    contracts = document["contracts"]
    if not isinstance(contracts, list):
        raise DatasetContractRegistryError("dataset contract registry contracts must be an array")
    if len(contracts) > _MAX_CONTRACTS:
        raise DatasetContractRegistryError("dataset contract registry exceeds 5000 contracts")
    validated: list[DatasetContract] = []
    for index, payload in enumerate(contracts):
        validated.append(_validate_contract_payload(index, payload))
    return DatasetContractRegistry(validated)


def _canonical_payload(contract: DatasetContract) -> str:
    """Serialize one validated contract to its stable native JSON form."""
    return json.dumps(
        contract.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_contract_payload(index: int, payload: object) -> DatasetContract:
    """Validate one JSON payload and identify the failing registry entry."""
    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return DatasetContract.model_validate_json(serialized)
    except (TypeError, ValueError, ValidationError) as exc:
        raise DatasetContractRegistryError(f"dataset contract at index {index} is invalid") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object and reject duplicate property names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetContractRegistryError("dataset contract JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    """Reject non-standard JSON numeric literals such as NaN and Infinity."""
    raise DatasetContractRegistryError(f"dataset contract JSON contains invalid number {value}")


def _validate_json_tree(document: object) -> None:
    """Bound JSON nesting and node count after parsing the bounded source file."""
    stack: list[tuple[object, int]] = [(document, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise DatasetContractRegistryError("dataset contract JSON exceeds 100000 nodes")
        if depth > _MAX_JSON_DEPTH:
            raise DatasetContractRegistryError("dataset contract JSON exceeds depth 32")
        if isinstance(value, dict):
            for key, item in value.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise DatasetContractRegistryError("dataset contract JSON contains a non-finite number")
        elif value is None or isinstance(value, (str, bool, int, float)):
            continue
        else:
            raise DatasetContractRegistryError("dataset contract JSON contains a non-JSON value")


__all__ = [
    "DatasetContractConflictError",
    "DatasetContractNotFoundError",
    "DatasetContractRegistry",
    "DatasetContractRegistryError",
    "canonical_sha256",
    "load_dataset_contract_registry",
]
