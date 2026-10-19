"""Refusal-evidence face for wave 35: what each of the 14 remaining rows still needs, measured.

The objective forbids verbal judgments. Plane 1 sits at 336/350, so 14 rows are excluded by
*something* -- and until now the strongest statement I had made about those exclusions was prose
copied out of provider code. This probe replaces that with per-row readings that say which class of
evidence each exclusion rests on, and which class this repo cannot produce offline.

Arms, every one able to fail:

``A_population``
    the shipped census's own action table over the residue, re-derived in a subprocess and required
    to sum to the residue count it prints *and* to have as many buckets as ``4_action_totals``
    declares (a table that drops a bucket is how "7 rows => +7 accepted" once measured 0/7; the
    first take of this probe dropped exactly one bucket and died on the parse).
``B_judge_generation``
    the same-generation judge reading (accepted / identities / claims) plus the census's
    identity_set, so the face cannot describe two different generations.
``C_wsj_ledger_state``
    every wsj row's own columns in the fixed model-task CSV: acceptance status and the two run
    columns. If these read NOT_ASSESSED/NOT_RUN, then no acceptance attempt exists on record --
    which is a different fact from "attempted and refused".
``D_wsj_no_request_site``
    count of network call sites inside the wsj package (requests/httpx/urlopen/Session/Client),
    over the named population of its .py files. Zero means the "HTTP 401" sentence in
    ``NOT_DECLARED`` was produced outside this package's code, so no test in this repo can
    re-derive it.
``E_no_wsj_access_capture``
    read the repo's own response-capture format (``*.headers``, whose first line is a wire status
    line like ``HTTP/2 403``) and attribute each capture to a provider by name. The arm requires
    captures to exist (denominator), refusals to exist (4xx), both capture classes to be present
    (``api_endpoint`` and ``documentation_page``, so the class split is measured not assumed), *no*
    401 capture anywhere, and *no* capture attributed to wsj. Prose that merely says "HTTP 401" is
    counted apart, in roles.
``F_offline_egress_gate``
    the live-leg skip gate exists in the test tree and the token is absent from this environment,
    i.e. confirming the 401 here would require an authorized live probe, not a code change.
``G_ruled_out_reasons_are_citable``
    for each ruled-out row -- the row set comes from the census's own ``row ... action=`` lines, not
    from a list I typed (my typed list was wrong for 5 of 11) -- the shipped guard-check's
    ``_ruled_out`` ledger entry, the specs.py line that carries the model key, and a **tiered**
    capture reading: ``model_endpoint`` / ``model_docs_only`` / ``provider_endpoint`` /
    ``provider_docs_only`` / ``no_capture``. The tier splits on the capture's own ``server:``
    header, because the captures named after a model turn out to be OpenBB documentation pages
    (``server: GitHub.com``) rather than upstream endpoint responses -- a contract review, not an
    access fact. The earlier version of this arm reported a single "capture_backed" count off
    provider-name matching, which made cboe/econdb/intrinio look evidentially equal to what is
    really documentation, and hid that seeking_alpha and wsj have nothing at either level.
``H_forge``
    the falsification arm: take two *real* arm-C row readings, doctor one into "already assessed",
    and require the same predicate arm C uses to go red. An arm that cannot be driven red is not
    measuring.
``I_predicates_can_differ``
    each zero in this face bracketed by a non-zero from the same predicate: ``_ruled_out`` on a
    model that exists vs one that does not, ``_network_sites`` on yfinance (must be non-zero) vs wsj
    (reads zero), and the capture predicate on a planted response vs a planted prose line.

Nothing is written except this probe's carrier.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import importlib.util
import json
import os
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
BUNDLE_REL = "docs/evidence/C139/wave34-tmxyfinance-rest-20261018"
CARRIER = REPO / BUNDLE_REL / "runs" / "w35_refusal_evidence_face.txt"
INVENTORY = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
C128 = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
NETWORK_TOKENS = ("requests.", "httpx", "urlopen", "Session(", "Client(", "curl_cffi")
LIVE_GATE = "OPENDATA_ALLOW_LIVE_E2E"
GREP = "/usr/bin/grep"
PROSE_ONLY = "HTTP 401; no official response schema"
# A response status line as it appears on the wire. The repo's own capture format is the
# ``*.headers`` file whose first line is ``HTTP/2 200``, so the version token is a variable: a regex
# that assumed HTTP/1.x found two files, both ruff source snippets, and missed the capture
# population entirely.
STATUS_LINE_RE = re.compile(r"^HTTP/(?P<version>[0-9.]+)[ \t]+(?P<code>[1-5][0-9]{2})\b")


def _load(name: str, path: Path) -> Any:  # noqa: ANN401  # shipped probes are heterogeneous
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import shipped probe {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _claims_hash() -> str:
    return hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]


def _judge_face() -> dict[str, Any]:
    """Read plane 1 through the repo's own judge, in this process."""
    sys.path.insert(0, str(REPO))
    from scripts.quality import iter2_model_evidence as judge

    document = json.loads(CLAIMS.read_text(encoding="utf-8"))
    claims_sha = _claims_hash()
    report = judge.build_report(
        judge.load_inventory(INVENTORY),
        document,
        claims_sha256=claims_sha,
        claims_path=str(CLAIMS.relative_to(REPO)),
    )
    return {
        "accepted": report["summary"]["offline_acceptance_count"],
        "identities": report["summary"]["identity_count"],
        "claims": report["summary"]["claim_count"],
        "statuses": report["summary"]["status_counts"],
        # same field the shipped census reads, truncated the same way -- this is the inventory's
        # expected identity set, so it gates "two generations", not "which rows are accepted".
        "identity": str(report["inventory"]["expected_identity_set_sha256"])[:16],
        "claims_sha256": claims_sha,
    }


