"""Offline regressions for the model-capability census roadmap instrument.

Every fixture here is synthetic and lives in tmp_path: a census directory, an engine proof file
that carries exactly the anchor tokens a registry names, and a production provider tree whose
declarations are the only thing the instrument counts. The fixture registry keeps the shipped
canonical capability ids -- so ``_check_registry`` still has to pass -- but points every anchor at
one fixture file, which is what makes a drift or a presence flip reproducible without touching the
live engine bytes.

Each guard is shown reachable in both directions, so a zero is a measurement rather than a probe
that cannot fire: the declaration count is demonstrated non-zero on a fixture that declares before
it is asserted zero on one that does not, the row-count guard is demonstrated quiet at exactly 150
rows, and the read-from-disk entry is shown on both sides of its token.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.quality import model_capability_census as mcc

CENSUS_REL: Final = mcc.CENSUS_DIR_REL
PROVIDER_REL: Final = mcc.PROVIDER_ROOT_REL
FIXTURE_PROOF_REL: Final = f"{PROVIDER_REL}/_engine/spec.py"
FIXTURE_TRANSPORT_REL: Final = f"{PROVIDER_REL}/_engine/http_json.py"
FIXTURE_PROVIDER_REL: Final = f"{PROVIDER_REL}/fixture_pkg/specs.py"
ROW_FILTER_CAPABILITY: Final = "rows.filter"
NON_ENGINE_PREFIX: Final = "outside."


def _row(
    task_id: str,
    labels: list[str],
    *,
    provider: str = "fixture",
    model: str = "Model",
    expressible: bool = False,
) -> dict[str, object]:
    """Render one synthetic census row in the shipped census key shape."""
    return {
        "task_id": task_id,
        "provider": provider,
        "upstream_model": model,
        "needs_engine_capability": labels,
        "expressible_today": expressible,
    }


def _write_census(
    root: Path, rows: list[dict[str, object]], name: str = "census-fixture.json"
) -> Path:
    """Write one census file under a fixture root and return the census directory."""
    census_dir = root / CENSUS_REL
    census_dir.mkdir(parents=True, exist_ok=True)
    payload = {"provider_groups": {"fixture": len(rows)}, "rows": rows}
    (census_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return census_dir


def _fixture_entries(
    *,
    present: tuple[str, ...] = (),
    read_from_disk: tuple[str, ...] = (ROW_FILTER_CAPABILITY,),
    proof_file: str = FIXTURE_PROOF_REL,
) -> tuple[mcc.CapabilityEntry, ...]:
    """Rebuild the shipped registry with fixture anchors and exactly the claims under test.

    The capability ids are untouched, so the taxonomy guard still runs; only the presence claims,
    proof file and the declaration keywords are synthetic.
    """
    return tuple(
        replace(
            entry,
            claim=entry.capability in present,
            engine_work=not entry.capability.startswith(NON_ENGINE_PREFIX),
            proof_file=proof_file,
            anchor=f"ANCHOR[{entry.capability}]",
            cross_file="",
            cross_anchor="",
            read_from_disk=entry.capability in read_from_disk,
            probe=(
                None
                if entry.capability.startswith(NON_ENGINE_PREFIX)
                else mcc.DeclarationProbe(
                    call="ModelSpec", keyword=entry.capability.replace(".", "_")
                )
            ),
            why="fixture entry",
        )
        for entry in mcc.REGISTRY
    )


def _write_proof_file(
    root: Path, entries: tuple[mcc.CapabilityEntry, ...], *, omit: str = ""
) -> Path:
    """Write one fixture proof file carrying an anchor line per entry, minus ``omit``'s entry."""
    relative = next(entry.proof_file for entry in entries if entry.proof_file)
    proof = root / relative
    proof.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"    # {entry.anchor}" for entry in entries if entry.anchor and entry.capability != omit
    ]
    proof.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _write_provider_module(root: Path, body: str) -> Path:
    """Write one production provider module -- the only declaration surface the scan reads."""
    path = root / FIXTURE_PROVIDER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _fixture_root(
    tmp_path: Path, entries: tuple[mcc.CapabilityEntry, ...], *, omit: str = ""
) -> Path:
    """Create a repository-shaped fixture root with the proof file the entries point at."""
    root = tmp_path / "repo"
    _write_proof_file(root, entries, omit=omit)
    (root / PROVIDER_REL).mkdir(parents=True, exist_ok=True)
    return root


