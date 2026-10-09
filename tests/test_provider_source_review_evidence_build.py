"""Offline regressions for the C65 provider-source-review evidence builder.

Every fixture is synthetic and lives in ``tmp_path``: a five-provider tree matching the pinned
OpenBB baseline package set, a fake OpenBB checkout whose git HEAD files (read, never written)
resolve to the commit the judge pins, and reviewer-authored findings JSON. The bundle is built
into a scratch directory inside the temp tree, then staged by copy into that temp tree's own
``docs/evidence/C65`` -- the repository's real archives are never touched. Two of these tests
exist to prove the judge still bites after a build: a one-byte source edit and a dropped
manifest entry must each turn ``validate()`` red with the exact digest/set-mismatch codes.
No verdict is ever invented here: the no-findings arm asserts ``reviewed: false`` survives.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

import pytest

from scripts.quality import provider_source_review_evidence as review
from scripts.quality import provider_source_review_evidence_build as builder

if TYPE_CHECKING:
    from pathlib import Path

PROVIDERS: Final = ("ecb", "fred", "imf", "oecd", "yfinance")
PROVIDER_ROOT: Final = "opendata/data/providers"
#: Fixed so freshness is deterministic: Madrid local time is 12:00 on the same day.
STAMP: Final = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
NEAREST_BASIS: Final = "ranked pair shows no shared complex implementation"

REGISTRATION: Final = '"""Provider registration (synthetic)."""\n\nPROVIDER = "{name}"\n'

#: Hand-written local source: a >=41-AST-node function kept deliberately below the candidate
#: thresholds against the baseline below (measured ordered_token_ratio 0.38, multiset_dice 0.69).
LOCAL_SOURCE: Final = '''"""Hand-written provider source (synthetic fixture)."""

import json


def fetch_series(client, params):
    payload = {"limit": 50, "window": params.get("window", "1M")}
    raw = client.get_json("/api/v1/series", payload)
    if raw.get("status") != "ok":
        raise ValueError("the series endpoint answered with an error status")
    rows = []
    for record in raw.get("items", []):
        symbol = str(record["symbol"]).upper()
        value = float(record["value"])
        if value > 0.0 and len(symbol) <= 8:
            rows.append({"symbol": symbol, "value": round(value, 4)})
        else:
            rows.append({"symbol": "UNKNOWN", "value": 0.0})
    total = sum(row["value"] for row in rows)
    summary = json.dumps({"rows": len(rows), "total": total})
    return {"payload": payload, "summary": summary, "raw": raw["items"]}
'''

BASELINE_SOURCE: Final = '''"""Baseline module inside the pinned synthetic OpenBB checkout."""

import json


def get_data(connection, query):
    try:
        response = connection.query(query_parameters=query)
    except TimeoutError:
        return []
    matrix = response.matrix
    labels = [item["label"] for item in matrix]
    values = [float(item["value"]) if item["value"] else 0.0 for item in matrix]
    encoded = json.dumps({"labels": labels, "values": values}, sort_keys=True)
    if encoded.count(";") > 100:
        encoded = encoded[:100]
    while len(encoded) < 16:
        encoded += "0"
    return {"body": encoded, "count": len(labels)}
'''

VENDOR_VERBATIM: Final = '"""Vendored verbatim copy (synthetic)."""\n\nPINNED = 1\n'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes().decode("utf-8"))


def _build_tree(work: Path) -> Path:
    root = work / "repo"
    for name in PROVIDERS:
        package = root / PROVIDER_ROOT / name
        package.mkdir(parents=True)
        (package / "registration.py").write_text(REGISTRATION.format(name=name), encoding="utf-8")
        (package / "provider.py").write_text(LOCAL_SOURCE, encoding="utf-8")
    vendor = root / PROVIDER_ROOT / "ecb/_vendor"
    vendor.mkdir(parents=True)
    (vendor / "verbatim.py").write_text(VENDOR_VERBATIM, encoding="utf-8")
    _dump_json(
        vendor / "upstream.lock",
        {
            "files": [
                {"path": "verbatim.py", "sha256": _sha(VENDOR_VERBATIM), "manual_edits": False}
            ]
        },
    )
    return root


def _build_checkout(work: Path) -> Path:
    checkout = work / "OpenBB"
    refs = checkout / ".git/refs/heads"
    refs.mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (checkout / ".git/refs/heads/main").write_text(
        f"{review.EXPECTED_OPENBB_COMMIT}\n", encoding="utf-8"
    )
    for name in PROVIDERS:
        package = checkout / "openbb_platform/providers" / name / f"openbb_{name}"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""Synthetic baseline."""\n', encoding="utf-8")
        (package / "fetch.py").write_text(BASELINE_SOURCE, encoding="utf-8")
    return checkout


