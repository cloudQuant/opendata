"""Judge every declarative engine model against the pinned upstream interface sheet.

A ``ModelSpec`` is written by a person, so the claim it makes about an upstream endpoint needs the
same kind of check a hand-written fetcher gets from its own test file. This tool asks the runtime
which models are declared (``catalog.engine_declared_models``), and for each one measures five
things:

* the model identity exists in the iteration-2 task ledger (the 350-row denominator),
* the declared output columns are exactly the upstream standard model's column names,
* every declared query parameter is a field of the upstream ``QueryParams`` class,
* the declared host and the literal path segments appear somewhere in that provider's package,
* the declared domain is registered with reviewed semantics and its columns are that domain's
  contract, because the engine publishes the contract class and only rows of that class are served.

The first four are set comparisons and containment tests on facts the pinned upstream tree already
publishes. The endpoint test cannot prove a URL is right for a live response -- that stays
SOURCE_VERIFIED -- but it does refuse a declaration whose endpoint was invented. The fifth is a
repository-local fact the upstream sheet cannot reveal: a declaration may match upstream perfectly
and still name rows no query or ingest service accepts.

Every check is run against a mutated copy of its own input before the audit starts, because a judge
that can only print OK is not measuring anything.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opendata.data.domains import contract_model, load_domains  # noqa: E402
from opendata.data.providers._engine.spec import (  # noqa: E402
    ColumnSpec,
    ModelSpec,
    ParamSpec,
)
from scripts.quality.provider_model_inventory import (  # noqa: E402
    LEDGER_RELATIVE_PATH,
    UPSTREAM_COMMIT,
)
from scripts.quality.upstream_model_specs import extract  # noqa: E402

DEFAULT_UPSTREAM_PATH = Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform")

#: Codes that make the audit fail. An unbacked fact is a fabrication, not a gap.
VIOLATIONS = frozenset(
    {
        "NO_LEDGER_TASK",
        "NO_SHEET_ENTRY",
        "COLUMN_MISSING",
        "COLUMN_EXTRA",
        "PARAM_UNBACKED",
        "HOST_UNBACKED",
        "PATH_UNBACKED",
        "NO_MODELS_DECLARED",
        "DOMAIN_UNREGISTERED",
        "DOMAIN_NO_SEMANTICS",
        "CONTRACT_COLUMNS_DISAGREE",
    }
)

SheetEntry = dict[str, Any]


@dataclass(frozen=True)
class Finding:
    """One measured disagreement between a declaration and the pinned upstream sheet."""

    code: str
    model: str
    detail: str

    def line(self) -> str:
        """Render the finding with its severity first, so a scan of the log reads top-down."""
        level = "FAIL" if self.code in VIOLATIONS else "info"
        return f"{level:4} {self.code:26} {self.model}: {self.detail}"


def declared_columns(spec: ModelSpec) -> set[str]:
    """The output column names a declaration publishes."""
    return {column.name for column in spec.columns}


def columns_disagree(spec: ModelSpec, entry: SheetEntry) -> list[Finding]:
    """Compare declared output columns with the upstream standard model's field names.

    ``row_envelope`` is the declaration's own mechanism for a value living on the enclosing document
    rather than on each record, so an envelope key counts as a carrier for a column and is reported
    as ``COLUMN_FROM_ENVELOPE``. A column with neither an upstream field nor an envelope carrier is
    invented.
    """
    upstream = set(entry.get("output_columns") or ())
    if not upstream:
        return [Finding("NO_SHEET_ENTRY", spec.model, "the sheet publishes no output columns")]
    declared = declared_columns(spec)
    envelope = set(spec.row_envelope)
    findings: list[Finding] = []
    missing = sorted(upstream - declared)
    carried = sorted((declared & envelope) - upstream)
    extra = sorted(declared - upstream - envelope)
    if missing:
        findings.append(
            Finding(
                "COLUMN_MISSING",
                spec.model,
                f"upstream publishes, declaration omits: {missing}",
            )
        )
    if carried:
        findings.append(
            Finding(
                "COLUMN_FROM_ENVELOPE",
                spec.model,
                f"no upstream record field, carried by row_envelope: {carried}",
            )
        )
    if extra:
        findings.append(
            Finding(
                "COLUMN_EXTRA",
                spec.model,
                f"neither an upstream field nor a row_envelope key: {extra}",
            )
        )
    return findings


def params_disagree(spec: ModelSpec, entry: SheetEntry) -> list[Finding]:
    """Refuse a declared parameter that is not a field of the upstream query-params class."""
    upstream = {field["name"] for field in entry.get("query_params") or ()}
    declared = {parameter.name for parameter in spec.params}
    findings: list[Finding] = []
    unbacked = sorted(declared - upstream)
    if unbacked:
        findings.append(
            Finding("PARAM_UNBACKED", spec.model, f"no upstream QueryParams field: {unbacked}")
        )
    unexplained = sorted(upstream - declared)
    if unexplained:
        findings.append(
            Finding(
                "UPSTREAM_PARAM_UNCOVERED",
                spec.model,
                f"upstream field not declared (client-side or unused): {unexplained}",
            )
        )
    return findings


def static_segments(path: str) -> list[str]:
    """The path's literal segments; a ``{placeholder}`` is supplied at request time."""
    return [segment for segment in path.split("/") if segment and "{" not in segment]


