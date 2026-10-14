#!/usr/bin/env python3
"""C104: the twelfth H.15 series id is a public identifier, and the enumeration said so eleven times.

``opendata/data/providers/federal_reserve/specs.py:342`` declares ``week_4`` with
``source_key="RIFSGFSW04_N.B"``, the Fed H.15 4-week bill series. ``generic-api-key`` reads the
field NAME ``source_key`` as a key name and the value as the key value, so the line is reported.
C36/C75 closed the same eleven findings by ENUMERATING the eleven ids one by one, refusing a
value-class rule ("anything under a field called source_key") because that would silence a real
credential written under the same kwarg. This probe re-measures that boundary for the twelfth id.

Arms, all off-tree (nothing here writes a tracked file):

  current    repo config exactly as it stands         -> 0 findings, exit 0 (pre-edit: the :342 finding
                                                          and a clean refusal, no traceback)
  dropped    same config minus the one enumerated id  -> the :342 finding must come back
  sibling    unenumerated sibling id, planted         -> still reported under the shipped config
  credential credential-shaped value under source_key= -> still reported under the shipped config

``sibling`` and ``credential`` are the arms that make this an enumeration rather than a relaxation:
if either goes quiet, the added entry is a value class and the gate got weaker.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[5]
CONFIG = REPO / ".gitleaks.toml"
PROBE_CONST = REPO / "scripts/quality/acceptance_item_probe.py"
SPEC_REL = "opendata/data/providers/federal_reserve/specs.py"
SPEC = REPO / SPEC_REL
PAYLOAD_SRC = pathlib.Path("/tmp/c103_scan_payload")  # the exact staged bytes, copied before any edit
NEW_ENTRY = r"^RIFSGFSW04_N\.B$"
SIBLING = "RIFSGFSW08_N.B"

fails: list[str] = []
faces: list[str] = []


def face(line: str) -> None:
    faces.append(line)
    print(line, flush=True)


def refuse(reason: str) -> None:
    fails.append(reason)
    print(f"  REFUSE: {reason}", flush=True)


def scan(source: pathlib.Path, config_text: str, workdir: pathlib.Path) -> tuple[int, list[dict]]:
    """Run gitleaks over one isolated payload with one config; return (exit, findings)."""
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = workdir / "config.toml"
    cfg.write_text(config_text, encoding="utf-8")
    report = workdir / "report.json"
    if report.exists():
        report.unlink()
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [
            "gitleaks",
            "detect",
            "--no-git",
            "--source",
            str(source),
            "--config",
            str(cfg),
            "--redact=20",
            "-f",
            "json",
            "-r",
            str(report),
            "--no-banner",
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if not report.exists():
        return done.returncode, []
    raw = report.read_text(encoding="utf-8").strip()
    return done.returncode, (json.loads(raw) if raw else [])


def identity(findings: list[dict], base: pathlib.Path) -> list[str]:
    """file:line#rule per finding -- never the secret text."""
    out = []
    for f in findings:
        path = str(f.get("File") or "?")
        try:
            path = str(pathlib.Path(path).relative_to(base))
        except ValueError:
            path = pathlib.Path(path).name
        out.append(f"{path}:{f.get('StartLine')}#{f.get('RuleID')}")
    return sorted(out)


def drop_entry(config_text: str) -> str:
    """Remove exactly the line holding NEW_ENTRY, leaving every byte of the others alone."""
    kept = [ln for ln in config_text.splitlines(keepends=True) if NEW_ENTRY not in ln]
    if len(kept) == len(config_text.splitlines(keepends=True)):
        refuse("NEW_ENTRY is not in the repo config; the `pre` arm cannot be a removal")
    return "".join(kept)


def api_key_regexes(config_text: str) -> list[str]:
    """The shipped config's generic-api-key allowlist, parsed the way the gate's judge parses it."""
    parsed = tomllib.loads(config_text)
    for rule in parsed.get("rules", []):
        if isinstance(rule, dict) and rule.get("id") == "generic-api-key":
            allow = rule.get("allowlist", {})
            if allow.get("regexTarget") != "secret":
                refuse("generic-api-key allowlist is not regexTarget=secret")
            return list(allow.get("regexes", []))
    refuse("no generic-api-key rule in the shipped config")
    return []


def const_regexes(probe_text: str) -> list[str]:
    """Entries of GENERIC_API_KEY_ALLOWED_REGEXES, read out of its own block only."""
    marker = "GENERIC_API_KEY_ALLOWED_REGEXES: Final = ("
    start = probe_text.find(marker)
    if start < 0:
        refuse("the pinned constant is gone from acceptance_item_probe.py")
        return []
    block = probe_text[start + len(marker) :]
    end = block.find("\n)")
    if end < 0:
        refuse("the pinned constant's block does not close")
        return []
    return re.findall(r'^\s+r"(\^[^"]*)",\s*$', block[:end], re.M)


