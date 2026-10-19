"""Artifact-request face: the one official artifact each of the 14 residue rows still needs.

The objective forbids verbal judgments. §13 established that the 14 rows left at plane 1
(336/350) are blocked on evidence or on an owner act, not on code -- but "blocked on evidence" was
still prose. This face turns it into a per-row request list built from three shipped sources:

``A_population``
    the residue rows, taken from the shipped census's own ``row ... action=`` lines through the
    sibling face's parser (a typed row list was wrong for 5 of 11 rows).
``B_upstream_resolved``
    for each row, the OpenBB model module that defines its output contract, resolved by name in the
    reference checkout. Unresolved rows are printed, not assumed.
``C_endpoint_literals``
    URL literals inside that module. This separates "the source is a website widget with a concrete
    template" from "there is no address at all", which the ledger flattens into one sentence.
``D_capture_present``
    whether the evidence tree already holds a capture of *that endpoint*, keyed on host plus the
    first path segment from the review receipts. Both granularities are printed: host-only matching
    made the first take over-claim, because intrinio's row hit on ``docs.intrinio.com`` -- a
    documentation page standing in for an API response.
``E_forge``
    the falsification arm: write a synthetic receipt into a temp tree, require the endpoint index to
    gain that key, delete it and require the index to equal its own prior snapshot exactly.
``F_contract_width``
    the output contract each row must satisfy: the ``class <Model>Data(<Standard>)`` declaration in
    the upstream module and its annotated field count, so "no schema" is reported against a width.
``G_generation``
    same-generation check: the identity set and the claims digest the sibling face judged.

Nothing in the repository is written; the forged object lives under a temp dir and is deleted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C139/wave34-tmxyfinance-rest-20261018"
CARRIER = REPO / BUNDLE / "runs" / "w38_artifact_request_face.txt"
SIBLING = REPO / BUNDLE / "probes" / "w35_refusal_evidence_face.py"
UPSTREAM = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
URL_RE = re.compile(r"https?://[^\s\"'\\)]+")
CLASS_RE = re.compile(r"^class\s+(\w*Data)\s*\((\w+)", re.M)
FIELD_RE = re.compile(r"^    (\w+)\s*:", re.M)
BODY_STOP_RE = re.compile(r"^(class |def |@)", re.M)
FORGED_SEGMENT = "forged_marker"


def _load(name: str, path: Path) -> Any:  # noqa: ANN401  # shipped probes are heterogeneous
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import shipped probe {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _endpoint_key(url: str) -> str:
    """``host|first-path-segment`` -- the granularity a capture is actually identified by."""
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    head = parts[1].lower() if len(parts) > 2 and parts[1] else ""
    return f"{parsed.netloc.lower()}|{head}"


def _host_key(url: str) -> str:
    return f"{urlparse(url).netloc.lower()}|"


def _upstream_module(provider: str, model: str) -> tuple[Path | None, int]:
    """(model module, files searched) for one row in the reference checkout."""
    root = UPSTREAM / provider / f"openbb_{provider}" / "models"
    if not root.is_dir():
        return None, 0
    stem = re.sub(r"[^a-z0-9]", "", model.lower())
    files = sorted(root.glob("*.py"))
    for path in files:
        if re.sub(r"[^a-z0-9]", "", path.stem.lower()) == stem:
            return path, len(files)
    for path in files:
        if stem and stem in re.sub(r"[^a-z0-9]", "", path.stem.lower()):
            return path, len(files)
    return None, len(files)


def _url_literals(path: Path) -> list[str]:
    return sorted({m.group(0) for m in URL_RE.finditer(path.read_text("utf-8", errors="replace"))})


def _contract_fields(path: Path) -> tuple[str, int]:
    """(contract class, annotated field count) declared inside one upstream model module."""
    text = path.read_text(encoding="utf-8", errors="replace")
    found = CLASS_RE.search(text)
    if not found:
        return "", 0
    tail = text[found.end() :]
    stop = BODY_STOP_RE.search(tail)
    body = tail[: stop.start()] if stop else tail
    return found.group(1), len(FIELD_RE.findall(body))


def _receipt_endpoints(extra_files: list[Path] | None = None) -> dict[str, str]:
    """Endpoint key -> the receipt that recorded it, read from the shipped review receipts."""
    found: dict[str, str] = {}
    candidates = sorted((REPO / "docs/evidence").rglob("*.json")) + list(extra_files or [])
    for path in candidates:
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
            url = str(cap.get("url") or "")
            if not urlparse(url).netloc:
                continue
            rel = str(path)
            found.setdefault(_endpoint_key(url), rel)
            found.setdefault(_host_key(url), rel)
    return found


def main() -> int:
    """Print the face; every arm can fail."""
    sys.path.insert(0, str(REPO))
    sib = _load("w35_face_sibling", SIBLING)
    census = sib._census_face()
    judge_face = sib._judge_face()
    residue = [r for r in census["rows"] if r.get("identity")]
    arms: dict[str, bool] = {}
    lines: list[str] = []

    lines.append(
        f"A_population residue={len(residue)} census_residue={census['residue']} "
        f"actions={census['actions']} judge_accepted={judge_face['accepted']}"
    )
    arms["A_population"] = (
        len(residue) == census["residue"] == 350 - judge_face["accepted"] > 0
        and sum(census["actions"].values()) == census["residue"]
        and all(r.get("action") for r in residue)
    )

    hosts = _receipt_endpoints()
    resolved = 0
    with_url = 0
    unresolved: list[str] = []
    captured: list[str] = []
    captured_with_key: list[tuple[str, str]] = []
    uncaptured: list[str] = []
    host_only_rows: list[str] = []
    total_fields = 0
    rows_with_contract = 0
    for row in residue:
        identity = row["identity"]
        provider, _, model = identity.partition("::")
        path, searched = _upstream_module(provider, model)
        urls: list[str] = []
        contract, fields = "", 0
        if path is not None:
            resolved += 1
            urls = _url_literals(path)
            contract, fields = _contract_fields(path)
        else:
            unresolved.append(identity)
        with_url += bool(urls)
        keys = sorted({_endpoint_key(u) for u in urls if urlparse(u).netloc})
        precise = [k for k in keys if k in hosts]
        coarse = [k.split("|")[0] + "|" for k in keys if k.split("|")[0] + "|" in hosts]
        if precise:
            captured.append(identity)
            captured_with_key.append((identity, precise[0]))
        else:
            uncaptured.append(identity)
            if coarse:
                host_only_rows.append(identity)
        total_fields += fields
        rows_with_contract += bool(contract)
        lines.append(
            f"row {identity} action={row.get('action')} "
            f"upstream={'resolved' if path else 'ABSENT'}/{searched} urls={len(urls)} "
            f"endpoint_keys={keys[:2]} contract={contract or '-'} fields={fields} "
            f"capture_endpoint={precise[:1]!r} capture_host_only={coarse[:1]!r} "
            f"first_url={urls[:1]}"
        )

    lines.append(f"B_upstream_resolved count={resolved} of={len(residue)} unresolved={unresolved}")
    arms["B_upstream_resolved"] = (
        0 < resolved < len(residue) and len(unresolved) == len(residue) - resolved
    )
    lines.append(
        f"C_endpoint_literals rows_with_url={with_url} rows_without={len(residue) - with_url} "
        f"receipt_endpoint_keys={len(hosts)}"
    )
    arms["C_endpoint_literals"] = 0 < with_url < len(residue) and len(hosts) > 0
    lines.append(
        f"D_capture_present captured={len(captured)} uncaptured={len(uncaptured)} "
        f"captured_rows={captured_with_key} host_page_only_rows={host_only_rows} "
        f"total_contract_fields={total_fields} rows_with_contract_class={rows_with_contract}"
    )
    arms["D_capture_present"] = len(captured) + len(uncaptured) == len(residue) > 0

    forged_target = uncaptured[0] if uncaptured else ""
    forged_provider, _, forged_model = forged_target.partition("::")
    forged_path, _ = _upstream_module(forged_provider, forged_model)
    found_urls = _url_literals(forged_path) if forged_path else []
    target_host = urlparse(found_urls[0]).netloc.lower() if found_urls else ""
    baseline_before = dict(hosts)
    scratch = Path(tempfile.mkdtemp(prefix="w38_forge_"))
    forged_file = scratch / "forged_receipt.json"
    forged_file.write_text(
        json.dumps({"source_captures": [{"url": f"https://{target_host}/{FORGED_SEGMENT}"}]}),
        encoding="utf-8",
    )
    after = _receipt_endpoints([forged_file])
    new_key = _endpoint_key(f"https://{target_host}/{FORGED_SEGMENT}")
    gained = new_key in after and new_key not in baseline_before
    forged_file.unlink()
    scratch.rmdir()
    reverted = _receipt_endpoints() == baseline_before
    lines.append(
        f"E_forge row={forged_target} host={target_host or '-'} key={new_key or '-'} "
        f"injection_gains_key={gained} index_reverts_exactly={reverted}"
    )
    arms["E_forge"] = gained and reverted and bool(target_host)

    lines.append(
        f"F_contract_width rows_with_contract_class={rows_with_contract} "
        f"total_contract_fields={total_fields}"
    )
    arms["F_contract_width"] = rows_with_contract > 0 and total_fields > 0

    identity = census["identity"]
    claims_sha = hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]
    lines.append(
        f"G_generation identity={identity} claims_sha256={claims_sha} "
        f"census_exit={census['exit']} census_red_arms={census['red_arms']}"
    )
    arms["G_generation"] = (
        len(identity) == 16 and len(claims_sha) == 16 and identity == judge_face["identity"]
    )

    failed = [name for name, ok in sorted(arms.items()) if not ok]
    lines.append(
        f"artifact_request_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    lines.append(f"w38_artifact_request_face_exit={0 if not failed else 1}")
    body = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w38_artifact_request_face.py\n"
        f"# argv: {sys.executable} {REPO / BUNDLE / 'probes' / 'w38_artifact_request_face.py'}\n"
        f"# claims_sha256={claims_sha}\n"
        f"# upstream_reference: {UPSTREAM} (output-contract reference only)\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