def _file_rows(measurement: builder.Measurement) -> list[dict[str, str]]:
    """One approved no-copy finding row per hand-written file of the fixture tree."""
    hand_written = sorted(path for paths in measurement.hand_written.values() for path in paths)
    return [
        {
            "path": relative,
            "verdict": review.EXPECTED_PACKAGE_REVIEW_RESULT,
            "basis": f"line-by-line no-copy observation for {relative.rsplit('/', 1)[-1]}",
            "reviewer": "fixture-reviewer",
        }
        for relative in hand_written
    ]


def _reviewer_payload(files: list[dict[str, Any]], nearest: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": builder.FINDINGS_SCHEMA,
        "reviewer": {
            "name": "fixture-reviewer",
            "kind": "AI",
            "authorization": review.EXPECTED_REVIEW_AUTHORIZATION,
        },
        "files": files,
        "nearest_pair_reviews": nearest,
    }


def _make_findings(
    root: Path, checkout: Path, findings_path: Path, *, rows: bool
) -> list[dict[str, Any]]:
    """Author the findings file in two passes; the ranked pairs come from the builder itself."""
    measurement = builder.measure_tree(root)
    file_rows = _file_rows(measurement) if rows else []
    _dump_json(findings_path, _reviewer_payload(file_rows, []))
    worklist = builder.nearest_pair_worklist(
        root, findings_path=findings_path, openbb_checkout=checkout
    )
    nearest = [
        {**stub, "result": review.EXPECTED_NEAREST_REVIEW_RESULT, "basis": NEAREST_BASIS}
        for stub in worklist
    ]
    _dump_json(findings_path, _reviewer_payload(file_rows, nearest))
    return nearest


def _build(root: Path, checkout: Path, work: Path, *, rows: bool = True) -> dict[str, Any]:
    findings_path = work / "findings.json"
    _make_findings(root, checkout, findings_path, rows=rows)
    out_dir = work / "build/out"
    facts = builder.build_bundle(
        root,
        findings_path=findings_path,
        out_dir=out_dir,
        openbb_checkout=checkout,
        now=STAMP,
    )
    facts["_out_dir"] = out_dir
    return facts


def _stage(root: Path, out_dir: Path) -> Path:
    """Copy the built bundle into the temp tree's own archive location, the way the judge reads."""
    evidence = root / review.SOURCE_REVIEW_REL.parent
    evidence.mkdir(parents=True)
    for relative in (
        review.SOURCE_REVIEW_REL,
        review.SIMILARITY_REL,
        review.NEAREST_REVIEW_REL,
    ):
        (evidence / relative.name).write_bytes((out_dir / relative.name).read_bytes())
    return evidence


def _issue_codes(root: Path) -> list[str]:
    return [issue.code for issue in review.validate(root, now=STAMP).issues]