def _census_face() -> dict[str, Any]:
    """Re-run the shipped census as a subprocess and read its own lines back.

    The dict is taken between ``2_actions `` and its `` sums=`` tail and read with
    ``ast.literal_eval``, not split on spaces: the first take split twice, so it discarded
    ``{'implement_missing_model': 1,`` and json-decoded the remainder, which raised "Extra data" and
    killed the run before any face line printed. ``declared`` is the census's own bucket count from
    ``4_action_totals``, so a short parse is caught by a count and not only by the exception that
    ended the first take.

    Residue and identity come from ``C_population_identity``, not from
    ``residue_axis_census_face``: this generation the census dies after arm C (its own
    ``readiness_census.py`` subprocess exits 1), so the terminal face line never prints. That is
    recorded as ``face_line_found`` instead of being worked around, because the per-row
    ``row <identity> ... action=<bucket>`` lines print before the
    death and are the authoritative row set -- the identity list hardcoded in the first take was
    wrong for 5 of the 11 ruled-out rows, which is why no identity is typed here.
    """
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [PY, str(C128 / "residue_axis_census.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": f"{REPO}:{C128}"},
    )
    actions: dict[str, int] = {}
    rows: list[dict[str, str]] = []
    red_arms: list[str] = []
    identity = "unparsed"
    residue = -1
    declared = -1
    face_line_found = False
    for line in proc.stdout.splitlines():
        if line.startswith("2_actions "):
            actions = ast.literal_eval(line[len("2_actions ") :].split(" sums=")[0])
        elif line.startswith("4_action_totals "):
            declared = int(re.search(r"actions=(\d+)", line).group(1))  # type: ignore[union-attr]
        elif line.startswith("C_population_identity "):
            identity = re.search(r"identity_set=(\w+)", line).group(1)  # type: ignore[union-attr]
            residue = int(re.search(r"residue=(\d+)", line).group(1))  # type: ignore[union-attr]
        elif line.startswith("residue_axis_census_face "):
            face_line_found = True
        elif line.startswith("row "):
            parts = line.split()
            fields = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
            fields["identity"] = parts[1]
            rows.append(fields)
        elif "PASS=False" in line and re.match(r"^[A-H]_", line):
            red_arms.append(line.split()[0])
    return {
        "actions": actions,
        "declared": declared,
        "rows": rows,
        "identity": identity,
        "residue": residue,
        "exit": proc.returncode,
        "red_arms": red_arms,
        "face_line_found": face_line_found,
        "stderr_tail": proc.stderr.strip().splitlines()[-1:],
    }


