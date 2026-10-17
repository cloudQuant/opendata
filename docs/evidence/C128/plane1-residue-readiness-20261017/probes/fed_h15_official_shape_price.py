"""Wave-28 step 4 instrument: price the H.15 long-form leg against the official bodies on disk.

The row this step lands (``federal_reserve::YieldCurve``) was withheld by wave 8 because of a
look-alike contract class, and the retarget fixes only that. This probe asks the harder question a
fixture typed from the reader's own constants cannot answer: **does the shipped reader accept the
body the Board actually publishes?** The repo holds five H.15 CSV bodies from two staging rounds,
and the answer is measured off those bytes -- HEAD's reader versus the worktree's reader, same
payload, no socket opened, nothing invented.

Two facts come out of the captures, and they are not the same finding:

* the fifth preamble label is published as ``Unique Identifier: `` with a trailing space, so the
  byte-exact compare in ``_PREAMBLE_LABELS`` refuses every official body that carries it;
* the full 12-series package additionally publishes a thirteenth column (``RIFSGFSW04_N.B``, the
  4-week tenor that belongs to the ``TreasuryRates`` spec and not to this long-form contract), so
  its header is 13 cells against the reader's 12-cell exact compare.

The first is fixed in this step, because three bodies print it and the fix still refuses a
genuinely wrong label. The second is NOT fixed here: whether the long-form domain should ignore a
trailing published column is a contract ruling, not a whitespace repair, so this probe only prices
it (arm G) and the row is landed on the 11-series official selection the captures also prove.

The probe is read-only: arm F flips ``row_model`` to the look-alike in memory to show the repo's
own guard can see the slip, and restores it in the same block.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO))

from opendata.data.domains import contract_model  # noqa: E402
from opendata.data.models.federal_reserve_clean_documents import (  # noqa: E402
    FederalReserveYieldCurveObservation,
)
from opendata.data.models.federal_reserve_macro import (  # noqa: E402
    FederalReserveYieldCurve,
)
from opendata.data.providers.federal_reserve._iter2_client import (  # noqa: E402
    FederalReserveSourceError,
)
from opendata.data.providers.federal_reserve.models import (  # noqa: E402
    yield_curve as worktree_reader,
)

_YIELD_PATH = "opendata/data/providers/federal_reserve/models/yield_curve.py"
_DOMAIN = "federal_reserve_yield_curve"
_OBSERVATION = FederalReserveYieldCurveObservation
_PADDED_FIFTH = "Unique Identifier: "

#: The bodies on disk. ``kind`` is what the *file name in its own run directory* claims it is:
#: ``official-*`` and the staging-root capture came off the source, the ``synthetic-*`` and the
#: materialized fixture trace to a synthetic response file. The pairing of one official body with a
#: ``synthetic-normalized`` twin of the same rows is what makes the trailing space a captured fact
#: rather than a guess about how the Board prints its labels.
CAPTURES: dict[str, tuple[str, str]] = {
    "source_capture": (
        "capture",
        "docs/evidence/C100/model-current-retakes/fed-eight/"
        "product-patch-staging-20261013T111344Z/h15-source-capture.csv",
    ),
    "official_11_series_a": (
        "official",
        "docs/evidence/C100/acceptance-quality-preservation/federal-reserve/runs/"
        "20261012T170302Z_7a5afafc/fixtures/official-h15-yield-curve-11-series.csv",
    ),
    "official_11_series_b": (
        "official",
        "docs/evidence/C100/acceptance-quality-preservation/federal-reserve/runs/"
        "20261012T170511Z_611f245a/fixtures/official-h15-yield-curve-11-series.csv",
    ),
    "synthetic_normalized": (
        "synthetic",
        "docs/evidence/C100/acceptance-quality-preservation/federal-reserve/runs/"
        "20261012T170511Z_611f245a/fixtures/synthetic-normalized-h15-yield-curve-11-series.csv",
    ),
    "materialized_fixture": (
        "synthetic",
        "docs/evidence/C100/model-current-retakes/fed-eight/"
        "product-patch-staging-20261013T111344Z/fixtures/h15.csv",
    ),
}


class Shape(NamedTuple):
    """One body as bytes plus the three shape facts the reader is asked to accept or refuse."""

    tag: str
    kind: str
    raw: bytes
    labels: list[str]
    padded_fifth: bool
    header_cells: int
    header_equals_reader: bool
    data_rows: int
    sha16: str


def _sha16(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _rows_of(raw: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def _load_head_reader() -> ModuleType:
    """Import HEAD's copy of the reader under a name that cannot shadow the worktree module."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    source = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled
        [git, "-C", str(REPO), "show", f"HEAD:{_YIELD_PATH}"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)  # noqa: SIM115
    try:
        handle.write(source)
    finally:
        handle.close()
    staged = Path(handle.name)
    try:
        spec = importlib.util.spec_from_file_location("w28s4_head_yield_curve", staged)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load HEAD reader")
        module = importlib.util.module_from_spec(spec)
        # pydantic resolves a model's annotations through sys.modules[__name__], so the stand-in
        # has to be registered before exec_module or HEAD's YieldCurveQuery never finishes building.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        staged.unlink()
        sys.modules.pop("w28s4_head_yield_curve", None)
    return module


