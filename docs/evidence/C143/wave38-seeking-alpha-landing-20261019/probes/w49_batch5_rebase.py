"""Re-base the batch5 registry fence for the domains waves 37 and 38 added, with a printed delta.

What broke, measured rather than narrated: ``tests/fixtures/registry_batch5_snapshot.json`` is a
tracked fence over the registry census -- 31 byte-pinned sources, six cohort arrays, a counts block
-- and four test files read it and compare it against the live registry:

* ``tests/test_provider_domains.py`` (through the shared helper ``_batch5_identity_snapshot``),
* ``tests/test_pipeline_jobs.py`` (through
  ``TestPartitionMaintenanceJob::test_the_registered_census_reaches_the_ods_layer``),
* ``tests/test_alert_matrix.py``,
* ``tests/test_alert_matrix_model_domains.py``.

Wave 37 landed ``cboe_index_snapshots`` and wave 38 lands ``seeking_alpha_calendar_earnings``;
neither wave moved the fence, so it has been red since wave 37's commit. Wave 37's face recorded the
four ``test_provider_domains`` reds as "pre-existing, identical in both trees", which was wrong: the
control worktree lacked the gitignored ``docs/evidence/C100`` inputs those tests also read, so they
were red there too -- for a different reason. Copying C100 into a fresh worktree of the pre-wave-37
commit reads them green, which is what isolates the real cause.

This instrument does not recompute the census by hand. It restores the committed fixture and runs
the repo's own shipped census drivers over it --
``docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py`` (identity rows, leg arrays,
domain and query-semantic cohorts, in the registry's own key order because
``test_alert_matrix.py`` compares ``tuple(deferred_domain_ids)`` to the live tuple) and
``.../rebase_descriptor_census.py`` (``canonical_ids_declared`` plus the descriptor-eligible
counts) -- then re-pins the drifted byte pins, restores C101's ``iter2_census_rebase`` block
byte-for-byte (the tests read ``registry_census_rebase.approved_delta_conflict`` out of it, and the
first driver rewrites that block), writes this wave's own delta block, and re-bases the test-side
literals in two passes: a mechanical scan rewrites every ``["<count key>"] == <int>`` whose key is a
fenced count (so no count site can be left behind by a hand-typed table), and a second table covers
the derived expressions the scan cannot see -- ``len(known)``, ``scope.deferred_legs``,
``len(expected_ods) + deferred_legs`` -- plus the four sha256 literals the tests pin.

Authority order matters and is copied from the tests: ``alert_matrix.registered_legs()`` and
``warehouse_tables()`` are read *after* ``register_providers()``. An earlier draft of this probe
read them before registering and got 0 legs and 358 dwd tables -- a true reading of an empty
registry that would have "re-based" the warehouse cohort from 53 to 358 and moved three test
literals with it.

Every from-value comes from ``git show HEAD:`` and every to-value from the live tree, so the run is
steady-state: re-running over a tree this instrument already moved prints the same delta and the
same verdict. The HEAD-relative pairs are printed as readings, never armed, because they age at
commit; the arms compare the written fixture and the rewritten test files against live readings.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
FIXTURE_REL = "tests/fixtures/registry_batch5_snapshot.json"
FIXTURE = REPO / FIXTURE_REL
TEST_DOMAINS = "tests/test_provider_domains.py"
TEST_JOBS = "tests/test_pipeline_jobs.py"
TEST_ALERT = "tests/test_alert_matrix.py"
TEST_ALERT_MODELS = "tests/test_alert_matrix_model_domains.py"
TEST_FILES = (TEST_DOMAINS, TEST_JOBS, TEST_ALERT, TEST_ALERT_MODELS)
CARRIER = REPO / BUNDLE / "runs" / "w49_batch5_rebase.txt"
GIT = "/usr/bin/git"  # absolute executable, so bandit sees no partial path
WAVE_DOMAINS = ("cboe_index_snapshots", "seeking_alpha_calendar_earnings")
DRIVERS = (
    "docs/evidence/C101/census-rebase/probes/rebase_batch5_census.py",
    "docs/evidence/C101/census-rebase/probes/rebase_descriptor_census.py",
)
COHORT_KEYS = (
    "domain_ids",
    "deferred_domain_ids",
    "deferred_source_domain_legs",
    "source_domain_legs",
    "query_semantic_domain_ids",
    "provider_model_identities",
)
COUNT_KEYS = (
    "domain_count",
    "query_semantic_domain_count",
    "deferred_domain_count",
    "deferred_source_domain_leg_count",
    "registered_source_domain_leg_count",
    "provider_model_identity_count",
    "descriptor_eligible_identity_count",
    "descriptor_eligible_legacy_identity_count",
    "model_key_only_identity_count",
    "warehouse_table_catalog_count",
    "legacy_dwd_domain_count",
    "legacy_source_domain_leg_count",
)
# The export census lives in two blocks that the tests cross-check against each other and against
# the live ``opendata.data.models.__all__``, so waves 37 and 38 adding two contract models moves it.
EXPORT_KEYS = (
    "all_symbols",
    "contract_model_classes_including_base",
    "non_base_contract_model_classes",
)
FENCED_KEYS = COUNT_KEYS + EXPORT_KEYS

A_DOMAINS_IDENTITIES = 'assert len(snapshot["provider_model_identities"]) == '
A_JOBS_KNOWN = "assert len(known) == "
A_JOBS_DEFERRED_DOMAINS = "assert len(deferred_domains) == "
A_JOBS_ACTUAL_LEGS = "assert len(actual_legs) == "
A_JOBS_DEFERRED_LEGS = "assert len(expected_deferred_legs) == deferred_legs == "
A_JOBS_ODS_PLUS_DEFERRED = "assert len(expected_ods) + deferred_legs == "
A_ALERT_DEFERRED_LEGS = "scope.deferred_legs == len(expected_deferred_legs) == "
A_ALERT_DOMAINS_PLUS_DEFERRED = (
    "scope.domains + len(scope.deferred_domains) == len(expected_domains) == "
)
A_ALERT_LEG_SUM = (
    "scope.source_legs + scope.unmapped_legs + scope.deferred_legs == len(actual_legs) == "
)
A_ALERT_BLOCK_DEFERRED = "== len(expected_deferred_legs)\n            == "
A_ALERT_LEGS = "assert len(legs) == "
A_ALERT_BLOCK_LEGS = "+ len(expected_deferred_legs)\n            == "
A_MODELS_IDENTITIES = "assert len(expected_models) == "
A_MODELS_LEGS_DOMAINS = "assert len(legs) == len(expected_domain_ids) == "
A_MODELS_ACTUAL_LEGS = "assert len(actual_legs) == "
A_MODELS_DEFERRED_LEGS = "scope.deferred_legs == len(expected_deferred_legs) == "
A_MODELS_SCOPE_DICT = 'assert scope_dict["deferred_legs"] == '

NUMBER_SITES: tuple[tuple[str, str, str], ...] = (
    (TEST_DOMAINS, A_DOMAINS_IDENTITIES, "provider_model_identity_count"),
    (TEST_JOBS, A_JOBS_KNOWN, "domain_count"),
    (TEST_JOBS, A_JOBS_DEFERRED_DOMAINS, "deferred_domain_count"),
    (TEST_JOBS, A_JOBS_ACTUAL_LEGS, "registered_source_domain_leg_count"),
    (TEST_JOBS, A_JOBS_DEFERRED_LEGS, "deferred_source_domain_leg_count"),
    (TEST_JOBS, A_JOBS_ODS_PLUS_DEFERRED, "registered_source_domain_leg_count"),
    (TEST_ALERT, A_ALERT_DEFERRED_LEGS, "deferred_source_domain_leg_count"),
    (TEST_ALERT, A_ALERT_DOMAINS_PLUS_DEFERRED, "domain_count"),
    (TEST_ALERT, A_ALERT_LEG_SUM, "registered_source_domain_leg_count"),
    (TEST_ALERT, A_ALERT_BLOCK_DEFERRED, "deferred_source_domain_leg_count"),
    (TEST_ALERT, A_ALERT_LEGS, "domain_count"),
    (TEST_ALERT, A_ALERT_BLOCK_LEGS, "registered_source_domain_leg_count"),
    (TEST_ALERT_MODELS, A_MODELS_IDENTITIES, "provider_model_identity_count"),
    (TEST_ALERT_MODELS, A_MODELS_LEGS_DOMAINS, "domain_count"),
    (TEST_ALERT_MODELS, A_MODELS_ACTUAL_LEGS, "registered_source_domain_leg_count"),
    (TEST_ALERT_MODELS, A_MODELS_DEFERRED_LEGS, "deferred_source_domain_leg_count"),
    (TEST_ALERT_MODELS, A_MODELS_SCOPE_DICT, "deferred_source_domain_leg_count"),
)

H_DOMAINS_SNAPSHOT = 'BATCH5_SNAPSHOT_SHA256 = "'
H_DOMAINS_SOURCE = 'BATCH5_DOMAIN_SOURCE_SHA256 = "'
H_DOMAINS_MODELS = '["opendata/data/models/__init__.py"] == (\n        "'
H_JOBS_SNAPSHOT = 'sha256(snapshot_bytes).hexdigest()\n            == "'
HEX_SITES: tuple[tuple[str, str], ...] = (
    (TEST_DOMAINS, H_DOMAINS_SNAPSHOT),
    (TEST_DOMAINS, H_DOMAINS_SOURCE),
    (TEST_DOMAINS, H_DOMAINS_MODELS),
    (TEST_JOBS, H_JOBS_SNAPSHOT),
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed(rel: str) -> bytes:
    """Read a path from HEAD; a non-zero git exit is a hard failure, never an empty reading."""
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        [GIT, "-C", str(REPO), "show", f"HEAD:{rel}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git show HEAD:{rel} exit={result.returncode} stderr={result.stderr[:200]!r}"
        )
    return result.stdout


def identity_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Identify one census identity row the way the tests do: source, model, domain."""
    return (row["source"], row["model"], row["domain"])