def declared_host(spec: ModelSpec) -> str:
    """The host part of the declared base URL."""
    remainder = spec.base_url.split("//", 1)[-1]
    return remainder.split("/")[0].split(":")[0]


def endpoint_disagree(spec: ModelSpec, package_text: str) -> list[Finding]:
    """Require the declared host and each literal path segment to occur in the provider package."""
    findings: list[Finding] = []
    host = declared_host(spec)
    if host and host not in package_text:
        findings.append(
            Finding("HOST_UNBACKED", spec.model, f"{host!r} occurs in no upstream file")
        )
    absent = [segment for segment in static_segments(spec.path) if segment not in package_text]
    if absent:
        findings.append(
            Finding("PATH_UNBACKED", spec.model, f"literal segment(s) absent upstream: {absent}")
        )
    return findings


def registered_domains() -> dict[str, tuple[bool, frozenset[str]]]:
    """Map each registered domain to whether it is reviewed and what its contract publishes.

    Read from the live registry rather than a fixture so the audit cannot pass against a domain
    somebody only meant to register.
    """
    domains: dict[str, tuple[bool, frozenset[str]]] = {}
    for domain, spec in load_domains().items():
        if not spec.semantics_declared:
            domains[domain] = (False, frozenset())
            continue
        domains[domain] = (True, frozenset(contract_model(domain).model_fields))
    return domains


def domain_disagree(
    spec: ModelSpec, domains: dict[str, tuple[bool, frozenset[str]]]
) -> list[Finding]:
    """Require the declaration's domain to exist and its columns to be that domain's contract.

    The engine publishes ``contract_model(domain)`` for a reviewed domain, and the query and ingest
    services accept a row only when its class is exactly that model. So a declaration on an
    unreviewed domain, or one whose columns do not match the contract, names rows nothing can
    serve -- a fact the upstream sheet cannot reveal, because it only describes upstream.
    """
    if spec.domain not in domains:
        return [
            Finding(
                "DOMAIN_UNREGISTERED",
                spec.model,
                f"domain {spec.domain!r} is not in opendata/data/domains.yaml",
            )
        ]
    declared, contract_fields = domains[spec.domain]
    if not declared:
        return [
            Finding(
                "DOMAIN_NO_SEMANTICS",
                spec.model,
                f"domain {spec.domain!r} has no reviewed semantics, so its rows reach no service",
            )
        ]
    disagree = sorted(declared_columns(spec) ^ contract_fields)
    if disagree:
        return [
            Finding(
                "CONTRACT_COLUMNS_DISAGREE",
                spec.model,
                f"columns and contract {spec.domain!r} differ on: {disagree}",
            )
        ]
    return []


def package_source(upstream_root: Path, source: str) -> str:
    """Concatenate a provider package's upstream Python text, excluding its tests."""
    root = upstream_root / "providers" / source
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.rglob("*.py"))
        if "test" not in path.parts
    )


