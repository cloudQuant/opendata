"""Can the tightened clean-room record face credit *anything*? Measure the affirmative arm.

C77 replaced AC-16|01/02's prose-grep record face with a path-gated, digest-pinned schema face
(``canonical_clean_room_records``), and the live tree immediately read ``0 packages / 12 refusals``.
A zero of that shape is worth nothing on its own: it is indistinguishable from a face that can never
credit anything -- which is exactly how the old face looked before it was caught crediting the gap's
own restatement (7 packages over 8 lines, all from ``C64/remaining-items-audit.md`` plus two
``C65/item-readings-*`` dumps of this probe's own output).

So each arm writes one throwaway record in a temp git repo and states the value it must return
before it runs. Every field-level arm leaves the other fields true-as-of-now, so a refusal has one
cause, not two:

* ``affirmative`` -- a record on the schema credits ``demo``. Without this arm the live zero says
  nothing: an always-zero ruler and an honest empty tree read the same.
* ``digest-drift`` -- the record pins a digest the bytes do not have.
* ``stale-census`` -- 覆盖面 writes 3 where the tree holds 2.
* ``negated-conclusion`` -- 「无法证明 demo 无 OpenBB 源码参照」 is a disclosure, not a declaration.
* ``echo-mark`` -- a body carrying 「判据原文：」 is a machine echo.
* ``no-date`` -- 审查日期 left blank.
* ``self-reviewer`` -- 审查人 written as 「本探针」: a round naming itself as the reviewer.
* ``thin-method`` -- 方法与反证 reduced to two characters.
* ``wrong-path`` -- the same conforming text filed outside ``docs/evidence/clean-room/``: the shape
  the old face credited and the new one refuses.
* ``bytes-moved`` -- a record that *was* true when written, then the package moved under it.
* ``restore`` -- after every tamper the untampered record credits again, so a refusal is the
  tamper's doing and never an artifact of the fixture (or a stale memo).

Re-run: ``python3.11 -u docs/evidence/C77/clean-room-record-schema-face.py``. Read-only over the
repo: the fixture tree is a ``tempfile.mkdtemp`` git repo and the live arm only reads.
Face: ``clean-room-record-schema-face.txt``.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scripts.quality import acceptance_item_probe as probe  # noqa: E402

GIT = shutil.which("git")

PKG = "demo"
REGISTRATION = "opendata/data/providers/demo/registration.py"
CLIENT = "opendata/data/providers/demo/client.py"
RECORD_REL = f"{probe.CLEAN_ROOM_RECORD_ROOT}/{PKG}.md"
REVIEWER = "schema-fixture"
METHOD = "逐文件读过 import 与函数体，并与上游同名 provider 的函数清单对照过"
CONCLUSION = f"审查结论：{PKG} 为自研实现，无 OpenBB 源码参照。"


def git(root: Path, *args: str) -> None:
    """Run one literal git command inside the fixture repo."""
    if GIT is None:
        raise RuntimeError("the fixture tree needs git on PATH")
    subprocess.run(  # noqa: S603  # nosec B603
        [GIT, *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def commit(root: Path, message: str) -> None:
    """Record the fixture's current state as one tracked tree."""
    git(root, "add", "-A")
    git(root, "-c", "user.name=f", "-c", "user.email=f@e", "commit", "-qm", message)


def build_fixture() -> Path:
    """A committed tree holding one self-developed package and nothing else."""
    root = Path(tempfile.mkdtemp(prefix="cleanroom-fixture-"))
    git(root, "init", "-q")
    (root / REGISTRATION).parent.mkdir(parents=True, exist_ok=True)
    (root / REGISTRATION).write_text("SPEC = {'name': 'demo'}\n", encoding="utf-8")
    (root / CLIENT).write_text("class Client:\n    pass\n", encoding="utf-8")
    commit(root, "fixture")
    return root


def package_files(root: Path) -> tuple[str, ...]:
    """The fixture package's .py census, read through the repo's own helper."""
    ctx = probe.Context(root=root, doc_items=(), ledger={})
    return tuple(probe.provider_py_files(ctx, PKG))


def record_body(
    root: Path,
    *,
    census: int | None = None,
    digest: str | None = None,
    conclusion: str | None = None,
    date: str = "2026-10-09",
    reviewer: str = REVIEWER,
    method: str = METHOD,
    extra: str = "",
) -> str:
    """One record on the schema, with each field individually movable by the arms.

    ``digest`` defaults to the digest of the bytes on disk *now*, so an arm that changes one field
    changes exactly one field.
    """
    files = package_files(root)
    return (
        f"# {PKG} 清洁室审查档案\n\n"
        f"{extra}"
        f"- 审查日期：{date}\n"
        f"- 审查人：{reviewer}\n"
        f"- 覆盖面：{(len(files) if census is None else census)} 个 py 文件\n"
        f"- 代码面摘要：{(probe.provider_py_digest(root, files) if digest is None else digest)}\n"
        f"- 方法与反证：{method}\n\n"
        f"{conclusion or CONCLUSION}\n"
    )