def main() -> int:
    for path, what in ((CONFIG, "config"), (PROBE_CONST, "probe_const"), (SPEC, "spec")):
        if not path.is_file():
            print(f"face: {what}_not_found_at={path}", flush=True)
            return 1
    work = pathlib.Path(tempfile.mkdtemp(prefix="c104_"))
    shipped = CONFIG.read_text(encoding="utf-8")
    spec_text = SPEC.read_text(encoding="utf-8")

    # --- denominators, measured before any verdict -------------------------------------------
    regexes = api_key_regexes(shipped)
    consts = const_regexes(PROBE_CONST.read_text(encoding="utf-8"))
    face(f"face: probe_const_allowed_regexes={len(consts)} config_regexes={len(regexes)}")
    face(f"face: probe_const_equals_config_regexes={consts == regexes}")
    if consts != regexes:
        refuse("acceptance_item_probe.GENERIC_API_KEY_ALLOWED_REGEXES and the shipped config's "
               "generic-api-key allowlist disagree -- tests/test_acceptance_item_probe.py:715 "
               "asserts exactly this equality")
    entry_present = NEW_ENTRY in regexes
    face(f"face: twelfth_id_enumerated={entry_present}")
    ids = sorted(set(re.findall(r'source_key="([^"]+)"', spec_text)))
    dotted = [i for i in ids if re.fullmatch(r"[A-Z0-9_]+(?:\.[A-Z0-9_]+)+", i)]
    face(f"face: spec_distinct_source_key={len(ids)} series_id_shaped={len(dotted)}")
    face(f"face: series_ids_enumerated_in_config="
         f"{sum(1 for i in dotted if any(re.fullmatch(rx, i) for rx in regexes))} of {len(dotted)}")
    if not PAYLOAD_SRC.is_dir():
        refuse(f"{PAYLOAD_SRC} is gone, so the arms would not be scanning the staged bytes")
        return 1
    staged_n = sum(1 for p in PAYLOAD_SRC.rglob("*") if p.is_file())
    face(f"face: staged_payload_files={staged_n}")

    # --- arm current: the config exactly as it stands, over the exact staged bytes -------------
    code, findings = scan(PAYLOAD_SRC, shipped, work / "current")
    ids_found = identity(findings, PAYLOAD_SRC)
    face(f"arm current: exit={code} findings={len(findings)} identities={ids_found}")
    if not entry_present:
        refuse("the repo config does not carry the enumerated twelfth id -- this is the pre-edit "
               "state; the arms below only judge a config that has it")
        if ids_found != [f"{SPEC_REL}:342#generic-api-key"]:
            refuse(f"the live finding is not the 4-week bill line: {ids_found}")
        shutil.rmtree(work, ignore_errors=True)
        face(f"face: workdir_removed={not work.exists()}")
        face(f"verdict: REFUSED failures={len(fails)}")
        return 1
    if findings:
        refuse(f"the shipped config still reports {ids_found} on the staged bytes")

    # --- arm dropped: one line less, the finding must come back -------------------------------
    pre_cfg = drop_entry(shipped)
    face(f"face: dropped_cfg_is_config_minus_one_line="
         f"{len(pre_cfg.splitlines()) == len(shipped.splitlines()) - 1}")
    dcode, dfind = scan(PAYLOAD_SRC, pre_cfg, work / "dropped")
    dids = identity(dfind, PAYLOAD_SRC)
    face(f"arm dropped: exit={dcode} findings={len(dfind)} identities={dids}")
    if dcode != 1:
        refuse(f"arm dropped exit {dcode}, wanted 1 -- removing the entry did not re-open the finding")
    if dids != [f"{SPEC_REL}:342#generic-api-key"]:
        refuse(f"arm dropped flagged something other than the 4-week bill line: {dids}")

    # --- arms sibling / credential: post config, planted single-file payloads ----------------
    # Segments, never one literal: an intact high-entropy token in this file would be its own
    # finding (the .gitleaks.toml header records exactly that accident at commit 5e7c81a2e39d).
    token = "".join(["z4Q9", "kM2pL7vR8tY3wN6bH1", "cV5xK0jD7sA2fG9hJ4", "kL8pQ3rT6yU0iO5eW7zX1cV"])
    for name, body in (
        ("sibling", spec_text.replace("RIFSGFSW04_N.B", SIBLING)),
        ("credential", spec_text.replace('source_key="RIFSGFSW04_N.B"', f'source_key="{token}"')),
    ):
        payload = work / f"plant_{name}"
        payload.mkdir()
        (payload / SPEC_REL).parent.mkdir(parents=True, exist_ok=True)
        (payload / SPEC_REL).write_text(body, encoding="utf-8")
        code, findings = scan(payload, shipped, work / name)
        ids_found = identity(findings, payload)
        face(f"arm {name}: exit={code} findings={len(findings)} identities={ids_found}")
        if code != 1 or not findings:
            refuse(f"arm {name} went quiet -- the added entry is a value class, not an enumeration")
        if not any("#generic-api-key" in i for i in ids_found):
            refuse(f"arm {name} reported without the rule under test: {ids_found}")

    shutil.rmtree(work, ignore_errors=True)
    face(f"face: workdir_removed={not work.exists()}")
    if fails:
        face(f"verdict: REFUSED failures={len(fails)}")
        return 1
    face("verdict: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