def _live_census() -> dict[str, Any]:
    """Recompute every fenced quantity from the live registry, providers registered first."""
    sys.path.insert(0, str(REPO))
    from opendata.data.domains import dwd_table, load_domains, ods_table
    from opendata.data.providers import register_providers
    from opendata.data.providers.catalog import list_providers
    from opendata.data.registry import ProviderRegistry
    from opendata.pipeline import alert_matrix

    register_providers()
    domains = load_domains()
    qsem = {d for d, spec in domains.items() if spec.semantics_declared}
    legs = alert_matrix.registered_legs()
    leg_order = list(legs)
    leg_pairs = {(source, domain) for domain, sources in legs.items() for source in sources}
    tables = set(alert_matrix.warehouse_tables())

    rows: list[dict[str, Any]] = []
    for provider in list_providers():
        for binding in provider.fetcher_bindings:
            fetcher = getattr(importlib.import_module(binding.module), binding.class_name)
            domain = fetcher.capability.domain
            rows.extend(
                {
                    "binding": f"{binding.module}:{binding.class_name}",
                    "contract": domains[domain].contract,
                    "domain": domain,
                    "model": model,
                    "scenario_present": bool(binding.scenario),
                    "source": provider.source,
                    "verified": bool(fetcher.capability.verified),
                    "canonical_ids_declared": bool(binding.canonical_model_ids),
                }
                for model in binding.canonical_model_ids or (binding.model_key,)
            )
    rows.sort(key=identity_key)

    registry = ProviderRegistry()
    register_providers(registry)
    descriptors = {(d.source, d.model, d.domain) for d in registry.list_model_descriptors()}

    legacy_leg_pairs = {pair for pair in leg_pairs if pair[1] not in qsem}

    # The export census, counted with the same expressions tests/test_provider_domains.py:1867-1877
    # uses: live ``opendata.data.models.__all__`` and ContractModel subclass tests.
    from opendata.data import models as models_module
    from opendata.data.models.base import ContractModel

    exported = [getattr(models_module, name) for name in models_module.__all__]
    contract_count = sum(
        isinstance(item, type) and issubclass(item, ContractModel) for item in exported
    )
    non_base_contract_count = sum(
        isinstance(item, type) and item is not ContractModel and issubclass(item, ContractModel)
        for item in exported
    )

    return {
        "all_symbols": len(models_module.__all__),
        "contract_model_classes_including_base": contract_count,
        "non_base_contract_model_classes": non_base_contract_count,
        "domain_count": len(domains),
        "query_semantic_domain_count": len(qsem),
        "deferred_domain_count": len([d for d in leg_order if d in qsem]),
        "deferred_source_domain_leg_count": len({p for p in leg_pairs if p[1] in qsem}),
        "registered_source_domain_leg_count": len(leg_pairs),
        "provider_model_identity_count": len(rows),
        "descriptor_eligible_identity_count": sum(1 for r in rows if r["canonical_ids_declared"]),
        "descriptor_eligible_legacy_identity_count": sum(
            1 for r in rows if r["canonical_ids_declared"] and r["domain"] not in qsem
        ),
        "model_key_only_identity_count": sum(1 for r in rows if not r["canonical_ids_declared"]),
        "registered_sources_or_descriptors": len(list(list_providers())),
        "warehouse_table_catalog_count": len(tables),
        "legacy_dwd_domain_count": len({d for d in domains if dwd_table(d) in tables}),
        "legacy_source_domain_leg_count": len(legacy_leg_pairs),
        "rows": rows,
        "leg_pairs": leg_pairs,
        "legacy_leg_pairs": legacy_leg_pairs,
        "qsem": qsem,
        "tables": tables,
        "domain_ids": sorted(domains),
        "leg_order": leg_order,
        "deferred_order": [d for d in leg_order if d in qsem],
        "descriptors": descriptors,
        "ods_table": ods_table,
        "dwd_table": dwd_table,
    }