def _read(reader: ModuleType, payload: bytes, window: dict[str, str]) -> list[object]:
    fetcher = reader.FederalReserveYieldCurveFetcher()
    params = reader.YieldCurveQuery.model_validate(window)
    return list(fetcher.transform_data(payload, params))


def _outcome(read: Callable[[bytes], list[object]], payload: bytes) -> str:
    """Either ``PARSED:<n>`` or the typed refusal code, so the two legs stay comparable."""
    try:
        return f"PARSED:{len(read(payload))}"
    except FederalReserveSourceError as exc:
        return f"REFUSED:{exc.code}"


def _relabeled(shape: Shape, label: str, replacement: str) -> bytes:
    """Swap one preamble label for a text the release does not print, keeping its padding."""
    rows = [row[:] for row in _rows_of(shape.raw)]
    touched = 0
    for row in rows[:5]:
        if row and row[0].strip() == label:
            row[0] = replacement + (" " if row[0].endswith(" ") else "")
            touched += 1
    if touched != 1:
        raise RuntimeError(f"label {label!r} found {touched} times")
    return ("\r\n".join(",".join(row) for row in rows) + "\r\n").encode()


def main() -> int:
    """Print the seven arms and return 1 if any arm's expected face did not hold."""
    head_reader = _load_head_reader()
    failed: list[str] = []
    window = {"start_date": "1962-01-01", "end_date": "2026-12-31"}
    per_row = len(worktree_reader._MATURITIES)

    if not (REPO / "opendata/data/domains.yaml").is_file():
        raise RuntimeError(f"repo root resolved to {REPO}, which is not this checkout")
    missing = [tag for tag, (_k, relative) in CAPTURES.items() if not (REPO / relative).is_file()]
    if missing:
        raise RuntimeError(f"bodies missing on disk: {missing}")

    print(
        f"subject={_YIELD_PATH} domain={_DOMAIN} head_module={head_reader.__name__} "
        f"repo={REPO.name} reader_cells={len(worktree_reader._HEADER)} "
        f"rows_per_observation={per_row} padded_fifth={_PADDED_FIFTH!r}"
    )

    # -- arm A: the bodies as bytes, before any leg is asked to interpret them.
    shapes: dict[str, Shape] = {}
    for tag, (kind, relative) in CAPTURES.items():
        raw = (REPO / relative).read_bytes()
        rows = _rows_of(raw)
        shape = Shape(
            tag=tag,
            kind=kind,
            raw=raw,
            labels=[row[0] if row else "" for row in rows[:5]],
            padded_fifth=len(rows) > 4 and rows[4][0] != rows[4][0].strip(),
            header_cells=len(rows[5]) if len(rows) > 5 else 0,
            header_equals_reader=len(rows) > 5 and tuple(rows[5]) == worktree_reader._HEADER,
            data_rows=max(len(rows) - 6, 0),
            sha16=_sha16(raw),
        )
        shapes[tag] = shape
        print(
            f"A_bodies {tag} kind={kind} bytes={len(raw)} sha16={shape.sha16} "
            f"padded_fifth={shape.padded_fifth} header_cells={shape.header_cells} "
            f"header_equals_reader={shape.header_equals_reader} data_rows={shape.data_rows}"
        )
    padded = sorted(tag for tag, shape in shapes.items() if shape.padded_fifth)
    unpadded = sorted(tag for tag, shape in shapes.items() if not shape.padded_fifth)
    official_padded = [tag for tag in padded if shapes[tag].kind in {"capture", "official"}]
    print(
        f"A_census files={len(shapes)} expected={len(CAPTURES)} padded={len(padded)} "
        f"unpadded={len(unpadded)} padded_tags={padded} official_or_capture_padded="
        f"{len(official_padded)}"
    )
    if len(shapes) != len(CAPTURES):
        failed.append("A_census_files")
    if not official_padded or not unpadded:
        failed.append("A_padding_must_be_measured_both_ways")

    # -- arm B: one body through HEAD's leg and the worktree's leg, same bytes, same window.
    for tag, shape in shapes.items():
        head_code = _outcome(lambda payload: _read(head_reader, payload, window), shape.raw)
        work_code = _outcome(lambda payload: _read(worktree_reader, payload, window), shape.raw)
        full = f"PARSED:{shape.data_rows * per_row}"
        print(
            f"B_leg_on_body {tag} kind={shape.kind} head={head_code} worktree={work_code} "
            f"full_parse_would_be={full} moved={head_code != work_code}"
        )
        expected_pair = {
            "source_capture": (
                "REFUSED:FEDERAL_RESERVE_BAD_CSV_PREAMBLE",
                "REFUSED:FEDERAL_RESERVE_BAD_CSV_HEADER",
            ),
            "official_11_series_a": ("REFUSED:FEDERAL_RESERVE_BAD_CSV_PREAMBLE", full),
            "official_11_series_b": ("REFUSED:FEDERAL_RESERVE_BAD_CSV_PREAMBLE", full),
            "synthetic_normalized": (full, full),
            "materialized_fixture": (full, full),
        }[tag]
        if (head_code, work_code) != expected_pair:
            failed.append(f"B_{tag}_face")

    # -- arm C: the guard keeps its teeth after the strip.
    official = shapes["official_11_series_a"]
    for label, replacement in (("Unit:", "Frequency:"), ("Currency:", "Currency")):
        tampered = _relabeled(official, label, replacement)
        code = _outcome(lambda payload: _read(worktree_reader, payload, window), tampered)
        print(
            f"C_guard_kept label={label!r} replaced_with={replacement!r} reads={code} "
            f"tampered_sha16={_sha16(tampered)}"
        )
        if code != "REFUSED:FEDERAL_RESERVE_BAD_CSV_PREAMBLE":
            failed.append("C_strip_loosened_the_preamble_guard")

    # -- arm D: what the rows that now parse actually carry, read off the contract face.
    parsed = cast(
        "list[FederalReserveYieldCurveObservation]",
        _read(worktree_reader, official.raw, window),
    )
    classes = sorted({type(row).__module__ + "." + type(row).__name__ for row in parsed})
    rate_types = sorted({type(row.rate).__name__ for row in parsed})
    nulls = sum(1 for row in parsed if row.rate is None)
    expected_rows = official.data_rows * per_row
    dates = sorted({str(row.date) for row in parsed})
    first_rate = parsed[0].rate if parsed else None
    print(
        f"D_parsed_rows rows={len(parsed)} expected={expected_rows} classes={classes} "
        f"rate_types={rate_types} nulls={nulls} dates={dates} first_rate={first_rate}"
    )
    if classes != [f"{_OBSERVATION.__module__}.{_OBSERVATION.__name__}"]:
        failed.append("D_rows_are_not_the_contract_class")
    if len(parsed) != expected_rows or nulls or not parsed:
        failed.append("D_parsed_face_not_the_derived_row_count")

    # -- arm E: the look-alike pair, which is why the row could hide for eight waves.
    contract_type = contract_model(_DOMAIN)
    macro_fields = tuple(FederalReserveYieldCurve.model_fields)
    contract_fields = tuple(_OBSERVATION.model_fields)
    print(
        f"E_look_alike contract_resolved={contract_type.__module__}.{contract_type.__name__} "
        f"macro_is_contract={FederalReserveYieldCurve is contract_type} "
        f"names_equal={FederalReserveYieldCurve.__name__ == contract_type.__name__} "
        f"macro_fields={macro_fields} contract_fields={contract_fields} "
        f"field_face_equal={macro_fields == contract_fields}"
    )
    if contract_type is not _OBSERVATION:
        failed.append("E_domain_contract_is_not_the_observation_class")
    if macro_fields != contract_fields:
        failed.append("E_look_alike_field_face_assumed_identical")

    # -- arm F: the repo's own guard, plus the in-memory counterfact that it can see a slip.
    fetcher = worktree_reader.FederalReserveYieldCurveFetcher()
    declared_class = type(fetcher)
    shipped = declared_class.row_model
    predicate = shipped is contract_type
    declared_class.row_model = FederalReserveYieldCurve
    try:
        forged = declared_class.row_model is contract_type
    finally:
        declared_class.row_model = shipped
    restored = declared_class.row_model is contract_type
    print(
        f"F_row_model_guard declared={shipped.__module__}.{shipped.__name__} passes={predicate} "
        f"look_alike_forged_fails={not forged} restored_after={restored}"
    )
    if not (predicate and not forged and restored):
        failed.append("F_row_model_guard_cannot_see_the_look_alike")

    # -- arm G: the second refusal is a different cause, so the two findings cannot be pooled.
    capture = shapes["source_capture"]
    capture_header = _rows_of(capture.raw)[5]
    widths = sorted({len(row) for row in _rows_of(capture.raw)[6:]})
    extra = [cell for cell in capture_header if cell not in worktree_reader._HEADER]
    print(
        f"G_full_package_columns header_cells={capture.header_cells} "
        f"reader_cells={len(worktree_reader._HEADER)} data_row_widths={widths} "
        f"columns_the_reader_does_not_declare={extra} "
        f"pinned_url_series_hash=bf17364827e38702b42a58cf8eaa3f78"
    )
    if capture.header_cells == len(worktree_reader._HEADER) or not extra:
        failed.append("G_full_package_header_was_assumed_equal")

    print(
        "fed_h15_official_shape_price_face arms=A,B,C,D,E,F,G "
        f"arms_failed={len(failed)} failed={failed} bodies={len(shapes)} "
        f"contract_class={contract_type.__name__}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