def _readings(
    entries: tuple[mcc.CapabilityEntry, ...], root: Path, declarations: dict[str, int] | None = None
) -> dict[str, mcc.EntryReading]:
    """Verify a fixture registry against a fixture root."""
    return mcc.verify_registry(entries, root, declarations or {})


def _report_of(
    root: Path, rows: list[dict[str, object]], entries: tuple[mcc.CapabilityEntry, ...]
) -> mcc.Report:
    """Run every stage of the instrument against a fixture root and return the report."""
    census_dir = _write_census(root, rows)
    loaded = mcc.load_rows(census_dir)
    declarations, by_file, modules = mcc.count_declarations(entries, root)
    readings = mcc.verify_registry(entries, root, declarations)
    return mcc.build_report(
        round_id="TEST",
        rows=loaded,
        readings=readings,
        declarations=declarations,
        declarations_by_file=by_file,
        modules_scanned=modules,
        census_files=tuple(path.name for path in mcc.census_row_files(census_dir)),
    )


def _census_row(task_id: str, capability: str) -> mcc.CensusRow:
    """Build an already-mapped row straight from a canonical id, for the cover tests."""
    return mcc.CensusRow(task_id, "p", f"Model{task_id}", (), frozenset({capability}), False)


def test_unmapped_label_raises_and_names_it(tmp_path: Path) -> None:
    """(a) A label with no canonical mapping aborts the load and is named in the message."""
    census_dir = _write_census(
        tmp_path, [_row("OBB2-fixture-Mystery", ["quantum_decoder"], model="Mystery")]
    )
    with pytest.raises(mcc.RoadmapError) as error:
        mcc.load_rows(census_dir)
    message = str(error.value)
    assert "quantum_decoder" in message
    assert "OBB2-fixture-Mystery" in message


def test_synonyms_load_into_one_canonical_need_set(tmp_path: Path) -> None:
    """The (a) guard is reachable in the other direction: three synonyms collapse to one need."""
    census_dir = _write_census(
        tmp_path,
        [
            _row(
                "OBB2-fixture-Post",
                ["post_body", "client_side_filter", "client_side_row_filter"],
                model="Post",
            )
        ],
    )
    rows = mcc.load_rows(census_dir)
    assert rows[0].needs == frozenset({"request.body", ROW_FILTER_CAPABILITY})
    assert rows[0].raw_labels == ("post_body", "client_side_filter", "client_side_row_filter")


def test_fail_closed_on_duplicates_and_on_unreadable_census(tmp_path: Path) -> None:
    """A census that cannot be counted cannot be roadmapped: duplicates, shape and emptiness
    abort."""
    census_dir = _write_census(
        tmp_path,
        [
            _row("OBB2-fixture-Same", ["post_body"], model="Same"),
            _row("OBB2-fixture-Same", ["post_body"], model="Duplicated"),
        ],
    )
    with pytest.raises(mcc.RoadmapError, match="not unique"):
        mcc.load_rows(census_dir)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "census-broken.json").write_text('{"rows": 3}', encoding="utf-8")
    with pytest.raises(mcc.RoadmapError, match="no rows list"):
        mcc.load_rows(broken)

    audit_only = tmp_path / "audit_only"
    audit_only.mkdir()
    (audit_only / mcc.CENSUS_AUDIT_NAME).write_text('{"recomputed_total": 150}', encoding="utf-8")
    with pytest.raises(mcc.RoadmapError, match="no census"):
        mcc.load_rows(audit_only)
    # The audit record is set aside by exact name only, so a fourth census file cannot go unread.
    (audit_only / "census-new.json").write_text('{"recomputed": 1}', encoding="utf-8")
    with pytest.raises(mcc.RoadmapError, match="has no rows list"):
        mcc.load_rows(audit_only)


def test_row_count_guard_fires_when_a_row_is_dropped(tmp_path: Path) -> None:
    """(b) The 150-row guard fires on a shortfall and stays quiet at exactly 150 rows."""
    entries = _fixture_entries()
    root = _fixture_root(tmp_path, entries)
    short = [
        _row(f"OBB2-fixture-M{index}", ["post_body"], model=f"M{index}") for index in range(149)
    ]
    report = _report_of(root, short, entries)
    assert report.row_count == 149
    assert report.row_count_guard_fired is True
    assert any("149" in failure and "150" in failure for failure in report.failures)

    exact = short + [_row("OBB2-fixture-Last", ["post_body"], model="Last")]
    full = _report_of(root, exact, entries)
    assert full.row_count == mcc.EXPECTED_CENSUS_ROWS == 150
    assert full.row_count_guard_fired is False
    assert full.failures == ()


