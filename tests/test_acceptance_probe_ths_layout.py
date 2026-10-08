"""The C64 probes keep reading current files after the provider source tree was reorganized."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import pytest

from scripts.quality import acceptance_item_probe as probe_module
from scripts.quality.acceptance_item_probe import (
    GAP,
    PROVEN,
    Context,
    Facts,
    ProbeError,
    measure_c64_ac4_02,
    measure_c64_ban_mechanism,
    measure_c64_fuyao_errors,
    measure_c64_fuyao_map,
    measure_c64_fuyao_transport,
    measure_c64_lock,
    probe_for,
)

if TYPE_CHECKING:
    from pathlib import Path

Measure = Callable[[Context], Facts]

HTTP_OLD = "opendata_fuyao/http_client.py"
HTTP_CURRENT = "opendata/data/providers/ths/transport/http_client.py"
ERRORS_OLD = "opendata_fuyao/error_messages.yaml"
ERRORS_CURRENT = "opendata/data/providers/ths/transport/error_messages.yaml"
MAP_OLD = "opendata_fuyao/endpoint_map.yaml"
MAP_CURRENT = "opendata/data/providers/ths/endpoint_map.yaml"
LOCK_OLD = "opendata_http/upstream.lock"
LOCK_CURRENT = "opendata/data/providers/akshare/_vendor/upstream.lock"
A3_SMOKE = "docs/evidence/A3/fuyao-live-smoke.txt"
A3_SMOKE_SHA256 = "fbecd370bb1bb4de4bb3b93d06e767fa2526a3e46b532e7d87faded17ce0e9d7"


class ReadSpyContext(Context):
    """Record literal paths passed through the production Context.read method."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, (), {})
        self.read_paths: list[str] = []

    def read(self, rel: str) -> str:
        self.read_paths.append(rel)
        return super().read(rel)


def _green_node_facts(prefix: str, nodes: Sequence[str]) -> Facts:
    """Stand in for named-node execution only in path/binding unit tests."""
    runs = str(len(nodes))
    return {
        f"{prefix}_runs": runs,
        f"{prefix}_exit": "0",
        f"{prefix}_passed": runs,
        f"{prefix}_failed": "0",
        f"{prefix}_skipped": "0",
        f"{prefix}_absent": "-",
    }


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _install_green_node_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module, "node_plane_facts", _green_node_facts)


