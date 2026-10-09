"""Guards that the signature-fidelity instrument's refusals and counters can move.

``scripts/quality/ported_signature_fidelity.py`` feeds AC2-04's judged clauses, so a
guard for it has to show each number is reachable in both directions: a pristine tree
can exit 0, one tampered def is named by path, flagging a row as a manual edit
unregisters a difference without erasing it, and a zero-difference reading taken
against an unjoinable checkout is never a pass. Every fixture is a synthetic two-tree
world under ``tmp_path`` -- no vendored tree, no upstream clone, no git, no network.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any, Final

import pytest

from scripts.codemod.port_module import UpstreamLock
from scripts.quality import ported_signature_fidelity as fidelity

if TYPE_CHECKING:
    from pathlib import Path

COMMIT: Final = "1" * 40
OTHER_COMMIT: Final = "2" * 40
UPSTREAM_PACKAGE: Final = "akshare"
LOCK: Final = UpstreamLock(url="https://upstream.invalid/akshare", commit=COMMIT, files={})

ALPHA: Final = (
    "def load(url, *, timeout=3):\n"
    "    return url\n"
    "\n"
    "\n"
    "class Client:\n"
    "    def fetch(self, name):\n"
    "        return name\n"
)
ALPHA_TAMPERED: Final = ALPHA.replace(
    "def load(url, *, timeout=3):",
    "def load(url, *, timeout=3, retries=2):",
)
BETA: Final = "def helper(v):\n    def inner(n):\n        return n\n\n    return inner(v)\n"
GAMMA: Final = "VERSION = 1\n\n\ndef version():\n    return VERSION\n"
UNPARSEABLE: Final = "def (:\n"
TAMPERED_ROW: Final = "pkg/alpha.py"
MISSING_ROW: Final = "pkg/gamma.py"
BROKEN_ROW: Final = "pkg/beta.py"
PRISTINE_ROWS: Final = {
    TAMPERED_ROW: ALPHA,
    BROKEN_ROW: BETA,
    MISSING_ROW: GAMMA,
}
#: A locked record that is not python: it belongs to ``lock_file_records`` and to nothing else.
NON_PYTHON_ROW: Final = "pkg/notes.json"
ALL_ROWS: Final = {**PRISTINE_ROWS, NON_PYTHON_ROW: '{"kept": true}\n'}
PYTHON_RECORDS: Final = len(PRISTINE_ROWS)

ADMISSIBLE: Final = fidelity.Checkout(
    present=True, commit=COMMIT, lock_commit=COMMIT, matches_lock=True, verified=True
)
ABSENT: Final = fidelity.Checkout(
    present=False, commit="", lock_commit=COMMIT, matches_lock=False, verified=False
)


def write_sources(base: Path, sources: dict[str, str]) -> None:
    """Lay a tiny tree out on disk under ``base``."""
    for relative, text in sources.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def build_world(
    tmp_path: Path,
    *,
    ported_overrides: dict[str, str] | None = None,
    manual_edits: tuple[str, ...] = (),
    drop_pristine: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Return ``(upstream_repo, ported_root)`` for a lock and the two trees it joins."""
    upstream_repo = tmp_path / "clone"
    ported_root = tmp_path / "vendor"
    write_sources(
        upstream_repo / UPSTREAM_PACKAGE,
        {name: text for name, text in ALL_ROWS.items() if name not in drop_pristine},
    )
    write_sources(ported_root, {**ALL_ROWS, **(ported_overrides or {})})
    rows = [
        {
            "path": name,
            "upstream_path": f"{UPSTREAM_PACKAGE}/{name}",
            "sha256": "0" * 64,
            "manual_edits": name in manual_edits,
        }
        for name in sorted(ALL_ROWS)
    ]
    payload = {"version": 1, "upstream": {"url": LOCK.url, "commit": COMMIT}, "files": rows}
    ported_root.mkdir(parents=True, exist_ok=True)
    (ported_root / "upstream.lock").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return upstream_repo, ported_root