def _number_at(text: str, anchor: str, label: str) -> int:
    """Return the integer written after ``anchor``, requiring the anchor to match exactly once."""
    hits = text.count(anchor)
    if hits != 1:
        raise AssertionError(f"anchor {label} matched {hits} times, expected 1")
    start = text.index(anchor) + len(anchor)
    match = re.match(r" *\d+", text[start:])
    if match is None:
        raise AssertionError(f"anchor {label} is not followed by an integer")
    return int(match.group())


def _rebase_number(text: str, anchor: str, label: str, value: int) -> tuple[str, int]:
    """Rewrite the integer after ``anchor`` to ``value``; a no-op when it is already current."""
    old = _number_at(text, anchor, label)
    head, _sep, tail = text.partition(anchor)
    digits = re.match(r" *\d+", tail)
    if digits is None:
        raise AssertionError(f"anchor {label} lost its integer")
    return head + anchor + str(value) + tail[digits.end() :], old


def _rebase_hex(text: str, anchor: str, label: str, value: str) -> tuple[str, str]:
    """Rewrite the sha256 hex that follows ``anchor``; a no-op when it is already current."""
    hits = text.count(anchor)
    if hits != 1:
        raise AssertionError(f"hex anchor {label} matched {hits} times, expected 1")
    start = text.index(anchor) + len(anchor)
    match = re.match(r"[0-9a-f]{64}", text[start:])
    if match is None:
        raise AssertionError(f"hex anchor {label} is not followed by a sha256 hex")
    old = match.group()
    return text[:start] + value + text[start + len(old) :], old


