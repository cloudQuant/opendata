"""Reconcile FMP runtime bindings against the fixed model-task inventory."""

from __future__ import annotations

import csv
from pathlib import Path

from opendata.data.providers.fmp.provider import PROVIDER

_INVENTORY_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "迭代计划"
    / "迭代2-统一Provider架构与全量能力补齐"
    / "模型级任务清单.csv"
)


def test_fmp_provider_bindings_match_all_fixed_model_identities() -> None:
    """Require exact provider/model coverage without treating binding as acceptance."""
    with _INVENTORY_PATH.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        fixed_ids = {row["upstream_model"] for row in rows if row["provider"] == "fmp"}

    binding_ids = [
        model_id
        for binding in PROVIDER.fetcher_bindings
        for model_id in binding.canonical_model_ids
    ]

    assert len(fixed_ids) == 69
    assert len(binding_ids) == 69
    assert len(binding_ids) == len(set(binding_ids))
    assert set(binding_ids) == fixed_ids