def audit(
    models: list[tuple[str, ModelSpec]],
    sheet: dict[str, Any],
    ledger_ids: set[str],
    upstream_root: Path,
) -> tuple[list[Finding], list[str]]:
    """Measure every declared model; return the findings and one summary row per model."""
    findings: list[Finding] = []
    rows: list[str] = []
    if not models:
        findings.append(
            Finding("NO_MODELS_DECLARED", "-", "no provider descriptor binds an engine model")
        )
        return findings, rows
    packages: dict[str, str] = {}
    domains = registered_domains()
    for source, spec in models:
        model_id = f"{source}::{spec.model}"
        entry = ((sheet.get(source) or {}).get("models") or {}).get(spec.model)
        model_findings = domain_disagree(spec, domains)
        if f"OBB2-{source}-{spec.model}" not in ledger_ids:
            findings.append(
                Finding(
                    "NO_LEDGER_TASK", model_id, f"OBB2-{source}-{spec.model} is not a ledger task"
                )
            )
        if entry is None:
            findings.append(
                Finding("NO_SHEET_ENTRY", model_id, "no interface facts were extracted")
            )
            findings.extend(model_findings)
            rows.append(f"{model_id:40} sheet=missing")
            continue
        packages.setdefault(source, package_source(upstream_root, source))
        model_findings += (
            columns_disagree(spec, entry)
            + params_disagree(spec, entry)
            + endpoint_disagree(spec, packages[source])
        )
        findings.extend(model_findings)
        codes = sorted({f.code for f in model_findings if f.code in VIOLATIONS})
        rows.append(
            f"{model_id:40} cols={len(declared_columns(spec))}/{len(entry['output_columns'])} "
            f"params={len(spec.params)}/{len(entry['query_params'])} "
            f"sha={str(entry['spec_sha256'])[:12]} violations={len(codes)}"
        )
        if codes:
            rows.append(f"{'':40} -> {', '.join(codes)}")
    return findings, rows


def self_test_spec() -> ModelSpec:
    """One correct declaration for the self-test to mutate."""
    return ModelSpec(
        model="SelfTest",
        domain="self_test",
        asset_class="equity",
        period="snapshot",
        market="us",
        base_url="https://example.test",
        path="/api/v1/quotes/{symbol}.json",
        rows_pointer="data",
        params=(ParamSpec("symbol", "str", required=True),),
        columns=(
            ColumnSpec("bid", "float", required=True),
            ColumnSpec("ask", "float", required=True),
        ),
        scenario="自检：证明每项判定既能放行也能翻红",
        error_prefix="SELFTEST",
    )


def self_test_entry() -> SheetEntry:
    """The upstream facts the self-test declaration is correct against."""
    return {
        "output_columns": ["bid", "ask"],
        "query_params": [{"name": "symbol", "type": "str", "default": None}],
        "spec_sha256": "0" * 64,
    }


SELF_TEST_PACKAGE = 'URL = f"https://example.test/api/v1/quotes/{symbol}.json"'

#: A registry the self-test can be correct against, plus one unreviewed domain to stand in for a
#: legacy entry. ``self_test`` mirrors the declaration's own columns; the contract comparison is
#: armed by a column the fixture contract does not publish.
SELF_TEST_DOMAINS: dict[str, tuple[bool, frozenset[str]]] = {
    "self_test": (True, frozenset({"bid", "ask"})),
    "self_test_legacy": (False, frozenset()),
}


def run_self_test() -> tuple[int, int, list[str]]:
    """Show each check accepts the correct declaration and refuses each targeted fabrication.

    Returns the number of arms, the number that fired as expected, and the notes for any that did
    not. A non-empty note list is a bug in this file, not in the declarations.
    """
    spec, entry, package = self_test_spec(), self_test_entry(), SELF_TEST_PACKAGE
    domains = SELF_TEST_DOMAINS
    notes: list[str] = []
    arms = 0
    fired = 0

    clean = [
        finding
        for finding in (
            domain_disagree(spec, domains)
            + columns_disagree(spec, entry)
            + params_disagree(spec, entry)
            + endpoint_disagree(spec, package)
        )
        if finding.code in VIOLATIONS
    ]
    if clean:
        notes.append(f"the correct declaration was itself refused: {[f.code for f in clean]}")

    cases: list[tuple[str, list[Finding], str]] = [
        (
            "column dropped from the declaration",
            columns_disagree(replace(spec, columns=(spec.columns[0],)), entry),
            "COLUMN_MISSING",
        ),
        (
            "column added to the declaration",
            columns_disagree(
                replace(spec, columns=(*spec.columns, ColumnSpec("invented", "str"))), entry
            ),
            "COLUMN_EXTRA",
        ),
        (
            "parameter invented",
            params_disagree(
                replace(spec, params=(*spec.params, ParamSpec("invented", "str"))), entry
            ),
            "PARAM_UNBACKED",
        ),
        (
            "column carried only by the row envelope",
            columns_disagree(
                replace(
                    spec,
                    columns=(*spec.columns, ColumnSpec("listing_currency", "str")),
                    row_envelope=("listing_currency",),
                ),
                entry,
            ),
            "COLUMN_FROM_ENVELOPE",
        ),
        (
            "host fabricated",
            endpoint_disagree(replace(spec, base_url="https://fabricated.test"), package),
            "HOST_UNBACKED",
        ),
        (
            "path fabricated",
            endpoint_disagree(replace(spec, path="/api/v1/nope/{symbol}.json"), package),
            "PATH_UNBACKED",
        ),
        (
            "domain nobody registered",
            domain_disagree(replace(spec, domain="self_test_absent"), domains),
            "DOMAIN_UNREGISTERED",
        ),
        (
            "domain carries no reviewed semantics",
            domain_disagree(replace(spec, domain="self_test_legacy"), domains),
            "DOMAIN_NO_SEMANTICS",
        ),
        (
            "columns differ from the domain's contract",
            domain_disagree(
                replace(spec, columns=(*spec.columns, ColumnSpec("invented", "str"))), domains
            ),
            "CONTRACT_COLUMNS_DISAGREE",
        ),
    ]
    for label, produced, expected in cases:
        arms += 1
        codes = {finding.code for finding in produced}
        if expected in codes:
            fired += 1
        else:
            notes.append(f"mutation {label!r} did not produce {expected} (got {sorted(codes)})")
    return arms, fired, notes