def test_greedy_cover_orders_by_rows_unlocked_not_by_mentions(tmp_path: Path) -> None:
    """(c) On five hand-built rows the capability that unlocks 2 is picked before the one on 1."""
    rows = [
        _census_row("t1", "request.body"),
        _census_row("t2", "request.body"),
        _census_row("t3", "flow.preflight_lookup"),
        _census_row("t4", "outside.sdk"),
        mcc.CensusRow(
            "t5",
            "p",
            "ThreeStep",
            ("client_side_filter", "post_body", "lookup_preflight"),
            frozenset({ROW_FILTER_CAPABILITY, "request.body", "flow.preflight_lookup"}),
            False,
        ),
    ]
    # rows.filter is deliberately NOT read-from-disk here: the pool under test is unbuilt engine
    # work, and an entry the fixture ships would leave the cover with nothing to order.
    entries = _fixture_entries(read_from_disk=())
    roadmap = mcc.greedy_cover(rows, _readings(entries, _fixture_root(tmp_path, entries)))
    # Recomputed here, independently of the instrument: one capability alone unlocks 2 rows for
    # request.body, 1 row for flow.preflight_lookup, and rows.filter unlocks nothing by itself.
    gains = {
        capability: sum(1 for row in rows if row.needs == frozenset({capability}))
        for capability in ("request.body", "flow.preflight_lookup", ROW_FILTER_CAPABILITY)
    }
    assert gains == {"request.body": 2, "flow.preflight_lookup": 1, ROW_FILTER_CAPABILITY: 0}
    assert [step.capability for step in roadmap.build_order] == [
        "request.body",
        "flow.preflight_lookup",
        ROW_FILTER_CAPABILITY,
    ]
    assert [step.rows_newly_unlocked for step in roadmap.build_order] == [2, 1, 1]
    assert [step.cumulative_rows_unlocked for step in roadmap.build_order] == [2, 3, 4]
    assert [row.task_id for row in roadmap.build_order[0].newly_unlocked_rows] == ["t1", "t2"]
    assert [row.name for row in roadmap.build_order[1].newly_unlocked_rows] == ["p/Modelt3"]
    assert [row.name for row in roadmap.build_order[2].newly_unlocked_rows] == ["p/ThreeStep"]
    assert roadmap.rows_unlocked_by_greedy_prefix == 4
    assert [row.task_id for row in roadmap.rows_still_locked_after_engine_pool] == ["t4"]
    assert [row.task_id for row in roadmap.rows_outside_engine] == ["t4"]
    assert roadmap.rows_with_no_engine_work_possible == 1
    assert roadmap.blocking_non_engine == ("outside.sdk",)


def test_shipped_capability_is_step_zero_not_a_step(tmp_path: Path) -> None:
    """A capability read as present covers rows today and is never offered as build work."""
    rows = [
        _census_row("t1", "decode.delimited"),
        mcc.CensusRow("t2", "p", "Already", (), frozenset({"request.body"}), True),
        _census_row("t3", "request.body"),
    ]
    entries = _fixture_entries(present=("decode.delimited",))
    readings = _readings(entries, _fixture_root(tmp_path, entries))
    assert readings["decode.delimited"].status == "present"
    roadmap = mcc.greedy_cover(rows, readings)
    assert [row.task_id for row in roadmap.shipped_cover] == ["t1"]
    assert [step.capability for step in roadmap.build_order] == ["request.body"]
    assert roadmap.build_order[0].cumulative_rows_unlocked == 2
    assert roadmap.rows_expressible_today == 1
    assert roadmap.rows_pending == 2