def _csv_rows(provider: str) -> list[dict[str, str]]:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    named = [row for row in rows if (row.get("provider") or "").strip() == provider]
    return named


def _csv_columns() -> list[str]:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        return list(next(csv.reader(handle)))


def _wsj_ledger_state() -> list[str]:
    """One line per wsj row: its own status columns, keyed by header name."""
    columns = _csv_columns()
    status_column = next(
        name for name in columns if "acceptance" in name.lower() or "status" in name.lower()
    )
    run_columns = [
        name for name in columns if name.lower().endswith("_status") and name != status_column
    ][:2]
    out = []
    for row in _csv_rows("wsj"):
        reads = {name: row.get(name, "MISSING-COLUMN") for name in [status_column, *run_columns]}
        out.append(f"{row['upstream_model']} {reads}")
    return out


def _network_sites(package: Path) -> dict[str, Any]:
    """Call sites and the named file populations for one provider package."""
    files = sorted(package.rglob("*.py"))
    hits = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            hits += sum(1 for token in NETWORK_TOKENS if token in line)
    return {
        "hits": hits,
        "py_files": len(files),
        "models_py_files": len(sorted((package / "models").rglob("*.py"))),
        "dirs": sorted(
            d.name for d in package.iterdir() if d.is_dir() and "__pycache__" not in d.parts
        ),
    }


def _provider_names() -> list[str]:
    """The provider packages on disk -- the population that makes a whole-tree dump classifiable."""
    root = REPO / "opendata/data/providers"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _status_codes(text: str) -> list[str]:
    """Status codes this file records, read from wire-shaped lines only.

    Prose that says "HTTP 401" is not a code here, because the predicate reads a status line shape
    (version token followed by a three-digit code), not the digits.
    """
    codes = []
    for line in text.splitlines():
        matched = STATUS_LINE_RE.match(line) or STATUS_LINE_RE.search(line)
        if matched:
            codes.append(matched.group("code"))
    return codes