def read_ledger_ids(ledger: Path) -> set[str]:
    """Collect the task ids the iteration-2 ledger fixes."""
    with ledger.open(encoding="utf-8-sig", newline="") as handle:
        return {row["task_id"] for row in csv.DictReader(handle) if row.get("task_id")}


def main(argv: list[str] | None = None) -> int:
    """Audit every declared engine model, after proving the audit can fail."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM_PATH)
    parser.add_argument("--ledger", type=Path, default=REPO_ROOT / LEDGER_RELATIVE_PATH)
    parser.add_argument("--source", default=None, help="restrict the audit to one provider")
    parser.add_argument("--out", type=Path, default=None, help="write a machine-readable report")
    parser.add_argument(
        "--expect-declared",
        type=int,
        default=None,
        help="fail when the declared population is smaller than this count",
    )
    args = parser.parse_args(argv)

    arms, fired, notes = run_self_test()
    print(f"self-test: correct declaration accepted, mutating arms={arms} fired={fired}")
    for note in notes:
        print(f"  SELF-TEST BROKEN: {note}")
    if fired != arms or notes:
        print("VERDICT: SELF-TEST FAILED (this judge is not yet proven on both sides)")
        return 1

    from opendata.data.providers.catalog import engine_declared_models

    models = engine_declared_models()
    if args.source is not None:
        models = [(source, spec) for source, spec in models if source == args.source]
    print(f"declared models: {len(models)}")
    if args.expect_declared is not None and len(models) < args.expect_declared:
        print(
            f"FAIL: declared={len(models)} expected at least={args.expect_declared} "
            "(a shrinking declared population is not a passing audit)"
        )
        return 1

    sheet = extract(args.upstream)
    ledger_ids = read_ledger_ids(args.ledger)
    print(
        f"upstream: {args.upstream} pinned={UPSTREAM_COMMIT[:12]} "
        f"sheet providers={len(sheet)} models={sum(len(p['models']) for p in sheet.values())} "
        f"ledger tasks={len(ledger_ids)}"
    )
    findings, rows = audit(models, sheet, ledger_ids, args.upstream)
    for row in rows:
        print(row)
    for finding in findings:
        print(finding.line())
    violations = [finding for finding in findings if finding.code in VIOLATIONS]

    if args.out is not None:
        payload = {
            "upstream_commit": UPSTREAM_COMMIT,
            "sheet_sha256": hashlib.sha256(json.dumps(sheet, sort_keys=True).encode()).hexdigest(),
            "declared_models": [f"{source}::{spec.model}" for source, spec in models],
            "self_test": {"arms": arms, "fired": fired},
            "findings": [{"code": f.code, "model": f.model, "detail": f.detail} for f in findings],
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"machine-readable report -> {args.out}")

    print(
        f"models={len(models)} findings={len(findings)} violations={len(violations)} "
        f"info={len(findings) - len(violations)} self_test={fired}/{arms}"
    )
    if violations:
        print("VERDICT: DECLARATION PROVENANCE FAILS")
        return 1
    print("VERDICT: DECLARATION PROVENANCE HOLDS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