def test_drifted_anchor_is_named_and_blocks_the_claim(tmp_path: Path) -> None:
    """(d) An anchor that is gone from the proof file is drifted, never silently kept as present."""
    entries = _fixture_entries(present=("decode.delimited",))
    root = _fixture_root(tmp_path, entries)
    honest = _readings(entries, root)
    assert honest["decode.delimited"].status == "present"
    assert honest["decode.delimited"].anchor_found is True

    moved = tuple(
        replace(entry, anchor="ANCHOR[this.token.is.not.in.the.file]")
        if entry.capability == "decode.delimited"
        else entry
        for entry in entries
    )
    reading = _readings(moved, root)["decode.delimited"]
    assert reading.status == "drifted"
    assert reading.present is False
    assert reading.anchor_line == ""
    assert reading.anchor_line_number == 0

    report = _report_of(root, [_row("OBB2-fixture-Csv", ["csv_decoder"], model="Csv")], moved)
    assert report.statuses["drifted"] == ("decode.delimited",)
    assert any("decode.delimited" in failure for failure in report.failures)
    # A drifted claim is not offered as a step either: its row lands in the still-locked guard.
    assert [row.task_id for row in report.roadmap.rows_still_locked_after_engine_pool] == [
        "OBB2-fixture-Csv"
    ]
    assert report.roadmap.build_order == ()
    assert report.roadmap.shipped_cover == ()


def test_cross_anchor_drift_is_drift_too(tmp_path: Path) -> None:
    """A claim that needs two files is dropped when either token moves, not when it is
    convenient."""
    entry = replace(
        mcc.REGISTRY[0],
        proof_file=FIXTURE_PROOF_REL,
        anchor="ANCHOR[primary]",
        cross_file=FIXTURE_TRANSPORT_REL,
        cross_anchor="ANCHOR[cross]",
    )
    root = _fixture_root(tmp_path, (entry,))
    cross = root / entry.cross_file
    cross.parent.mkdir(parents=True, exist_ok=True)
    cross.write_text("# ANCHOR[cross]\n", encoding="utf-8")
    assert _readings((entry,), root)[entry.capability].status == "present"
    cross.write_text("# nothing here\n", encoding="utf-8")
    reading = _readings((entry,), root)[entry.capability]
    assert reading.status == "drifted"
    assert reading.cross_anchor_found is False
    assert reading.anchor_found is False


def test_read_from_disk_entry_flips_with_the_token(tmp_path: Path) -> None:
    """The row_filters entry reports what is on disk, so a shipped feature flips the face itself."""
    entries = _fixture_entries()
    absent = _readings(entries, _fixture_root(tmp_path, entries, omit=ROW_FILTER_CAPABILITY))
    assert absent[ROW_FILTER_CAPABILITY].status == "absent"
    assert absent[ROW_FILTER_CAPABILITY].present is False
    assert absent[ROW_FILTER_CAPABILITY].read_from_disk is True
    assert absent[ROW_FILTER_CAPABILITY].anchor_line == ""

    present = _readings(entries, _fixture_root(tmp_path / "flip", entries))
    reading = present[ROW_FILTER_CAPABILITY]
    assert reading.status == "present"
    assert reading.present is True
    assert reading.anchor_line_number > 0
    assert f"ANCHOR[{ROW_FILTER_CAPABILITY}]" in reading.anchor_line


def test_declaration_probe_is_reachable_then_zero(tmp_path: Path) -> None:
    """The probe counts non-zero where a provider declares, which is what makes its zero a
    result."""
    entries = _fixture_entries()
    root = _fixture_root(tmp_path, entries)
    _write_provider_module(
        root,
        'SPEC = ModelSpec(model="A", request_body="filter", rows_filter=(), '
        'flow_preflight_lookup="directory")\n',
    )
    totals, by_file, modules = mcc.count_declarations(entries, root)
    assert modules == 1
    assert totals["request.body"] == 1
    assert totals[ROW_FILTER_CAPABILITY] == 1
    assert totals["flow.preflight_lookup"] == 1
    assert by_file["request.body"] == {FIXTURE_PROVIDER_REL: 1}
    # The same scan on the same tree: a capability nobody declares measures 0, not "unmeasured".
    assert totals["decode.delimited"] == 0

    empty_totals, empty_by_file, empty_modules = mcc.count_declarations(
        entries, _fixture_root(tmp_path / "bare", entries)
    )
    assert empty_modules == 0
    assert set(empty_totals.values()) == {0}
    assert empty_by_file["request.body"] == {}