def test_six_measurements_read_current_canonical_sources_and_keep_a3_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read spy sees canonical paths while source binding and the A3 path remain intact."""
    _install_green_node_stub(monkeypatch)
    ctx = ReadSpyContext(probe_module.REPO_ROOT)
    measurements: tuple[tuple[str, Measure], ...] = (
        ("AC-4|02", measure_c64_ac4_02),
        ("AC-7|01", measure_c64_fuyao_transport),
        ("AC-7|02", measure_c64_fuyao_errors),
        ("AC-7|04", measure_c64_fuyao_map),
        ("§5|03", measure_c64_ban_mechanism),
        ("AC-12|01", measure_c64_lock),
    )

    for item, measure in measurements:
        facts = measure(ctx)
        assert facts["binding"] == "yes", item
        assert probe_for(item).judge(facts).state == PROVEN, item

    assert ctx.read_paths == [
        "opendata/data/http_client.py",
        HTTP_CURRENT,
        HTTP_CURRENT,
        A3_SMOKE,
        ERRORS_CURRENT,
        MAP_CURRENT,
        HTTP_CURRENT,
        LOCK_CURRENT,
    ]
    assert all(path not in ctx.read_paths for path in (HTTP_OLD, ERRORS_OLD, MAP_OLD, LOCK_OLD))
    a3_bytes = (probe_module.REPO_ROOT / A3_SMOKE).read_bytes()
    assert hashlib.sha256(a3_bytes).hexdigest() == A3_SMOKE_SHA256


def test_bad_canonical_files_do_not_use_good_historical_decoys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current-file binding fails closed even when each obsolete path has valid-looking text."""
    _install_green_node_stub(monkeypatch)
    structured_fields = (
        "source",
        "endpoint",
        "parameter_summary",
        "elapsed_seconds",
        "request_id",
        "failure_category",
    )
    _write(tmp_path, "opendata/data/http_client.py", " ".join(f'"{x}"' for x in structured_fields))
    _write(tmp_path, HTTP_CURRENT, "# canonical transport binding removed\n")
    _write(
        tmp_path,
        HTTP_OLD,
        " ".join(
            (
                *[f'"{x}"' for x in structured_fields],
                "max_attempts",
                "retryable",
                "record_rate_limit",
            )
        ),
    )
    _write(tmp_path, A3_SMOKE, "4 passed, 26 deselected\nhttps://fuyao.aicubes.cn\n")

    _write(tmp_path, ERRORS_CURRENT, "# canonical error table removed\n")
    _write(tmp_path, ERRORS_OLD, "category: message: advice:\n")
    _write(tmp_path, MAP_CURRENT, "# canonical endpoint map removed\n")
    _write(tmp_path, MAP_OLD, "version: sections:\n")
    _write(tmp_path, LOCK_CURRENT, "# canonical lock inventory removed\n")
    _write(
        tmp_path,
        LOCK_OLD,
        "commit sha256 https://github.com/cloudQuant/akshare\n",
    )

    ctx = ReadSpyContext(tmp_path)
    measurements: tuple[tuple[str, Measure], ...] = (
        ("AC-4|02", measure_c64_ac4_02),
        ("AC-7|01", measure_c64_fuyao_transport),
        ("AC-7|02", measure_c64_fuyao_errors),
        ("AC-7|04", measure_c64_fuyao_map),
        ("§5|03", measure_c64_ban_mechanism),
        ("AC-12|01", measure_c64_lock),
    )
    decoys = {HTTP_OLD, ERRORS_OLD, MAP_OLD, LOCK_OLD}

    for item, measure in measurements:
        facts = measure(ctx)
        assert facts["binding"] == "no", item
        assert probe_for(item).judge(facts).state == GAP, item

    assert {HTTP_CURRENT, ERRORS_CURRENT, MAP_CURRENT, LOCK_CURRENT} <= set(ctx.read_paths)
    assert A3_SMOKE in ctx.read_paths
    assert decoys.isdisjoint(ctx.read_paths)


@pytest.mark.parametrize(
    ("item", "measure", "historical_path", "canonical_path"),
    [
        ("AC-4|02", measure_c64_ac4_02, HTTP_OLD, HTTP_CURRENT),
        ("AC-7|01", measure_c64_fuyao_transport, HTTP_OLD, HTTP_CURRENT),
        ("§5|03", measure_c64_ban_mechanism, HTTP_OLD, HTTP_CURRENT),
        ("AC-7|02", measure_c64_fuyao_errors, ERRORS_OLD, ERRORS_CURRENT),
        ("AC-7|04", measure_c64_fuyao_map, MAP_OLD, MAP_CURRENT),
        ("AC-12|01", measure_c64_lock, LOCK_OLD, LOCK_CURRENT),
    ],
)
def test_missing_canonical_file_never_falls_back_to_historical_decoy(
    item: str,
    measure: Measure,
    historical_path: str,
    canonical_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid legacy decoy cannot mask a missing canonical file for any of the six probes."""
    _install_green_node_stub(monkeypatch)
    _write(tmp_path, historical_path, "valid-looking historical decoy\n")
    if item == "AC-4|02":
        _write(tmp_path, "opendata/data/http_client.py", "core transport is present\n")
    ctx = ReadSpyContext(tmp_path)

    with pytest.raises(ProbeError, match=canonical_path):
        measure(ctx)

    assert canonical_path in ctx.read_paths
    assert historical_path not in ctx.read_paths