def _manifest_index() -> dict[str, dict[str, str]]:
    """Capture file name -> (url host, http code, declared use), read off the review receipts.

    ``capture-receipt.json`` style manifests carry ``source_captures[]`` with the *requested URL*
    for each body/headers file. That is a stronger class source than a ``server:`` header, because
    it distinguishes "we fetched docs.openbb.co" from "we fetched finviz.com" even when neither
    response names its server. Coverage is partial and printed, so a missing entry is a gap rather
    than an inference.
    """
    idx: dict[str, dict[str, str]] = {}
    for path in sorted((REPO / "docs/evidence").rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            continue
        caps = doc.get("source_captures") if isinstance(doc, dict) else None
        if not isinstance(caps, list):
            continue
        for cap in caps:
            if not isinstance(cap, dict):
                continue
            host = urlparse(str(cap.get("url") or "")).netloc.lower()
            entry = {
                "host": host,
                "http": str(cap.get("http") or ""),
                "use": str(cap.get("use") or ""),
                "manifest": str(path.relative_to(REPO)),
            }
            for field in ("headers_file", "body_file"):
                name = str(cap.get(field) or "")
                if name:
                    idx.setdefault(Path(name).name, entry)
    return idx


def _host_class(host: str) -> str:
    """Documentation of a contract, versus a live provider surface."""
    if not host:
        return "unknown_host"
    if host.startswith("docs.") or host.endswith(".github.io") or "documentation" in host:
        return "contract_page"
    return "provider_surface"


def _capture_class(lines: list[str], manifest: dict[str, str] | None) -> dict[str, str]:
    """(code, server, class, class source, host) of one capture.

    Class is not cosmetic: a ``contract_page`` capture proves the output contract was reviewed,
    which is not the same fact as an upstream surface answering. Only ``provider_surface`` captures
    replay as an access claim.
    """
    code = (_status_codes(lines[0]) or [""])[0] if lines else ""
    server = ""
    for line in lines:
        if line.lower().startswith("server:"):
            server = line.split(":", 1)[1].strip()
            break
    if manifest:
        kind = _host_class(manifest["host"])
        source, host = "manifest_url", manifest["host"]
    elif not server:
        kind, source, host = "server_unrecorded", "server_header", ""
    elif "github.com" in server.lower():
        kind, source, host = "contract_page", "server_header", server
    else:
        kind, source, host = "provider_surface", "server_header", server
    if not code:
        kind = "no_status_line"
    return {"code": code, "server": server, "class": kind, "source": source, "host": host}


def _headers_census() -> dict[str, Any]:
    """Read the repo's own response-capture format: every ``*.headers`` file under docs/evidence.

    Two attribution channels are kept apart on purpose. Provider-name attribution (the provider's
    package name appears in the file name or one of the two parent dirs) is coarse:
    ``cboe-allaccess`` says nothing about ``IndexSnapshots``. Model-name attribution normalizes
    separators and matches the upstream model name, which is what ties a capture to the row.
    """
    providers = _provider_names()
    manifest = _manifest_index()
    files = sorted((REPO / "docs/evidence").rglob("*.headers"))
    records: list[dict[str, str]] = []
    for path in files:
        rel = str(path.relative_to(REPO))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]
        except OSError:
            lines = []
        cls = _capture_class(lines, manifest.get(path.name))
        hay = f"{path.parent.name}/{path.name}/{path.parent.parent.name}".lower()
        hit = [p for p in providers if p in hay or p.replace("_", "-") in hay]
        records.append(
            {
                "rel": rel,
                "name": path.name,
                "key": path.name.lower().replace("-", "").replace("_", ""),
                "code": cls["code"],
                "server": cls["server"],
                "class": cls["class"],
                "source": cls["source"],
                "host": cls["host"],
                "provider": hit[0] if hit else "",
            }
        )
    codes: dict[str, int] = {}
    for rec in records:
        key = rec["code"] or "NO_STATUS_LINE"
        codes[key] = codes.get(key, 0) + 1
    by_provider: dict[str, list[str]] = {}
    for rec in records:
        if rec["provider"]:
            by_provider.setdefault(rec["provider"], []).append(rec["name"])
    prefixes: dict[str, int] = {}
    for rec in records:
        if rec["provider"]:
            continue
        token = re.split(r"[-_.]", rec["name"])[0]
        prefixes[token] = prefixes.get(token, 0) + 1
    return {
        "total": len(records),
        "codes": dict(sorted(codes.items())),
        "by_provider": {k: sorted(v) for k, v in sorted(by_provider.items())},
        "providers_with_captures": sorted(by_provider),
        "unattributed_count": sum(1 for rec in records if not rec["provider"]),
        "unattributed_prefixes": dict(sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "classes": {
            kind: sum(1 for rec in records if rec["class"] == kind)
            for kind in sorted({rec["class"] for rec in records})
        },
        "manifest_named": sum(1 for rec in records if rec["source"] == "manifest_url"),
        "manifest_total": len(manifest),
        "records": records,
    }


def _row_captures(headers: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    """Model-level and provider-level captures for one residue row, with their classes."""
    model_key = model.lower().replace("_", "").replace("-", "")
    model_hits = [r for r in headers["records"] if model_key in r["key"]]
    provider_hits = [r for r in headers["records"] if r["provider"] == provider]

    def classes(hits: list[dict[str, str]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in hits:
            out[rec["class"]] = out.get(rec["class"], 0) + 1
        return dict(sorted(out.items()))

    if model_hits:
        tier = (
            "model_provider_surface"
            if classes(model_hits).get("provider_surface")
            else (
                "model_contract_page"
                if classes(model_hits).get("contract_page")
                else "model_unrecorded"
            )
        )
        basis = model_hits
    elif provider_hits:
        tier = (
            "provider_surface_only"
            if classes(provider_hits).get("provider_surface")
            else "provider_page_only"
        )
        basis = provider_hits
    else:
        tier = "no_capture"
        basis = []
    return {
        "tier": tier,
        "model_captures": [r["name"] for r in model_hits][:2],
        "model_classes": classes(model_hits),
        "provider_capture_count": len(provider_hits),
        "provider_classes": classes(provider_hits),
        "basis_codes": sorted({r["code"] for r in basis if r["code"]}),
        "basis_sources": sorted({r["source"] for r in basis}),
        "basis_hosts": sorted({r["host"] for r in basis if r["host"]})[:3],
    }


def _role(rel: str, line: str, providers: list[str], ledger_text: str) -> str:
    """What kind of carrier names wsj and 401.

    A zero is only readable as absence if the hits that *do* exist are accounted for by role. The
    fourth take named 70 hits in five roles, and 17 of them were unclassified until their own lines
    were read: linter output that echoes the ``specs.py`` source line verbatim (``"content":``), a
    committed metadata receipt that stores the ledger reason itself, and a prior wave's
    ``"endpoint": "NO_READING..."`` assessment. The ledger fragment is passed in from the shipped
    ``_ruled_out`` reading rather than typed, so a rewritten reason cannot silently reclassify.
    """
    if rel.startswith(BUNDLE_REL):
        # this face's own probe, carrier and notes quote the tokens they search for; counting them
        # as "carriers exist" would be self-confirmation, so they get their own role instead.
        return "this_wave_artifact"
    named = sum(
        1 for name in providers if f'"{name}"' in line or f"/{name}" in line or name in line
    )
    # equality with the on-disk provider population, not a threshold I picked: a line naming every
    # provider is a listing of the set (the C100 receipts do this in one JSON line); naming fewer
    # stays unclassified and turns the arm red, which is the reading I want to see.
    if named == len(providers):
        return "multi_provider_dump"
    if '"content":' in line:
        return "lint_source_echo"
    if ledger_text and ledger_text[:40] in line:
        return "ledger_prose_echo"
    if "NO_READING" in line or '"endpoint"' in line:
        return "prior_assessment"
    if "reason_full[" in line or "NOT_DECL" in line:
        return "ledger_prose_echo"
    if rel.endswith(".py") and "mutant" in line:
        return "tamper_recipe"
    if rel.endswith(".py"):
        return "probe_source_quote"
    if "_stdout." in rel or "pytest" in rel:
        return "test_log"
    if "/notes/" in rel:
        return "audit_note"
    return "unclassified"


def _access_capture_census(ledger_text: str) -> dict[str, Any]:
    """Walk the evidence tree once and separate prose that *says* 401 from a capture *of* one.

    Three defects were paid for on the way here. The first take grepped whole files for "wsj" and
    "401" and read 841 hits, which measured nothing (an 800 KB single-line pytest log matches that
    predicate as well as the ledger reason does), so the co-naming test is line-level and every hit
    is classified by role. The second take restricted the text population to
    ``.md/.txt/.json/.csv`` and read ``status_line_files=0`` -- an artifact of the filter, because
    ``docs/evidence/C100/remaining-protocol-review/public-six/sources/*.headers`` are response
    captures with status lines. The walk now tries every file that decodes as text, so the wsj zero
    is bracketed by an in-tree positive of the same shape rather than only by a planted file. And
    because this probe's own source and carrier contain the literal "HTTP/1.1 401", files under this
    bundle are excluded from the capture predicates and that exclusion is counted and printed.
    """
    providers = _provider_names()
    walked = 0
    readable = 0
    self_excluded = 0
    path_named: list[str] = []
    co_naming: dict[str, list[str]] = {}
    protocol_files = 0
    longest_matched_line = 0
    for path in sorted((REPO / "docs/evidence").rglob("*")):
        if not path.is_file():
            continue
        walked += 1
        rel = str(path.relative_to(REPO))
        if "wsj" in path.name.lower():
            path_named.append(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        readable += 1
        matched = [line for line in text.splitlines() if "wsj" in line.lower() and "401" in line]
        if matched:
            for line in matched:
                co_naming.setdefault(_role(rel, line, providers, ledger_text), []).append(rel)
            longest_matched_line = max(longest_matched_line, max(len(line) for line in matched))
        if rel.startswith(BUNDLE_REL):
            self_excluded += 1
            continue
        if "HTTP/" in text:
            protocol_files += 1
    return {
        "walked": walked,
        "readable": readable,
        "self_excluded": self_excluded,
        "providers_on_disk": len(providers),
        "path_named": path_named,
        "co_naming": co_naming,
        "co_naming_files": len({x for hits in co_naming.values() for x in hits}),
        "protocol_files": protocol_files,
        "longest_matched_line": longest_matched_line,
    }


def _plant_controls(tmp_root: Path) -> dict[str, Any]:
    """Run the capture predicate through a real capture and through the prose it must not match."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    capture = tmp_root / "w35_planted_capture.txt"
    capture.write_text(
        "HTTP/1.1 401 Unauthorized\r\nwww-authenticate: NTLM\r\n\r\n", encoding="utf-8"
    )
    prose = tmp_root / "w35_planted_prose.txt"
    prose.write_text(f"reason_full[wsj::ETFActive]={PROSE_ONLY}\n", encoding="utf-8")
    out = {
        "capture_planted_codes": _status_codes(capture.read_text(encoding="utf-8")),
        "prose_planted_codes": _status_codes(prose.read_text(encoding="utf-8")),
    }
    capture.unlink()
    prose.unlink()
    tmp_root.rmdir()
    return out


def main() -> int:
    """Print the face; every arm can fail."""
    arms: dict[str, bool] = {}
    lines: list[str] = []
    judge_face = _judge_face()
    census = _census_face()
    actions: dict[str, int] = census["actions"]
    residue: int = census["residue"]
    identity: str = census["identity"]

    lines.append(
        f"population judge_accepted={judge_face['accepted']} "
        f"identities={judge_face['identities']} "
        f"claims={judge_face['claims']} statuses={judge_face['statuses']}"
    )
    lines.append(
        f"population census_residue={residue} census_buckets_parsed={len(actions)} "
        f"census_buckets_declared={census['declared']} census_row_lines={len(census['rows'])} "
        f"census_actions={actions} identity_set={identity}"
    )
    lines.append(
        f"census_self_exit={census['exit']} census_red_arms={census['red_arms']} "
        f"census_face_line_found={census['face_line_found']} "
        f"census_stderr_tail={census['stderr_tail']} "
        "note: the shipped census dies after its arm C, so its terminal face line is unreadable "
        "this generation and residue/identity come from its C_population_identity line instead"
    )
    arms["A_population"] = (
        sum(actions.values()) == residue == len(census["rows"]) == 350 - judge_face["accepted"]
        and len(actions) == census["declared"] > 0
    )
    arms["B_judge_generation"] = (
        residue + judge_face["accepted"] == 350
        and identity == judge_face["identity"]
        and len(identity) == 16
        and judge_face["claims_sha256"] == _claims_hash()
    )

    def _all_pending(rows: list[str]) -> bool:
        return bool(rows) and all("NOT_ASSESSED" in row and "NOT_RUN" in row for row in rows)

    guard = _load("w35_guard_axes", C128 / "incomplete_row_guard_check.py")
    wsj_ruled = [
        r["identity"]
        for r in census["rows"]
        if r.get("action") == "ledger_ruled_out" and r["identity"].startswith("wsj::")
    ]
    ledger_text = ""
    if wsj_ruled:
        provider, _, model = sorted(wsj_ruled)[0].partition("::")
        ledger_text = guard._ruled_out(provider, model)[0].split(":", 1)[1]
    wsj = _wsj_ledger_state()
    lines.extend(f"wsj_ledger {row}" for row in wsj)
    arms["C_wsj_ledger_state"] = _all_pending(wsj)

    wsj_pkg = _network_sites(REPO / "opendata/data/providers/wsj")
    lines.append(
        f"wsj_network_sites hits={wsj_pkg['hits']} py_files={wsj_pkg['py_files']} "
        f"models_py_files={wsj_pkg['models_py_files']} dirs={wsj_pkg['dirs']} "
        f"tokens={list(NETWORK_TOKENS)}"
    )
    arms["D_wsj_no_request_site"] = wsj_pkg["py_files"] > 0 and wsj_pkg["hits"] == 0

    access = _access_capture_census(ledger_text)
    headers = _headers_census()
    plant = _plant_controls(Path(tempfile.mkdtemp(prefix="w35_capture_controls_")))
    roles = {role: len(hits) for role, hits in sorted(access["co_naming"].items())}
    refusal_codes = sum(
        count for code, count in headers["codes"].items() if code.isdigit() and code.startswith("4")
    )
    lines.append(
        f"headers_census total={headers['total']} codes={headers['codes']} "
        f"providers_with_captures={headers['providers_with_captures']} "
        f"classes={headers['classes']} unattributed={headers['unattributed_count']}"
    )
    lines.append(
        f"unattributed_prefixes={headers['unattributed_prefixes']} "
        f"unattributed_sum={sum(headers['unattributed_prefixes'].values())} "
        f"unattributed={headers['unattributed_count']}"
    )
    lines.append(
        f"wsj_capture_facing headers_named_wsj={len(headers['by_provider'].get('wsj', []))} "
        f"path_named_wsj={len(access['path_named'])} "
        f"code_401_captures={headers['codes'].get('401', 0)} "
        f"code_4xx_captures={refusal_codes}"
    )
    lines.append(
        f"prose_census walked={access['walked']} readable_text={access['readable']} "
        f"self_excluded={access['self_excluded']} providers_on_disk={access['providers_on_disk']} "
        f"http_protocol_string_files={access['protocol_files']} "
        f"co_naming_lines={sum(roles.values())} co_naming_files={access['co_naming_files']} "
        f"longest_matched_line={access['longest_matched_line']}"
    )
    lines.append(f"co_naming_roles={roles}")
    lines.append(f"capture_predicate_controls {plant}")
    lines.append(
        f"ledger_fragment_source rows={sorted(wsj_ruled)} fragment_chars={len(ledger_text)} "
        f"fragment={ledger_text[:48]!r}"
    )
    arms["E_no_wsj_access_capture"] = (
        headers["total"] > 0
        and sum(headers["unattributed_prefixes"].values()) == headers["unattributed_count"]
        and headers["classes"].get("provider_surface", 0) > 0
        and headers["classes"].get("contract_page", 0) > 0
        and headers["manifest_named"] > 0
        and refusal_codes > 0
        and headers["codes"].get("401", 0) == 0
        and len(headers["by_provider"].get("wsj", [])) == 0
        and len(access["path_named"]) == 0
        and access["readable"] > 0
        and access["self_excluded"] > 0
        and sum(roles.values()) > 0
        and roles.get("unclassified", 0) == 0
        and "401" in plant["capture_planted_codes"]
        and plant["prose_planted_codes"] == []
    )

    gated = subprocess.run(  # nosec B603  # noqa: S603
        [GREP, "-rl", LIVE_GATE, str(REPO / "tests")],
        capture_output=True,
        text=True,
        check=False,
    )
    gate_files = [p for p in gated.stdout.splitlines() if p.strip()]
    gate_py = [p for p in gate_files if p.endswith(".py")]
    lines.append(
        f"live_gate files={len(gate_files)} py_files={len(gate_py)} "
        f"pycache_files={len(gate_files) - len(gate_py)} "
        f"token_present_in_env={LIVE_GATE in os.environ} "
        f"py_sample={sorted(Path(p).name for p in gate_py)[:3]}"
    )
    # the shipped grep has no --include, so most of its hits can be __pycache__ blobs (6 of 8 in
    # this generation): the gate is real but lives in the source files, and a count that leans on
    # stale bytecode overstates how many places enforce it.
    arms["F_offline_egress_gate"] = len(gate_py) > 0 and LIVE_GATE not in os.environ

    ruled_rows = [r["identity"] for r in census["rows"] if r.get("action") == "ledger_ruled_out"]
    lines.append(
        f"ruled_out_rows_from_census count={len(ruled_rows)} "
        f"providers={sorted({i.partition('::')[0] for i in ruled_rows})}"
    )
    cited = 0
    tiers: dict[str, int] = {}
    no_capture_rows: list[str] = []
    for identity_key in sorted(ruled_rows):
        provider, _, model = identity_key.partition("::")
        cap = _row_captures(headers, provider, model)
        tiers[cap["tier"]] = tiers.get(cap["tier"], 0) + 1
        if cap["tier"] == "no_capture":
            no_capture_rows.append(identity_key)
        entry, no_ledger = guard._ruled_out(provider, model)
        spec = REPO / "opendata/data/providers" / provider / "specs.py"
        line_no = "no-specs-file"
        if spec.is_file():
            for number, text in enumerate(spec.read_text(encoding="utf-8").splitlines(), 1):
                if f'"{model}"' in text:
                    line_no = f"{spec.relative_to(REPO)}:{number}"
                    break
        if entry and not no_ledger and line_no != "no-specs-file":
            cited += 1
        lines.append(
            f"ruled_out_cite row={identity_key} ledger_dict={entry.split(':', 1)[0]} "
            f"cite={line_no} no_ledger={no_ledger or '-'} entry_chars={len(entry)} "
            f"tier={cap['tier']} basis_sources={cap['basis_sources']} "
            f"model_captures={cap['model_captures']} "
            f"model_classes={cap['model_classes']} "
            f"provider_captures={cap['provider_capture_count']} "
            f"provider_classes={cap['provider_classes']} basis_codes={cap['basis_codes']}"
        )
    arms["G_ruled_out_reasons_are_citable"] = cited == len(ruled_rows) > 0
    lines.append(
        f"ruled_out_cited count={cited} population={len(ruled_rows)} "
        f"tiers={dict(sorted(tiers.items()))} no_capture_rows={no_capture_rows} "
        f"no_capture={len(no_capture_rows)}"
    )

    if len(wsj) >= 2:
        doctored = [
            wsj[0],
            wsj[1].replace("NOT_ASSESSED", "OFFLINE_ACCEPTED").replace("NOT_RUN", "DONE"),
        ]
    else:
        doctored = []
    forged_red = not _all_pending(doctored)
    lines.append(
        f"H_forge rows={len(doctored)} forged_from_real_row={bool(wsj)} "
        f"arm_c_goes_red_on_forge={forged_red}"
    )
    arms["H_forge"] = forged_red

    absent_entry, absent_reason = guard._ruled_out("cboe", "W35DefinitelyNotAModel")
    first_provider, _, first_model = sorted(ruled_rows)[0].partition("::")
    present_entry, _ = guard._ruled_out(first_provider, first_model)
    ctrl = _network_sites(REPO / "opendata/data/providers/yfinance")
    docs_rec = next((r for r in headers["records"] if r["class"] == "contract_page"), None)
    api_rec = next((r for r in headers["records"] if r["class"] == "provider_surface"), None)
    fake_cap = _row_captures(headers, "wsj", "ETFNothingCaptured")
    model_cap = _row_captures(headers, first_provider, first_model)
    lines.append(
        f"I_predicate_controls ledger_absent_entry_chars={len(absent_entry)} "
        f"ledger_absent_reason={absent_reason or '-'} "
        f"ledger_present_row={first_provider}::{first_model} "
        f"ledger_present_entry_chars={len(present_entry)} "
        f"network_control_hits={ctrl['hits']} network_control_files={ctrl['py_files']} "
        f"capture_planted={plant['capture_planted_codes']} "
        f"prose_planted={plant['prose_planted_codes']}"
    )
    lines.append(
        f"I_capture_class_controls docs_host={docs_rec['host'] if docs_rec else '-'} "
        f"api_host={api_rec['host'] if api_rec else '-'} "
        f"docs_name={docs_rec['name'] if docs_rec else '-'} "
        f"api_name={api_rec['name'] if api_rec else '-'} "
        f"manifest_named={headers['manifest_named']}/{headers['total']} "
        f"manifest_index_files={headers['manifest_total']} "
        f"fake_tier={fake_cap['tier']} real_tier={model_cap['tier']} "
        f"tier_differs={fake_cap['tier'] != model_cap['tier']}"
    )
    arms["H_forge"] = forged_red and len(doctored) == 2
    arms["I_predicates_can_differ"] = (
        absent_entry == ""
        and len(present_entry) > 0
        and ctrl["hits"] > 0
        and "401" in plant["capture_planted_codes"]
        and plant["prose_planted_codes"] == []
        and docs_rec is not None
        and api_rec is not None
        and docs_rec["host"] != api_rec["host"]
        and fake_cap["tier"] == "no_capture"
        and fake_cap["tier"] != model_cap["tier"]
    )

    failed = [name for name, ok in sorted(arms.items()) if not ok]
    lines.append(
        f"refusal_evidence_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    lines.append(f"w35_refusal_evidence_face_exit={0 if not failed else 1}")
    body = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w35_refusal_evidence_face.py\n"
        f"# argv: {PY} {REPO / BUNDLE_REL / 'probes' / 'w35_refusal_evidence_face.py'}\n"
        f"# claims_sha256={hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]}\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