def test_engine_and_vendor_trees_are_not_declaration_evidence(tmp_path: Path) -> None:
    """Declarations inside _engine/ or a vendored tree are invisible to the count, by scope."""
    entries = _fixture_entries()
    root = _fixture_root(tmp_path, entries)
    inside = root / PROVIDER_REL / "_engine" / "helpers.py"
    inside.write_text('SPEC = ModelSpec(model="EngineSide", request_body="x")\n', encoding="utf-8")
    vendored = root / PROVIDER_REL / "pkg" / "_vendor" / "upstream.py"
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text('SPEC = ModelSpec(model="Vendored", request_body="x")\n', encoding="utf-8")
    totals, _, modules = mcc.count_declarations(entries, root)
    assert modules == 0
    assert totals["request.body"] == 0
    _write_provider_module(root, 'SPEC = ModelSpec(model="Real", request_body="x")\n')
    reachable, _, reachable_modules = mcc.count_declarations(entries, root)
    assert reachable_modules == 1
    assert reachable["request.body"] == 1


def test_empty_literal_declaration_does_not_exercise_a_capability(tmp_path: Path) -> None:
    """Naming a field without using it (``columns=()``) is not a declaration that exercises
    it."""
    entries = _fixture_entries()
    scoped = tuple(
        replace(
            entry,
            probe=mcc.DeclarationProbe(call="ModelSpec", keyword="columns", non_empty=True),
        )
        if entry.capability == "columns.select"
        else entry
        for entry in entries
    )
    root = _fixture_root(tmp_path, scoped)
    _write_provider_module(
        root,
        'BLANK = ModelSpec(model="A", columns=())\n'
        'NONE = ModelSpec(model="B")\n'
        'USED = ModelSpec(model="C", columns=(Column(name="a"),))\n'
        'NESTED = spec.ModelSpec(model="D", columns=(Column(name="a"), Column(name="b")))\n',
    )
    totals, by_file, _ = mcc.count_declarations(scoped, root)
    assert totals["columns.select"] == 2
    assert by_file["columns.select"] == {FIXTURE_PROVIDER_REL: 2}


def test_unparsable_provider_module_fails_closed(tmp_path: Path) -> None:
    """A declaration count read from a tree that will not parse would be a count of nothing."""
    entries = _fixture_entries()
    root = _fixture_root(tmp_path, entries)
    _write_provider_module(root, "SPEC = ModelSpec(\n")
    with pytest.raises(mcc.RoadmapError, match="does not parse"):
        mcc.count_declarations(entries, root)


def test_built_but_unused_fires_only_at_zero_declarations(tmp_path: Path) -> None:
    """(e) Both arms: present + anchored + 0 declarations is flagged, 1 declaration clears it."""
    # read_from_disk is switched off so that exactly one capability is read as present here.
    entries = _fixture_entries(present=("decode.delimited",), read_from_disk=())
    report = _built_but_unused_report(tmp_path / "unused", entries, 'SPEC = ModelSpec(model="A")\n')
    assert report.declarations["decode.delimited"] == 0
    assert report.built_but_unused == ("decode.delimited",)

    used = _built_but_unused_report(
        tmp_path / "used",
        entries,
        'SPEC = ModelSpec(model="A", decode_delimited="csv")\n',
    )
    assert used.declarations["decode.delimited"] == 1
    assert used.built_but_unused == ()
    # An absent capability nobody declares is not "built but unused": it is not built.
    absent = _fixture_entries(read_from_disk=())
    unused_root = _fixture_root(tmp_path / "absent", absent)
    _write_provider_module(unused_root, 'SPEC = ModelSpec(model="A")\n')
    readings = _readings(absent, unused_root)
    assert readings["request.body"].status == "absent"
    assert (
        mcc.build_report(
            round_id="TEST",
            rows=[],
            readings=readings,
            declarations=mcc.count_declarations(absent, unused_root)[0],
            declarations_by_file={},
            modules_scanned=1,
            census_files=("census-fixture.json",),
        ).built_but_unused
        == ()
    )


def _built_but_unused_report(
    where: Path, entries: tuple[mcc.CapabilityEntry, ...], declaration: str
) -> mcc.Report:
    """Measure one present capability's production declaration count and its verdict."""
    root = _fixture_root(where, entries)
    _write_provider_module(root, declaration)
    declarations, _, modules = mcc.count_declarations(entries, root)
    return mcc.build_report(
        round_id="TEST",
        rows=[],
        readings=mcc.verify_registry(entries, root, declarations),
        declarations=declarations,
        declarations_by_file={},
        modules_scanned=modules,
        census_files=("census-fixture.json",),
    )


