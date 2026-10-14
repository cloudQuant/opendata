"""Load the per-provider offline witness configuration the Iteration 2 claims pipeline runs.

Witness bodies live in ``scripts/quality/iter2_witnesses/<provider>.py``, one module per
provider, and each exposes ``CONFIG``: the registration module, the fixture credential
environment, the allowed hosts, and a per-model witness entry (fixture builder, a valid
query, a tamper plan with its expected typed refusal, and the files each phase pins). Every
digest, exit code, timestamp and verdict that reaches ``claims.json`` is produced by actually
running the probe; nothing in a witness module is copied into a claim as a label.

The split is load-bearing for parallel work: a claim pins *its own provider's* witness
module, so another provider adding or editing a witness can never stale this provider's
``probe`` pin group. Witness discovery is one directory listing, so the set of providers with
witnesses is measured, never hand-listed twice.

Witnesses run exclusively through the offline seam: the engine's
``fetch_pages(..., transport=...)`` and the fetchers' ``http_transport`` attribute, served by
``FixedResponseTransport`` from ``opendata.data.providers._engine.testing``. No leg touches the
network and no ``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.
"""

from __future__ import annotations

import importlib.util
import pkgutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WITNESS_DIR: Final = Path(__file__).resolve().parent / "iter2_witnesses"
WITNESS_DIR_REL: Final = "scripts/quality/iter2_witnesses"

_REQUIRED_ENTRY_KEYS: Final = (
    "fixture_document",
    "query",
    "tamper",
    "refusal_class",
    "refusal_code",
    "tamper_reason",
    "source_files",
    "contract_files",
)


def providers_with_witness() -> list[str]:
    """Every provider that has a witness module, sorted by provider source name."""
    return sorted(
        info.name
        for info in pkgutil.iter_modules([str(WITNESS_DIR)])
        if not info.name.startswith("_")
    )


def witness_module_rel(source: str) -> str:
    """The repo-relative path of one provider's witness module, which is what a claim pins."""
    return f"{WITNESS_DIR_REL}/{source}.py"


def _load(source: str) -> dict[str, Any]:
    """Import one witness module by file location and return its validated ``CONFIG`` mapping."""
    path = WITNESS_DIR / f"{source}.py"
    if not path.is_file():
        raise KeyError(source)
    spec = importlib.util.spec_from_file_location(f"iter2_witnesses_{source}", path)
    if spec is None or spec.loader is None:
        raise KeyError(f"{source}: witness module has no importable spec")
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = getattr(module, "CONFIG", None)
    if not isinstance(config, dict):
        raise KeyError(f"{source}: witness module publishes no CONFIG mapping")
    models = config.get("models")
    if not isinstance(models, dict):
        raise KeyError(f"{source}: CONFIG declares no per-model witness entries")
    for model, entry in models.items():
        if not isinstance(entry, dict):
            raise KeyError(f"{source}::{model}: witness entry is not a mapping")
        missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in entry]
        if missing:
            raise KeyError(f"{source}::{model}: witness entry lacks {missing}")
    return config


_CACHE: dict[str, dict[str, Any]] = {}


def provider_config(source: str) -> dict[str, Any]:
    """Return the witness configuration for one provider, or raise KeyError."""
    if source not in _CACHE:
        _CACHE[source] = _load(source)
    return _CACHE[source]


def model_entry(source: str, model: str) -> dict[str, Any]:
    """Return one model's witness entry inside the provider configuration."""
    return provider_config(source)["models"][model]


def fixture_builders() -> dict[str, Callable[[], Any]]:
    """Map ``provider/model`` to the callable that materializes its fixture bytes."""
    builders: dict[str, Callable[[], Any]] = {}
    for source in providers_with_witness():
        for model, entry in provider_config(source)["models"].items():
            builders[f"{source}/{model}"] = entry["fixture_document"]
    return builders


class _WitnessMapping(Mapping[str, dict[str, Any]]):
    """The provider-keyed witness view, resolved from the per-provider modules on access.

    ``providers_with_witness()`` is the key population, so this view cannot report a provider
    whose module is absent nor hide one whose module exists.
    """

    def __getitem__(self, source: str) -> dict[str, Any]:
        return provider_config(source)

    def __iter__(self) -> Iterator[str]:
        return iter(providers_with_witness())

    def __len__(self) -> int:
        return len(providers_with_witness())


PROVIDER_WITNESSES: Final[Mapping[str, dict[str, Any]]] = _WitnessMapping()

__all__ = [
    "PROVIDER_WITNESSES",
    "WITNESS_DIR_REL",
    "fixture_builders",
    "model_entry",
    "provider_config",
    "providers_with_witness",
    "witness_module_rel",
]