def write_record(root: Path, body: str, rel: str = RECORD_REL) -> None:
    """Put a body at some path and commit it, so the path the face reads is the path tested."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    commit(root, f"record at {rel}")


def unwrite_record(root: Path) -> None:
    """Delete and un-track the canonical record; the package stays as it was."""
    path = root / RECORD_REL
    if path.exists():
        path.unlink()
    commit(root, "record removed")


def arm(name: str, root: Path, expect: dict[str, Any]) -> bool:
    """Read the face over the fixture as it now stands and compare with the stated expectation."""
    credited, binds, refusals = probe.canonical_clean_room_records(
        probe.Context(root=root, doc_items=(), ledger={}), [PKG]
    )
    got: dict[str, Any] = {
        "credited": credited,
        "binds": binds,
        "reason": " ; ".join(refusals)[:200],
    }
    ok = all(got[key] == value for key, value in expect.items() if key != "reason_in")
    hit = expect.get("reason_in")
    if hit is not None:
        ok = ok and hit in got["reason"]
    print(f"--- arm {name}: {'PASS' if ok else 'FAIL'}")
    for key in sorted(expect):
        if key == "reason_in":
            print(f"    expect reason contains {hit!r}")
        else:
            print(f"    expect {key}={expect[key]!r}  got {got.get(key)!r}")
    print(f"    refusal reason: {got['reason'] or '-'}")
    return ok


def live_face() -> dict[str, Any]:
    """The real tree: what the canonical face credits, and what the legacy face used to credit."""
    ctx = probe.load_context()
    names = probe.provider_packages(ctx)
    credited, binds, refusals = probe.canonical_clean_room_records(ctx, names)
    legacy_pkgs, legacy_binds = probe.legacy_prose_record_face(ctx, names)
    return {
        "provider_pkgs": len(names),
        "recorded_pkgs": len(credited),
        "recorded_binds": binds,
        "record_refusals": len(refusals),
        "refusal_detail": refusals,
        "legacy_record_pkgs": len(legacy_pkgs),
        "legacy_record_binds": legacy_binds,
        "legacy_credited": legacy_pkgs,
    }


def main() -> int:
    """Run every arm against its stated expectation, then print the live tree's reading."""
    root = build_fixture()
    results: dict[str, bool] = {}

    write_record(root, record_body(root))
    results["affirmative"] = arm("affirmative", root, {"credited": [PKG], "binds": 1, "reason": ""})

    write_record(root, record_body(root, digest="0" * 64))
    results["digest-drift"] = arm(
        "digest-drift", root, {"credited": [], "reason_in": "当前字节不符"}
    )

    write_record(root, record_body(root, census=len(package_files(root)) + 1))
    results["stale-census"] = arm("stale-census", root, {"credited": [], "reason_in": "覆盖面写"})

    write_record(root, record_body(root, conclusion="审查结论：无法证明 demo 无 OpenBB 源码参照。"))
    results["negated-conclusion"] = arm(
        "negated-conclusion", root, {"credited": [], "reason_in": "否定式披露不算声明"}
    )

    write_record(root, record_body(root, extra="判据原文：（本档案抄自探针输出）\n"))
    results["echo-mark"] = arm("echo-mark", root, {"credited": [], "reason_in": "机器回声"})

    write_record(root, record_body(root, date=""))
    results["no-date"] = arm("no-date", root, {"credited": [], "reason_in": "审查日期不是 ISO 日"})

    write_record(root, record_body(root, reviewer="本探针"))
    results["self-reviewer"] = arm(
        "self-reviewer", root, {"credited": [], "reason_in": "审查人缺位或是自指"}
    )

    write_record(root, record_body(root, method="看过"))
    results["thin-method"] = arm(
        "thin-method", root, {"credited": [], "reason_in": "方法与反证一栏没写实"}
    )

    unwrite_record(root)
    write_record(root, record_body(root), rel="docs/evidence/C99/demo-notes.md")
    results["wrong-path"] = arm("wrong-path", root, {"credited": [], "reason_in": "没有逐包档案"})
    write_record(root, record_body(root))

    (root / CLIENT).write_text("class Client:\n    pass  # 审后新增的一行\n", encoding="utf-8")
    commit(root, "package moved")
    results["bytes-moved"] = arm("bytes-moved", root, {"credited": [], "reason_in": "当前字节不符"})

    write_record(root, record_body(root))
    results["restore"] = arm("restore", root, {"credited": [PKG], "binds": 1, "reason": ""})

    print("\n=== arms ===")
    failed = sorted(name for name, passed in results.items() if not passed)
    print(f"arms run: {len(results)}, failing: {failed or '-'}")

    print("\n=== live tree (the zero the affirmative arm makes meaningful) ===")
    live = live_face()
    for key in (
        "provider_pkgs",
        "recorded_pkgs",
        "recorded_binds",
        "record_refusals",
        "legacy_record_pkgs",
        "legacy_record_binds",
    ):
        print(f"  {key}: {live[key]}")
    print(f"  legacy credited: {', '.join(live['legacy_credited']) or '-'}")
    print("  refusal detail (verbatim, one line per package):")
    for row in live["refusal_detail"]:
        print(f"    - {row}")

    print("\n=== controls ===")
    print(f"  AFFIRMATIVE-CAN-CREDIT: {results['affirmative']}")
    print(
        "  ZERO-IS-INFORMATIVE: "
        f"{live['recorded_pkgs'] == 0 and results['affirmative'] and results['restore']}"
    )
    print(f"  LEGACY-IS-INFLATED: {live['legacy_record_pkgs'] > live['recorded_pkgs']}")
    if failed or not results["affirmative"]:
        print("REFUTE: an arm disagrees with its stated expectation")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