def test_round_trip_build_is_accepted_by_the_judge(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    facts = _build(root, checkout, tmp_path)
    staged = _stage(root, facts["_out_dir"])

    result = review.validate(root, now=STAMP)
    assert result.issues == []
    assert result.valid
    assert facts["validated"] and facts["validation_issues"] == []
    assert result.facts["provider_names"] == sorted(PROVIDERS)
    assert (staged / "provider-source-review.json").is_file()


def test_single_byte_tamper_turns_validation_red(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    facts = _build(root, checkout, tmp_path)
    _stage(root, facts["_out_dir"])
    assert _issue_codes(root) == []

    source = root / f"{PROVIDER_ROOT}/ecb/provider.py"
    original = source.read_bytes()
    tampered = original.replace(b"UNKNOWN", b"UNKNOWM", 1)
    assert len(tampered) == len(original)
    assert sum(a != b for a, b in zip(original, tampered, strict=True)) == 1
    source.write_bytes(tampered)

    codes = _issue_codes(root)
    assert codes == [
        "review-ecb-hash-mismatch",
        "review-hash-mismatch",
        "similarity-local-hash-mismatch",
    ]


def test_dropping_one_manifest_entry_turns_validation_red(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    facts = _build(root, checkout, tmp_path)
    evidence = _stage(root, facts["_out_dir"])

    review_path = evidence / review.SOURCE_REVIEW_REL.name
    payload = _load_json(review_path)
    dropped = f"{PROVIDER_ROOT}/ecb/provider.py"
    for record in payload["packages"]:
        if record["provider"] == "ecb":
            record["source_files_sha256"].pop(dropped)
    _dump_json(review_path, payload)

    codes = _issue_codes(root)
    assert "review-ecb-file-set-mismatch" in codes
    assert "review-file-set-mismatch" in codes


def test_no_findings_rows_never_marks_a_hand_written_package_reviewed(
    tmp_path: Path,
) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    facts = _build(root, checkout, tmp_path, rows=False)
    evidence = _stage(root, facts["_out_dir"])

    payload = _load_json(evidence / review.SOURCE_REVIEW_REL.name)
    packages = {record["provider"]: record for record in payload["packages"]}
    assert set(packages) == set(PROVIDERS)
    for provider, record in packages.items():
        assert record["hand_written_files"] > 0, provider
        assert record["reviewed"] is False, provider
        assert record["review_result"] == builder.UNREVIEWED_RESULT
        assert record["findings_rows"] == 0
    assert facts["packages_reviewed"] == 0
    assert facts["findings_missing"] == facts["hand_written_files"]

    codes = _issue_codes(root)
    assert codes.count("provider-review-incomplete") == len(PROVIDERS)


def test_findings_naming_unknown_path_refuses_before_writing(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    findings_path = tmp_path / "findings.json"
    _make_findings(root, checkout, findings_path, rows=True)
    payload = _load_json(findings_path)
    payload["files"].append(
        {
            "path": f"{PROVIDER_ROOT}/ecb/never_written.py",
            "verdict": review.EXPECTED_PACKAGE_REVIEW_RESULT,
            "basis": "this path does not exist on disk",
            "reviewer": "fixture-reviewer",
        }
    )
    _dump_json(findings_path, payload)
    out_dir = tmp_path / "refused/out"

    with pytest.raises(builder.BuildError, match="do not exist"):
        builder.build_bundle(
            root,
            findings_path=findings_path,
            out_dir=out_dir,
            openbb_checkout=checkout,
            now=STAMP,
        )
    assert not out_dir.exists()


def test_vendored_requires_lock_digest_match(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    vendor = root / PROVIDER_ROOT / "ecb/_vendor"
    drifted = '"""Drifted copy."""\nDRIFT = 2\n'
    edited = '"""Edited copy."""\nEDITED = 3\n'
    chained = '"""Manifest-chained copy."""\nCHAINED = 4\n'
    upstream_bytes = "upstream pristine bytes"
    (vendor / "verbatim_ok.py").write_text(VENDOR_VERBATIM, encoding="utf-8")
    (vendor / "drifted.py").write_text(drifted, encoding="utf-8")
    (vendor / "edited.py").write_text(edited, encoding="utf-8")
    (vendor / "chained.py").write_text(chained, encoding="utf-8")
    _dump_json(
        vendor / "upstream.lock",
        {
            "files": [
                {"path": "verbatim.py", "sha256": _sha(VENDOR_VERBATIM), "manual_edits": False},
                {"path": "verbatim_ok.py", "sha256": _sha(VENDOR_VERBATIM), "manual_edits": False},
                {
                    "path": "drifted.py",
                    "sha256": _sha("other bytes entirely"),
                    "manual_edits": False,
                },
                {"path": "edited.py", "sha256": _sha(edited), "manual_edits": True},
                {"path": "chained.py", "sha256": _sha(upstream_bytes), "manual_edits": False},
            ]
        },
    )
    _dump_json(
        vendor / "manifest.json",
        {
            "files": [
                {
                    "path": "chained.py",
                    "sha256": _sha(chained),
                    "upstream_sha256": _sha(upstream_bytes),
                    "manual_edits": False,
                }
            ]
        },
    )

    measurement = builder.measure_tree(root)
    vendored = set(measurement.vendored["ecb"])
    hand_written = set(measurement.hand_written["ecb"])
    prefix = f"{PROVIDER_ROOT}/ecb/_vendor/"
    assert vendored == {
        f"{prefix}verbatim.py",
        f"{prefix}verbatim_ok.py",
        f"{prefix}chained.py",
    }
    for hidden in (f"{prefix}drifted.py", f"{prefix}edited.py"):
        assert hidden in hand_written
        assert hidden in measurement.unproven
    assert "match neither" in measurement.unproven[f"{prefix}drifted.py"]
    assert "manual_edits" in measurement.unproven[f"{prefix}edited.py"]
    matching = measurement.records[f"{prefix}verbatim_ok.py"]
    assert matching.vendored and "upstream.lock" in matching.proof
    chained_record = measurement.records[f"{prefix}chained.py"]
    assert chained_record.vendored and "manifest.json" in chained_record.proof


def test_bundle_counts_are_recomputed_from_the_temp_tree(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    facts = _build(root, checkout, tmp_path)
    evidence = _stage(root, facts["_out_dir"])

    disk: dict[str, dict[str, str]] = {}
    for name in PROVIDERS:
        files = sorted((root / PROVIDER_ROOT / name).rglob("*.py"))
        disk[name] = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        }
    total_on_disk = sum(len(files) for files in disk.values())

    payload = _load_json(evidence / review.SOURCE_REVIEW_REL.name)
    for record in payload["packages"]:
        files = disk[record["provider"]]
        assert record["python_files"] == len(files)
        assert record["source_files_sha256"] == files
        assert (
            record["hand_written_files"] + record["lock_pinned_verbatim_files"]
            == record["python_files"]
        )
    assert payload["counts"]["python_files"] == total_on_disk
    assert facts["python_files"] == total_on_disk

    similarity = _load_json(evidence / review.SIMILARITY_REL.name)
    counts = similarity["counts"]
    assert counts["local_provider_python_files"] == total_on_disk
    assert {
        provider: entry["python_files"] for provider, entry in counts["by_provider_local"].items()
    } == {name: len(files) for name, files in disk.items()}
    local_rows = similarity["source_manifests"]["local_files_sha256"]
    assert {row["path"]: row["sha256"] for row in local_rows} == {
        path: digest for files in disk.values() for path, digest in files.items()
    }
    assert facts["hand_written_files"] + facts["vendored_files"] == total_on_disk

    nearest = _load_json(evidence / review.NEAREST_REVIEW_REL.name)
    all_files = {path for files in disk.values() for path in files}
    for row in nearest["nearest_pair_reviews"]:
        assert row["result"] == review.EXPECTED_NEAREST_REVIEW_RESULT
        assert row["basis"] == NEAREST_BASIS
        assert row["local_path"] in all_files


def test_cli_refuses_to_write_into_docs_evidence(tmp_path: Path) -> None:
    root = _build_tree(tmp_path)
    checkout = _build_checkout(tmp_path)
    findings_path = tmp_path / "findings.json"
    _make_findings(root, checkout, findings_path, rows=True)

    exit_code = builder.main(
        [
            "--root",
            str(root),
            "--openbb-root",
            str(checkout),
            "--findings",
            str(findings_path),
            "-o",
            str(root / "docs/evidence/C65"),
        ]
    )
    assert exit_code == 2
    assert not (root / "docs/evidence/C65").exists()