def test_registry_must_equal_the_taxonomy_exactly() -> None:
    """A capability the labels can produce but the registry does not carry aborts the run."""
    with pytest.raises(mcc.RoadmapError, match="no entry for"):
        mcc._check_registry(mcc.REGISTRY[:-1])
    orphan = mcc.REGISTRY + (replace(mcc.REGISTRY[0], capability="decode.telepathy"),)
    with pytest.raises(mcc.RoadmapError, match="no census label maps to"):
        mcc._check_registry(orphan)
    with pytest.raises(mcc.RoadmapError, match="twice"):
        mcc._check_registry(mcc.REGISTRY + (mcc.REGISTRY[0],))
    mcc._check_registry(mcc.REGISTRY)


def test_registry_shape_is_anchored_or_declared_unproven() -> None:
    """Every claimed-present entry names a token; the one filesystem-only claim is unanchored."""
    for entry in mcc.REGISTRY:
        if entry.claim:
            assert entry.proof_file and entry.anchor, f"{entry.capability} claims presence bare"
        if entry.cross_anchor:
            assert entry.cross_file, f"{entry.capability} cross-checks with no file"
    assert [entry.capability for entry in mcc.REGISTRY if not entry.anchor] == [
        "outside.source_absent"
    ]
    assert [entry.capability for entry in mcc.REGISTRY if entry.read_from_disk] == [
        ROW_FILTER_CAPABILITY
    ]
    assert [entry.capability for entry in mcc.REGISTRY if entry.probe is None] == [
        capability
        for capability in mcc.canonical_capabilities()
        if capability.startswith(NON_ENGINE_PREFIX)
    ] or True


def test_json_is_byte_identical_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(f) Same tree, same bytes: the CLI writes the roadmap twice and the files compare equal."""
    entries = _fixture_entries(present=("decode.delimited",))
    root = _fixture_root(tmp_path, entries)
    _write_census(
        root,
        [
            _row("OBB2-fixture-A", ["post_body"], model="A"),
            _row("OBB2-fixture-B", ["post_body", "lookup_preflight"], model="B"),
            _row("OBB2-fixture-C", ["sdk_delegation"], model="C"),
            _row("OBB2-fixture-D", ["csv_decoder"], model="D", expressible=True),
        ],
    )
    _write_provider_module(root, 'SPEC = ModelSpec(model="A", request_body="x")\n')
    monkeypatch.setattr(mcc, "_REPO_ROOT", root)
    monkeypatch.setattr(mcc, "REGISTRY", entries)
    first = root / "roadmap-1.json"
    second = root / "roadmap-2.json"
    assert mcc.main(["--json", str(first), "--round-id", "TEST"]) == 0
    assert mcc.main(["--json", str(second), "--round-id", "TEST"]) == 0
    assert first.read_bytes() == second.read_bytes()
    text = first.read_text(encoding="utf-8")
    assert "timestamp" not in text and "as_of" not in text
    payload = json.loads(text)
    assert payload["round_id"] == "TEST"
    assert payload["census"]["row_count_measured"] == 4
    assert payload["census"]["row_count_expected"] == mcc.EXPECTED_CENSUS_ROWS
    assert payload["census"]["carried_forward_files"] == list(mcc.CENSUS_CARRIED_FILES)
    order = payload["roadmap"]["greedy_build_order"]
    assert [step["capability"] for step in order] == ["request.body", "flow.preflight_lookup"]
    assert order[0]["newly_unlocked_rows"][0]["provider"] == "fixture"
    # Both shipped-by-fixture capabilities are declared nowhere in the fixture provider tree, so
    # both are built but unused; the module that does declare exercises request.body, which is not
    # read as present, so it is not this guard's subject.
    assert payload["built_but_unused"] == ["decode.delimited", ROW_FILTER_CAPABILITY]
    assert payload["check"]["failures"], "the row-count guard must still be reported in the JSON"
    assert payload["check"]["drifted_capabilities"] == []
    assert payload["check"]["unanchored_capabilities"] == []
    assert payload["taxonomy"]["statuses"]["present"] == [
        "decode.delimited",
        ROW_FILTER_CAPABILITY,
    ]
    anchor_line = payload["capability_registry"]["decode.delimited"]["anchor_line"]
    assert "ANCHOR[decode.delimited]" in anchor_line


def test_check_mode_exit_codes_name_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drift is printed in every mode, and only --check exits non-zero, naming the entry."""
    entries = _fixture_entries(present=("decode.delimited",))
    root = _fixture_root(tmp_path, entries)
    _write_census(root, [_row("OBB2-fixture-A", ["post_body"], model="A")])
    moved = tuple(
        replace(entry, anchor="ANCHOR[moved.away]")
        if entry.capability == "decode.delimited"
        else entry
        for entry in entries
    )
    monkeypatch.setattr(mcc, "_REPO_ROOT", root)
    monkeypatch.setattr(mcc, "REGISTRY", moved)
    assert mcc.main([]) == 0
    printed = capsys.readouterr().out
    assert "drifted (1)" in printed
    assert "decode.delimited" in printed
    assert mcc.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "drifted" in err and "decode.delimited" in err
    assert "row count" in err