COUNT_LITERAL = re.compile(
    # The parent must be the counts block itself: a site reached through another key, like
    # snapshot["batch5_count_adjudication"]["counts"]["all_symbols"], is preceded by a quote and is
    # a frozen adjudication, not a live census.
    r'(?<!["\'])(?:snapshot\["counts"\]|snapshot\["candidate_counts"\]|counts|candidate_counts)'
    r'\["(?P<key>[a-z_]+)"\] == (?P<value>\d+)'
)


def _fenced_values(live: dict[str, Any]) -> dict[str, int]:
    """Map every fenced count key, in either block, to this run's measurement."""
    return {key: live[key] for key in FENCED_KEYS}


def _head_fenced_value(head: dict[str, Any], key: str) -> int:
    """Read a fenced count out of the committed fixture, whichever block holds it."""
    block = head["counts"] if key in COUNT_KEYS else head["candidate_counts"]
    return int(block[key])


def _count_literals(text: str) -> list[tuple[str, int]]:
    """Every ``<counts block>["<fenced key>"] == <int>`` a test file holds."""
    return [
        (match.group("key"), int(match.group("value")))
        for match in COUNT_LITERAL.finditer(text)
        if match.group("key") in FENCED_KEYS
    ]


def _rebase_count_literals(text: str, live: dict[str, Any]) -> tuple[str, list[tuple[str, int]]]:
    """Rewrite every fenced count literal to this run's measurement, and report what was seen."""
    seen: list[tuple[str, int]] = []
    values = _fenced_values(live)

    def _replace(match: re.Match[str]) -> str:
        key = match.group("key")
        if key not in FENCED_KEYS:
            return match.group(0)
        seen.append((key, int(match.group("value"))))
        prefix = match.group(0)[: match.start("value") - match.start(0)]
        return f"{prefix}{values[key]}"

    return COUNT_LITERAL.sub(_replace, text), seen