def audit_world(
    tmp_path: Path,
    *,
    ported_overrides: dict[str, str] | None = None,
    manual_edits: tuple[str, ...] = (),
    drop_pristine: tuple[str, ...] = (),
    checkout: fidelity.Checkout = ADMISSIBLE,
) -> dict[str, Any]:
    """Run the audit over a synthetic world with the checkout state the test asks for."""
    upstream_repo, ported_root = build_world(
        tmp_path,
        ported_overrides=ported_overrides,
        manual_edits=manual_edits,
        drop_pristine=drop_pristine,
    )
    return fidelity.audit_lock(
        ported_root=ported_root, upstream_repo=upstream_repo, checkout=checkout
    )


def assert_partition(payload: dict[str, Any]) -> None:
    """The compared and not-compared buckets have to add up to the locked python rows."""
    assert (
        payload["rows_compared"] + payload["missing_total"] + payload["parse_errors_total"]
        == payload["python_records"]
    )
    assert payload["rows_equal"] + payload["differing_total"] == payload["rows_compared"]


def admit_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pinned checkout look present, matching and verified without touching git."""
    monkeypatch.setattr(fidelity, "_git_head", lambda repo: COMMIT)
    monkeypatch.setattr(fidelity, "verify_upstream", lambda repo, lock: None)


def test_a_module_function_and_a_same_named_method_cannot_cancel_each_other() -> None:
    source = "def f(a, b):\n    pass\n\n\nclass C:\n    def f(self, a, b):\n        pass\n"

    # Two keys with the same name and the same parameter list: only the enclosing chain keeps
    # them apart, and the class itself is a key, so a naive name+args join would cancel here.
    assert fidelity.signature_keys(source) == Counter(
        {
            ("", "FunctionDef", "f", "a, b"): 1,
            ("", "ClassDef", "C", ""): 1,
            ("C", "FunctionDef", "f", "self, a, b"): 1,
        }
    )


def test_a_class_and_a_zero_arg_function_of_the_same_name_are_not_the_same_key() -> None:
    assert fidelity.signature_keys("class D:\n    pass\n\n\ndef D():\n    pass\n") == Counter(
        {
            ("", "ClassDef", "D", ""): 1,
            ("", "FunctionDef", "D", ""): 1,
        }
    )


def test_a_renamed_nested_helper_moves_the_multiset() -> None:
    original = fidelity.signature_keys(BETA)
    renamed = fidelity.signature_keys(BETA.replace("def inner(", "def deep("))

    assert original == Counter(
        {
            ("", "FunctionDef", "helper", "v"): 1,
            ("helper", "FunctionDef", "inner", "n"): 1,
        }
    )
    assert renamed == Counter(
        {
            ("", "FunctionDef", "helper", "v"): 1,
            ("helper", "FunctionDef", "deep", "n"): 1,
        }
    )
    assert original != renamed


def test_the_same_source_text_gives_the_same_multiset_even_with_a_header() -> None:
    assert fidelity.signature_keys(ALPHA) == fidelity.signature_keys(ALPHA)
    assert fidelity.signature_keys(ALPHA) == fidelity.signature_keys(ALPHA + "# ported\n")


def test_unparseable_bytes_raise_so_the_row_is_not_compared() -> None:
    with pytest.raises(SyntaxError):
        fidelity.signature_keys(UNPARSEABLE)


def test_a_pristine_tree_compares_every_row_and_exits_zero(tmp_path: Path) -> None:
    payload = audit_world(tmp_path)

    assert payload["python_records"] == PYTHON_RECORDS
    assert payload["lock_file_records"] == PYTHON_RECORDS + 1
    assert payload["rows_compared"] == payload["python_records"] == PYTHON_RECORDS
    assert payload["rows_equal"] == PYTHON_RECORDS
    assert payload["differing_total"] == 0
    assert payload["differing_and_not_flagged_manual_total"] == 0
    assert payload["missing_total"] == 0
    assert payload["parse_errors_total"] == 0
    assert payload["checkout_present"] is True
    assert fidelity._exit_code(payload) == fidelity.EXIT_OK
    assert_partition(payload)


def test_one_tampered_ported_signature_is_named_by_path(tmp_path: Path) -> None:
    payload = audit_world(tmp_path, ported_overrides={TAMPERED_ROW: ALPHA_TAMPERED})

    assert payload["rows_compared"] == PYTHON_RECORDS
    assert payload["differing_total"] == 1
    assert payload["differing_paths"] == [TAMPERED_ROW]
    assert payload["differing_and_not_flagged_manual_total"] == 1
    assert payload["differing_and_not_flagged_manual"] == [TAMPERED_ROW]
    assert payload["manual_edits_true_rows"] == 0
    assert_partition(payload)
    # ``_exit_code`` measures completeness (admissible checkout + every row compared), not
    # fidelity, so a signature difference the lock does not flag still exits 0. That reading is
    # asserted nowhere here on purpose: the counter above is the load-bearing one for AC2-04.


def test_flagging_the_row_as_manual_keeps_the_difference_and_drops_the_refusal(
    tmp_path: Path,
) -> None:
    unregistered = audit_world(tmp_path, ported_overrides={TAMPERED_ROW: ALPHA_TAMPERED})
    registered = audit_world(
        tmp_path / "flagged",
        ported_overrides={TAMPERED_ROW: ALPHA_TAMPERED},
        manual_edits=(TAMPERED_ROW,),
    )

    assert unregistered["differing_total"] == 1
    assert registered["differing_total"] == 1
    assert registered["differing_paths"] == [TAMPERED_ROW]
    assert unregistered["differing_and_not_flagged_manual_total"] == 1
    assert registered["differing_and_not_flagged_manual_total"] == 0
    assert registered["differing_and_not_flagged_manual"] == []
    assert unregistered["manual_edits_true_rows"] == 0
    assert registered["manual_edits_true_rows"] == 1
    assert fidelity._exit_code(registered) == fidelity.EXIT_OK


def test_an_absent_upstream_dir_is_reported_and_not_admissible(tmp_path: Path) -> None:
    checkout = fidelity.resolve_checkout(tmp_path / "no-such-clone", LOCK)

    assert checkout.present is False
    assert checkout.commit == ""
    assert checkout.lock_commit == COMMIT
    assert checkout.matches_lock is False
    assert checkout.verified is False
    assert checkout.admissible is False
    assert checkout.to_json()["checkout_present"] is False


def test_a_head_that_is_not_the_locked_commit_does_not_match_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fidelity, "_git_head", lambda repo: OTHER_COMMIT)

    checkout = fidelity.resolve_checkout(tmp_path, LOCK)

    assert checkout.present is True
    assert checkout.commit == OTHER_COMMIT
    assert checkout.lock_commit == COMMIT
    assert checkout.matches_lock is False
    assert checkout.admissible is False


def test_a_verify_upstream_refusal_is_printed_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(repo: Path, lock: UpstreamLock) -> None:
        raise RuntimeError("upstream repo is dirty; refusing to port")

    monkeypatch.setattr(fidelity, "_git_head", lambda repo: COMMIT)
    monkeypatch.setattr(fidelity, "verify_upstream", refuse)

    checkout = fidelity.resolve_checkout(tmp_path, LOCK)

    assert checkout.matches_lock is True
    assert checkout.verified is False
    assert checkout.to_json()["checkout_verified_by_port_module"] is False
    assert checkout.admissible is False


def test_an_all_equal_population_under_a_bad_checkout_never_exits_zero(tmp_path: Path) -> None:
    payload = audit_world(tmp_path, checkout=ABSENT)

    assert payload["differing_total"] == 0
    assert payload["differing_and_not_flagged_manual_total"] == 0
    assert payload["checkout_present"] is False
    assert payload["checkout_commit_matches_lock"] is False
    assert payload["checkout_verified_by_port_module"] is False
    assert ABSENT.admissible is False
    assert fidelity._exit_code(payload) == fidelity.EXIT_INCOMPLETE


def test_a_locked_row_with_no_pristine_file_is_counted_as_missing(tmp_path: Path) -> None:
    payload = audit_world(tmp_path, drop_pristine=(MISSING_ROW,))

    assert payload["missing_total"] == 1
    assert payload["missing_paths"] == [MISSING_ROW]
    assert payload["rows_compared"] == PYTHON_RECORDS - 1
    assert payload["python_records"] == PYTHON_RECORDS
    assert fidelity._exit_code(payload) == fidelity.EXIT_INCOMPLETE
    assert_partition(payload)


def test_unparseable_ported_bytes_are_a_parse_error_not_an_equal_row(tmp_path: Path) -> None:
    payload = audit_world(tmp_path, ported_overrides={BROKEN_ROW: UNPARSEABLE})

    assert payload["parse_errors_total"] == 1
    assert payload["parse_errors"] == [BROKEN_ROW]
    assert payload["rows_compared"] == PYTHON_RECORDS - 1
    assert payload["rows_equal"] == PYTHON_RECORDS - 1
    assert payload["differing_total"] == 0
    assert fidelity._exit_code(payload) == fidelity.EXIT_INCOMPLETE
    assert_partition(payload)


def test_the_three_buckets_partition_the_locked_python_rows(tmp_path: Path) -> None:
    payload = audit_world(
        tmp_path,
        drop_pristine=(MISSING_ROW,),
        ported_overrides={BROKEN_ROW: UNPARSEABLE},
    )

    assert payload["python_records"] == PYTHON_RECORDS
    assert payload["lock_file_records"] == PYTHON_RECORDS + 1
    assert payload["rows_compared"] == 1
    assert payload["missing_total"] == 1
    assert payload["parse_errors_total"] == 1
    assert_partition(payload)
    assert fidelity._exit_code(payload) == fidelity.EXIT_INCOMPLETE


def test_main_without_a_lock_prints_one_json_object_and_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = fidelity.main(
        [
            "--ported-root",
            str(tmp_path / "vendor"),
            "--upstream-repo",
            str(tmp_path / "clone"),
        ]
    )
    captured = capsys.readouterr()
    printed = [line for line in captured.out.splitlines() if line.strip()]

    assert code == fidelity.EXIT_INCOMPLETE
    assert fidelity.EXIT_INCOMPLETE == 2
    assert len(printed) == 1
    payload = json.loads(printed[-1])
    assert payload["missing_total"] == 1
    assert payload["missing_paths"] == ["upstream.lock"]
    assert payload["rows_compared"] == 0
    assert payload["differing_total"] == 0
    assert payload["exit_code"] == 2
    assert "could not be read" in captured.err


def test_main_exits_zero_when_the_checkout_and_every_row_are_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, vendor = build_world(tmp_path)
    admit_checkout(monkeypatch)

    code = fidelity.main(["--ported-root", str(vendor), "--upstream-repo", str(repo)])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == fidelity.EXIT_OK
    assert payload["checkout_present"] is True
    assert payload["checkout_commit"] == COMMIT
    assert payload["checkout_commit_matches_lock"] is True
    assert payload["checkout_verified_by_port_module"] is True
    assert payload["rows_compared"] == PYTHON_RECORDS
    assert payload["differing_total"] == 0
    assert payload["exit_code"] == fidelity.EXIT_OK


def test_main_names_a_tampered_row_in_the_object_it_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, vendor = build_world(tmp_path, ported_overrides={TAMPERED_ROW: ALPHA_TAMPERED})
    admit_checkout(monkeypatch)

    code = fidelity.main(["--ported-root", str(vendor), "--upstream-repo", str(repo)])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == int(payload["exit_code"])
    assert payload["differing_total"] == 1
    assert payload["differing_paths"] == [TAMPERED_ROW]
    assert payload["differing_and_not_flagged_manual"] == [TAMPERED_ROW]
    assert payload["differing_and_not_flagged_manual_total"] == 1
    assert payload["missing_total"] == 0
