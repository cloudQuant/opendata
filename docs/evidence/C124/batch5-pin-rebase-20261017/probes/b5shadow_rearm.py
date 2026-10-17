"""C124: two-sided re-arm for the batch5 per-file byte-pin judge.

The fence lives in ``tests/fixtures/registry_batch5_snapshot.json``
(``source_hashes_sha256``, 31 entries) and is judged by
``tests/test_provider_domains.py::_batch5_identity_snapshot``: line 1363 asserts the fixture
payload digest, then line 1368 re-derives every pinned file from disk.

``arm_A_forged`` rewrites one live pinned digest to ``0``*64 in a shadow fixture and re-pins
the shadow copy's payload literal, so the only gate that can fail is the one under test.
``arm_B_inert`` runs the identical apparatus over the shipped bytes. Both shadow roots place
``opendata/`` and ``docs/`` as symlinks to the real repository, so ``parents[1]`` still measures
real file bytes; nothing under the repository is written by either arm.

Usage: python docs/evidence/C124/batch5-pin-rebase-20261017/probes/b5shadow_rearm.py
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
FIXTURE = REPO / "tests" / "fixtures" / "registry_batch5_snapshot.json"
LITERAL_SITES = ("tests/test_provider_domains.py", "tests/test_pipeline_jobs.py")
HEX = re.compile(r"^[0-9a-f]{64}$")
SHADOW_PARENT = Path("/tmp")  # nosec B108  # noqa: S108


def sha(path: Path) -> str:
    """Return the sha256 hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pins() -> dict[str, str]:
    """Return the pinned source-hash census of the shipped fixture."""
    return json.loads(FIXTURE.read_bytes())["source_hashes_sha256"]


def pick_victim() -> str:
    """Choose the first pinned entry whose digest currently matches disk."""
    for rel, expected in sorted(pins().items()):
        source = REPO / rel
        if source.is_file() and HEX.fullmatch(expected) and sha(source) == expected:
            return rel
    raise SystemExit("verdict: REFUSED - no live pinned entry available to forge")


def build(shadow: Path, victim: str | None) -> dict[str, object]:
    """Materialize one shadow root and return the facts it was built from."""
    if shadow.exists():
        shutil.rmtree(shadow)
    (shadow / "tests" / "fixtures").mkdir(parents=True)
    (shadow / "opendata").symlink_to(REPO / "opendata")
    (shadow / "docs").symlink_to(REPO / "docs")
    conftest = REPO / "tests" / "conftest.py"
    if conftest.exists():
        (shadow / "tests" / "conftest.py").symlink_to(conftest)
    for item in sorted((REPO / "tests" / "fixtures").iterdir()):
        if item.name != FIXTURE.name:
            (shadow / "tests" / "fixtures" / item.name).symlink_to(item)

    shipped = FIXTURE.read_bytes()
    text = shipped.decode("utf-8")
    if victim is None:
        written = shipped
    else:
        old = re.search(rf'"{re.escape(victim)}": "([0-9a-f]{{64}})"', text)
        if old is None:
            raise SystemExit(f"verdict: REFUSED - {victim!r} is not pinned as a hex value")
        needle = f'"{victim}": "{old.group(1)}"'
        if text.count(needle) != 1:
            raise SystemExit(f"verdict: REFUSED - {victim!r} is not a unique hex value")
        written = text.replace(needle, f'"{victim}": "{"0" * 64}"').encode("utf-8")
        if len(written) != len(shipped):
            raise SystemExit("verdict: REFUSED - forge is not byte-length preserving")
    (shadow / "tests" / "fixtures" / FIXTURE.name).write_bytes(written)

    literal_counts = {}
    for site in LITERAL_SITES:
        source_text = (REPO / site).read_text(encoding="utf-8")
        hits = source_text.count(sha(FIXTURE))
        if hits != 1:
            raise SystemExit(f"verdict: REFUSED - {site} carries {hits} payload literals")
        literal_counts[site] = hits
        (shadow / site).parent.mkdir(parents=True, exist_ok=True)
        (shadow / site).write_text(
            source_text.replace(sha(FIXTURE), hashlib.sha256(written).hexdigest()),
            encoding="utf-8",
        )
    return {
        "fixture_sha256": hashlib.sha256(written).hexdigest(),
        "fixture_bytes": len(written),
        "victim": victim,
        "payload_literal_sites": literal_counts,
    }


def run(shadow: Path) -> tuple[int, str, list[str]]:
    """Judge one shadow root and return its exit, verdict line, and bite lines."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            str(shadow / "tests" / "test_provider_domains.py"),
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
            "--tb=line",
        ],
        capture_output=True,
        text=True,
        cwd=str(shadow),
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO),
            "HOME": str(Path.home()),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
    )
    lines = (proc.stdout + proc.stderr).splitlines()
    verdict = [ln.strip() for ln in lines if re.search(r"\d+ (passed|failed|error)", ln)]
    bites = [ln.strip() for ln in lines if "AssertionError" in ln]
    return proc.returncode, (verdict[-1] if verdict else "NO_VERDICT_LINE"), bites


def main() -> None:
    """Print one face per arm, plus the census the arms were built against."""
    live = pins()
    stale = [
        rel
        for rel, expected in sorted(live.items())
        if (REPO / rel).is_file() and sha(REPO / rel) != expected
    ]
    print(
        f"pin_entries={len(live)} stale_before_arms={len(stale)} "
        f"famafrench_pinned_paths={sum('famafrench' in rel for rel in live)}"
    )
    victim = pick_victim()
    print(f"victim={victim} victim_digest_on_disk={sha(REPO / victim)}")
    for label, argument in (("arm_A_forged", victim), ("arm_B_inert", None)):
        shadow = SHADOW_PARENT / f"b5shadow_{label}"
        meta = build(shadow, argument)
        code, reading, bites = run(shadow)
        print(
            f"{label}_exit={code} reading={reading} fixture_sha256={meta['fixture_sha256']} "
            f"fixture_bytes={meta['fixture_bytes']} forged={argument is not None}"
        )
        for bite in bites[:1]:
            print(f"  first_bite: {bite}")


if __name__ == "__main__":
    main()