def write_carrier(arms: dict[str, bool], lines: list[str], code: int) -> int:
    """Print and persist the face, including the exit line, whether or not the run completed."""
    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append(
        f"batch5_rebase_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    lines.append(f"w49_batch5_rebase_exit={code}")
    body = "\n".join(lines) + "\n"
    CARRIER.parent.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(
        "# carrier for w49_batch5_rebase.py\n"
        f"# argv: {sys.executable} "
        f"{REPO / BUNDLE / 'probes' / 'w49_batch5_rebase.py'}\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return code


def main() -> int:
    """Run the measurement, but always ship a carrier: a crash mid-face loses the whole record."""
    lines: list[str] = []
    arms: dict[str, bool] = {}
    try:
        return run(arms=arms, lines=lines)
    except Exception as exc:  # an instrument must still ship its own face
        lines.append(f"CRASH {type(exc).__name__}: {exc}")
        return write_carrier(arms=arms, lines=lines, code=1)


def run(arms: dict[str, bool], lines: list[str]) -> int:
    """The measurement body, kept separate so a crash still writes a carrier."""

    def finish(code: int) -> int:
        return write_carrier(arms=arms, lines=lines, code=code)

    head_bytes = _committed(FIXTURE_REL)
    head_snap: dict[str, Any] = json.loads(head_bytes)
    head_texts = {rel: _committed(rel).decode("utf-8") for rel in TEST_FILES}
    worktree_before = _digest(FIXTURE)
    FIXTURE.write_bytes(head_bytes)
    lines.append(
        f"base restored_from=HEAD sha={hashlib.sha256(head_bytes).hexdigest()[:16]} "
        f"worktree_before_sha={worktree_before[:16]}"
    )

    driver_ok = True
    for rel in DRIVERS:
        result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute executables
            [sys.executable, rel],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        lines.append(f"driver {Path(rel).name} exit={result.returncode} stdout_lines={len(output)}")
        lines.extend(f"  driver:: {row}" for row in output[:6])
        driver_ok = driver_ok and result.returncode == 0
    arms["census_drivers_ran_clean"] = driver_ok
    if not driver_ok:
        return finish(1)

    snap: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    live = _live_census()
    # Two blocks are historical records, not live censuses, and the first driver overwrites both:
    # ``iter2_census_rebase`` (the tests read registry_census_rebase.approved_delta_conflict out of
    # it) and ``candidate_counts`` (batch5's reviewed candidate export census, which waves 33 and 34
    # left byte-frozen while they moved ``counts``). ``counts`` is the live registry census.
    snap["iter2_census_rebase"] = copy.deepcopy(head_snap["iter2_census_rebase"])
    driver_candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))["candidate_counts"]
    moved_by_driver = sorted(
        key
        for key in head_snap["candidate_counts"]
        if driver_candidate.get(key) != head_snap["candidate_counts"][key]
    )
    snap["candidate_counts"] = copy.deepcopy(head_snap["candidate_counts"])
    # Of the candidate block, only the three export counts are compared against the live
    # ``opendata.data.models.__all__`` (tests/test_provider_domains.py:1862-1896), so only those
    # move; domain_specs/fixed_identities/fixed_bound/provider_model_legs stay batch5's reviewed
    # arithmetic, which waves 33 and 34 also left in place while moving ``counts``.
    rebase_block = snap["iter2_census_rebase"]
    adjudication = head_snap["batch5_count_adjudication"]["counts"]
    export_moves: list[str] = []
    for key in EXPORT_KEYS:
        from_value = int(head_snap["candidate_counts"][key])
        snap["candidate_counts"][key] = live[key]
        rebase_block["live_counts"][key] = live[key]
        rebase_block["delta_vs_adjudication"][key] = live[key] - int(adjudication[key])
        export_moves.append(
            f"{key} {from_value}->{live[key]} "
            f"delta_vs_adjudication {from_value - int(adjudication[key])}->"
            f"{live[key] - int(adjudication[key])}"
        )
    lines.append(
        f"history_restored iter2_census_rebase_historical_subblocks=byte_equal_to_head "
        f"candidate_counts_keys_the_driver_moved={len(moved_by_driver)} {moved_by_driver}"
    )
    lines.extend(f"export_census {row}" for row in export_moves)

    pin_moved: list[str] = []
    for rel, want in sorted(head_snap["source_hashes_sha256"].items()):
        now = _digest(REPO / rel)
        if now != want:
            pin_moved.append(f"{rel} from={want[:16]} to={now[:16]}")
        snap["source_hashes_sha256"][rel] = now

    counts = snap["counts"]
    head_rows = {identity_key(r): r for r in head_snap["provider_model_identities"]}
    live_rows = {identity_key(r): r for r in live["rows"]}
    moved_rows = [
        (key, sorted(f for f in live_rows[key] if head_rows[key].get(f) != live_rows[key][f]))
        for key in live_rows
        if key in head_rows and head_rows[key] != live_rows[key]
    ]
    head_legs = {(r["source"], r["domain"]) for r in head_snap["source_domain_legs"]}
    head_legacy = {(r["source"], r["domain"]) for r in head_snap["legacy_source_domain_legs"]}
    head_domains = set(head_snap["domain_ids"])
    head_qsem = set(head_snap["query_semantic_domain_ids"])
    head_legacy_domains = set(head_snap["legacy_dwd_domain_ids"])
    head_warehouse = {live["ods_table"](domain, source) for source, domain in head_legacy} | {
        live["dwd_table"](domain) for domain in head_legacy_domains
    }
    identities_added = sorted(set(live_rows) - set(head_rows))
    legs_added = sorted(live["leg_pairs"] - head_legs)

    delta: dict[str, Any] = {
        "scope": (
            "the two model domains waves 37 and 38 added; the legacy/measured cohort, the ods "
            "mapped+unmapped split, the warehouse table catalog, the model-key-only partition and "
            "the provider count are declared frozen below and each is re-checked against the live "
            "registry by this probe"
        ),
        "measured_by": (
            f"from: git show HEAD:{FIXTURE_REL}; to: "
            + " + ".join(Path(driver).name for driver in DRIVERS)
            + " over the live registry, read after register_providers()"
        ),
        "domains_added": sorted(set(live["leg_order"]) - head_domains),
        "domains_removed": sorted(head_domains - set(live["leg_order"])),
        "identities_added": [list(key) for key in identities_added],
        "identities_removed": [list(key) for key in sorted(set(head_rows) - set(live_rows))],
        "identity_fields_moved": len(moved_rows),
        "legs_added": [list(pair) for pair in legs_added],
        "legs_removed": [list(pair) for pair in sorted(head_legs - live["leg_pairs"])],
        "cohort_counts": {
            key: {"from": len(head_snap[key]), "to": len(snap[key])} for key in COHORT_KEYS
        },
        "counts": {
            key: {"from": head_snap["counts"].get(key), "to": counts.get(key)} for key in COUNT_KEYS
        },
        "frozen_verified": {
            "legacy_legs_equal_live": head_legacy == live["legacy_leg_pairs"],
            "legacy_head_count": len(head_legacy),
            "legacy_live_count": live["legacy_source_domain_leg_count"],
            "warehouse_head_set_equal_live": head_warehouse == live["tables"],
            "warehouse_head_count": len(head_warehouse),
            "warehouse_live_count": live["warehouse_table_catalog_count"],
            "legacy_dwd_domains_equal_live": sorted(head_legacy_domains)
            == sorted(d for d in live["domain_ids"] if live["dwd_table"](d) in live["tables"]),
            "model_key_only_head": head_snap["counts"]["model_key_only_identity_count"],
            "model_key_only_live": live["model_key_only_identity_count"],
            "registered_sources_head": head_snap["candidate_counts"][
                "registered_sources_or_descriptors"
            ],
            "registered_sources_live": live["registered_sources_or_descriptors"],
            "query_semantic_added": sorted(live["qsem"] - head_qsem),
            "candidate_counts_restored_keys": moved_by_driver,
        },
        "export_census": {
            "measured_by": (
                "live opendata.data.models.__all__ counted with the same ContractModel subclass "
                "tests tests/test_provider_domains.py:1867-1877 uses; written to both blocks the "
                "tests cross-check (candidate_counts and iter2_census_rebase.live_counts) and the "
                "delta recomputed as live minus batch5_count_adjudication.counts"
            ),
            "keys": {
                key: {
                    "from": head_snap["candidate_counts"][key],
                    "to": live[key],
                    "adjudication": head_snap["batch5_count_adjudication"]["counts"][key],
                }
                for key in EXPORT_KEYS
            },
            "frozen_blocks_equal_head": {
                "batch5_count_adjudication": snap["batch5_count_adjudication"]
                == head_snap["batch5_count_adjudication"],
                "batch4_baseline": snap["batch4_baseline"] == head_snap["batch4_baseline"],
                "batch5_adjudication_counts": snap["iter2_census_rebase"][
                    "batch5_adjudication_counts"
                ]
                == head_snap["iter2_census_rebase"]["batch5_adjudication_counts"],
            },
        },
        "source_pin_delta": {
            "entries_pinned": len(head_snap["source_hashes_sha256"]),
            "entries_changed": len(pin_moved),
            "changed": pin_moved,
        },
        "test_sites": (
            "the test-side literals moved by this re-base are listed in the probe's carrier, not "
            "here: tests/test_provider_domains.py and tests/test_pipeline_jobs.py byte-pin this "
            "fixture's digest, so recording the list inside the fixture would invalidate the very "
            "digest it would have to name"
        ),
        "why_late": (
            "wave 37 landed cboe_index_snapshots without re-basing this fence and wave 38 adds a "
            "second domain on top of the same drift; re-basing a pin a wave moved is the house "
            "rule "
            "(waves 33 and 34 each carry a waveNN_census_rebase block, over 15 and 3 domains)"
        ),
    }
    lines.append(
        f"cohort_delta domains={len(head_domains)}->{len(live['domain_ids'])} "
        f"qsem={len(head_qsem)}->{len(live['qsem'])} "
        f"legs={len(head_legs)}->{len(live['leg_pairs'])} "
        f"identities={len(head_rows)}->{len(live_rows)} fields_moved={len(moved_rows)}"
    )
    lines.append(
        f"frozen legacy_legs={len(head_legacy)}->{live['legacy_source_domain_leg_count']} "
        f"warehouse={len(head_warehouse)}->{live['warehouse_table_catalog_count']} "
        f"model_key_only={delta['frozen_verified']['model_key_only_head']}->"
        f"{live['model_key_only_identity_count']} providers="
        f"{delta['frozen_verified']['registered_sources_head']}->"
        f"{live['registered_sources_or_descriptors']}"
    )
    lines.append(f"pins entries={len(head_snap['source_hashes_sha256'])} changed={len(pin_moved)}")
    lines.extend(f"pin {row}" for row in pin_moved)
    snap["wave37_wave38_census_rebase"] = delta
    FIXTURE.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    fixture_sha = _digest(FIXTURE)
    domains_yaml_sha = _digest(REPO / "opendata/data/domains.yaml")
    models_init_sha = _digest(REPO / "opendata/data/models/__init__.py")
    armed = hashlib.sha256((REPO / "opendata/data/domains.yaml").read_bytes() + b"#\n").hexdigest()
    lines.append(
        f"fixture_sha head={hashlib.sha256(head_bytes).hexdigest()[:16]} "
        f"worktree={fixture_sha[:16]}"
    )

    hex_values = {
        H_DOMAINS_SNAPSHOT: fixture_sha,
        H_DOMAINS_SOURCE: domains_yaml_sha,
        H_DOMAINS_MODELS: models_init_sha,
        H_JOBS_SNAPSHOT: fixture_sha,
    }

    texts = {rel: (REPO / rel).read_text(encoding="utf-8") for rel in TEST_FILES}
    scanned: list[tuple[str, str, int]] = []
    for rel in TEST_FILES:
        texts[rel], seen = _rebase_count_literals(texts[rel], live)
        scanned.extend((rel, key, written_value) for key, written_value in seen)
    lines.append(f"counts_literals_scanned={len(scanned)}")
    for rel, key, written_value in scanned:
        lines.append(f"counts_site {Path(rel).name} {key} was={written_value} -> {live[key]}")
    site_failures: list[str] = []
    applied = 0
    for rel, anchor, quantity in NUMBER_SITES:
        label = f"{Path(rel).name}:{quantity}"
        try:
            texts[rel], old_worktree = _rebase_number(texts[rel], anchor, label, live[quantity])
            _head, old_head = _rebase_number(head_texts[rel], anchor, label, live[quantity])
        except AssertionError as exc:
            site_failures.append(f"{label}: {exc}")
            continue
        applied += 1
        lines.append(f"site {label} head={old_head} worktree={old_worktree} -> {live[quantity]}")
    for rel, anchor in HEX_SITES:
        hex_value = hex_values[anchor]
        label = f"{Path(rel).name}:hex@{anchor.split(chr(10))[0].strip()[:26]}"
        try:
            texts[rel], old_worktree_hex = _rebase_hex(texts[rel], anchor, label, hex_value)
            _head, old_head_hex = _rebase_hex(head_texts[rel], anchor, label, hex_value)
        except AssertionError as exc:
            site_failures.append(f"{label}: {exc}")
            continue
        applied += 1
        lines.append(
            f"site {label} head={old_head_hex[:16]} worktree={old_worktree_hex[:16]} "
            f"-> {hex_value[:16]}"
        )
    for rel in texts:
        (REPO / rel).write_text(texts[rel], encoding="utf-8")
    lines.extend(f"SITE_FAIL {row}" for row in site_failures)

    written = json.loads(FIXTURE.read_text(encoding="utf-8"))
    live_now = _live_census()
    final_texts = {rel: (REPO / rel).read_text(encoding="utf-8") for rel in TEST_FILES}
    fixture_sha_now = _digest(FIXTURE)

    arms["fixture_equals_live_census"] = (
        all(written["counts"][key] == live_now[key] for key in COUNT_KEYS)
        and written["domain_ids"] == live_now["leg_order"]
        and written["deferred_domain_ids"] == live_now["deferred_order"]
        and written["query_semantic_domain_ids"] == sorted(live_now["qsem"])
        and len(written["provider_model_identities"]) == len(live_now["rows"])
        and written["counts"]["descriptor_eligible_identity_count"] == len(live_now["descriptors"])
        and all(written["candidate_counts"][key] == live_now[key] for key in EXPORT_KEYS)
    )
    rebase_written = written["iter2_census_rebase"]
    rebase_head = head_snap["iter2_census_rebase"]
    adjudicated = head_snap["batch5_count_adjudication"]["counts"]
    historical_keys = sorted(set(rebase_head) - {"live_counts", "delta_vs_adjudication"})
    arms["census_rebase_block_restored"] = (
        all(rebase_written[key] == rebase_head[key] for key in historical_keys)
        and "approved_delta_conflict" in rebase_written["registry_census_rebase"]
        and all(rebase_written["live_counts"][key] == live_now[key] for key in EXPORT_KEYS)
        and all(
            rebase_written["delta_vs_adjudication"][key] == live_now[key] - adjudicated[key]
            for key in EXPORT_KEYS
        )
    )
    frozen_candidate_keys = sorted(set(head_snap["candidate_counts"]) - set(EXPORT_KEYS))
    arms["candidate_counts_frozen_except_export"] = all(
        written["candidate_counts"][key] == head_snap["candidate_counts"][key]
        for key in frozen_candidate_keys
    ) and all(written["candidate_counts"][key] == live_now[key] for key in EXPORT_KEYS)
    arms["cohort_delta_attributed"] = (
        delta["domains_added"] == sorted(WAVE_DOMAINS)
        and delta["domains_removed"] == []
        and delta["identities_removed"] == []
        and delta["legs_removed"] == []
        and {key[2] for key in identities_added} == set(WAVE_DOMAINS)
        and {pair[1] for pair in legs_added} == set(WAVE_DOMAINS)
        and moved_rows == []
    )
    frozen = delta["frozen_verified"]
    arms["frozen_cohorts_verified"] = (
        frozen["legacy_legs_equal_live"]
        and frozen["warehouse_head_set_equal_live"]
        and frozen["legacy_dwd_domains_equal_live"]
        and frozen["model_key_only_head"] == live_now["model_key_only_identity_count"]
        and frozen["registered_sources_head"] == live_now["registered_sources_or_descriptors"]
        and sorted(frozen["query_semantic_added"]) == sorted(WAVE_DOMAINS)
        and all(delta["export_census"]["frozen_blocks_equal_head"].values())
    )
    arms["pins_current"] = all(
        _digest(REPO / rel) == pin for rel, pin in written["source_hashes_sha256"].items()
    )
    arms["pin_still_bites"] = armed != _digest(REPO / "opendata/data/domains.yaml")

    site_values_ok = not site_failures
    for rel, anchor, quantity in NUMBER_SITES:
        try:
            current = _number_at(final_texts[rel], anchor, f"{rel}:{quantity}")
        except AssertionError:
            site_values_ok = False
            continue
        site_values_ok = site_values_ok and current == live_now[quantity]
    arms["test_literals_synced"] = (
        site_values_ok
        and f'{H_DOMAINS_SNAPSHOT}{fixture_sha_now}"' in final_texts[TEST_DOMAINS]
        and f'{H_DOMAINS_SOURCE}{domains_yaml_sha}"' in final_texts[TEST_DOMAINS]
        and f'"{models_init_sha}"' in final_texts[TEST_DOMAINS]
        and f'"{fixture_sha_now}"' in final_texts[TEST_JOBS]
    )
    arms["site_table_complete"] = (
        applied == len(NUMBER_SITES) + len(HEX_SITES) and len(scanned) >= 5
    )
    stale_counts = [
        (rel, key, value)
        for rel in TEST_FILES
        for key, value in _count_literals(final_texts[rel])
        if value != live_now[key]
    ]
    moved_keys = {key for key in FENCED_KEYS if _head_fenced_value(head_snap, key) != live[key]}
    covered_keys = {key for _rel, key, _value in scanned}
    lines.append(
        f"counts_coverage moved={sorted(moved_keys)} "
        f"covered={sorted(covered_keys)} uncovered={sorted(moved_keys - covered_keys)}"
    )
    arms["counts_literals_complete"] = not stale_counts and moved_keys <= covered_keys
    lines.extend(f"STALE_COUNT {rel} {key} == {value}" for rel, key, value in stale_counts)

    lines.append(
        f"counterfact_one_byte_appended_digest={armed[:16]} "
        f"differs={armed != domains_yaml_sha} live={domains_yaml_sha[:16]}"
    )
    lines.append(
        f"sites number={len(NUMBER_SITES)} hex={len(HEX_SITES)} applied={applied} "
        f"failures={len(site_failures)}"
    )
    return finish(0 if all(arms.values()) else 1)


if __name__ == "__main__":
    sys.exit(main())