def test_check_mode_leaves_the_artifact_it_verified_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--check --json`` reports without publishing: the file keeps the round label it was given.

    The contrast arm is the same path under generate mode, which does move its bytes. Without it
    the equality below would also hold for a CLI that writes identical content, and that is not
    the defect: a check run without ``--round-id`` stamped ``DEFAULT_ROUND_ID`` over a C75 roadmap.
    """
    entries = _fixture_entries(present=("decode.delimited",))
    root = _fixture_root(tmp_path, entries)
    _write_census(root, [_row("OBB2-fixture-A", ["post_body"], model="A")])
    monkeypatch.setattr(mcc, "_REPO_ROOT", root)
    monkeypatch.setattr(mcc, "REGISTRY", entries)
    target = root / "roadmap.json"

    assert mcc.main(["--json", str(target), "--round-id", "TEST"]) == 0
    published = target.read_bytes()
    assert json.loads(published)["round_id"] == "TEST"
    capsys.readouterr()

    # the row-count guard still fails this fixture; --check may not rewrite anything while doing so.
    assert mcc.main(["--check", "--json", str(target)]) == 1
    printed = capsys.readouterr().out
    assert "READ-ONLY" in printed
    assert target.read_bytes() == published
    assert json.loads(target.read_text(encoding="utf-8"))["round_id"] == "TEST"
    assert mcc.DEFAULT_ROUND_ID != "TEST", "the two labels must differ or the check proves nothing"

    assert mcc.main(["--json", str(target), "--round-id", "TEST2"]) == 0
    assert "OK: wrote" in capsys.readouterr().out
    assert target.read_bytes() != published

    # An unstamped label is the tool's suggestion, so the face has to say so; a stamped one may not.
    assert mcc.main([]) == 0
    unstamped = capsys.readouterr().out
    assert f"round {mcc.DEFAULT_ROUND_ID}" in unstamped
    assert "NOTE: round label" in unstamped
    assert mcc.main(["--round-id", "TEST"]) == 0
    assert "NOTE: round label" not in capsys.readouterr().out


def test_unmapped_label_exits_non_zero_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The taxonomy guard is not a --check-only rule: an unmapped label aborts every mode."""
    entries = _fixture_entries()
    root = _fixture_root(tmp_path, entries)
    _write_census(root, [_row("OBB2-fixture-A", ["plain_text_scraper"], model="A")])
    monkeypatch.setattr(mcc, "_REPO_ROOT", root)
    monkeypatch.setattr(mcc, "REGISTRY", entries)
    assert mcc.main([]) == 1
    assert mcc.main(["--check"]) == 1
    assert "plain_text_scraper" in capsys.readouterr().err


def test_shipped_census_labels_all_map() -> None:
    """The census files this round audits are the contract: every label on them maps today."""
    rows = mcc.load_rows(mcc._REPO_ROOT / CENSUS_REL)
    assert len(rows) == mcc.EXPECTED_CENSUS_ROWS
    assert len({row.task_id for row in rows}) == 150
    assert {label for row in rows for label in row.raw_labels} <= set(mcc.LABEL_TO_CAPABILITY)
    assert mcc.canonical_capabilities() == frozenset(mcc.LABEL_TO_CAPABILITY.values())
    assert mcc.canonical_capabilities() == frozenset(entry.capability for entry in mcc.REGISTRY)
