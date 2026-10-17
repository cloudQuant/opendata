"""Measure whether a residual row has a *post-response* refusal for a counterfact to aim at.

Wave 25 armed a three-row batch on reachability alone and landed two rows. The row that fell out
(`government_us::WeatherBulletinDownload`) was seam-reachable; what it lacked was a guard that a
tampered document can trip. Its nine raise sites all fire on the query, before any transport, and
the shipped counterfact mechanism tampers the fixture *document* (`tamper.path` starts at
`responses`). So reachability is one axis and witnessability is another, and the second axis was
never measured -- it was assumed to follow from the first. It does not.

This census measures the second axis over every provider package on disk. It records every ``raise``
site whose first argument is a SCREAMING_SNAKE string literal (that literal is either the refusal
code itself or, for the ``_fail("SUFFIX")`` factory shape, its suffix) and buckets it by the method
that holds it:

* ``transform_data`` -- the engine protocol hands this method the decoded payload
  (``transform_data(self, raw, params)``, ``_engine/http_json.py:1462``), so a document tamper
  reaches a refusal here by construction.
* ``extract_data`` -- document-adjacent but ambiguous: this wave landed
  ``commodity_psd_report.py:104`` (a magic-byte check on the fetched body) *and* carries
  ``weather_bulletin_download.py:112`` (a guard that fires before any transport). A method name
  cannot tell those apart, so the count is reported separately and never merged into the strict one.
* ``other`` -- every other method, class helper or module function, including a helper that
  ``transform_data`` happens to call. A witness cannot reach these by tampering a response body.

Three arms keep the counters honest. Arm A takes the refusal codes of every row the judge already
accepted -- read out of the landed report, none typed here -- and requires each to resolve to a
raise site in its own provider package. Arm B takes the accepted rows of one provider and requires
their codes to resolve into a document method, while ``weather_bulletin_download.py`` contributes
zero strict sites: the instrument has to accept the rows the wave landed and refuse the row the
wave refused, on the same rule. Arm C plants a code that exists nowhere and requires the resolver to
call it absent, because an ``absent=0`` reading is only worth anything once the counter can be
non-zero.

Exits non-zero when an arm fails. ``td=0`` for a provider is a *screen*, not a verdict: the strict
count is a floor, since a refusal in a helper called from ``transform_data`` lands in ``other``.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seam_surface_match as seam  # shipped report reader, fake surface, verb collector

PROVIDERS_DIR = seam.REPO_ROOT / "opendata" / "data" / "providers"
DOCUMENT_METHODS = ("transform_data", "extract_data")
MIN_CODE_LEN = 8


def _is_code_literal(value: str) -> bool:
    """A refusal code is SCREAMING_SNAKE; a message is a sentence."""
    return (
        len(value) >= MIN_CODE_LEN and value == value.upper() and value.replace("_", "").isalnum()
    )


def _error_name(node: ast.Raise) -> str:
    exc = node.exc
    func = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return "<indirect>"


def _first_literal(node: ast.Raise) -> str:
    """The code literal a raise carries, or the literal *suffix* of a composed code.

    Two shapes have to resolve: ``raise Err("CODE")`` and the engine's
    ``raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")``, where only the suffix is a
    literal. For the second this returns ``SHAPE_INVALID`` and the matcher ends up comparing by
    ``code.endswith(suffix)``, which is exactly how the runtime composes the code.
    """
    exc = node.exc
    if not isinstance(exc, ast.Call) or not exc.args:
        return ""
    arg = exc.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value if _is_code_literal(arg.value) else ""
    if isinstance(arg, ast.JoinedStr) and arg.values:
        tail = arg.values[-1]
        if isinstance(tail, ast.Constant) and isinstance(tail.value, str):
            suffix = tail.value.strip("_")
            if _is_code_literal(suffix):
                return suffix
    return ""


def _own_raises(func: ast.AST) -> list[ast.Raise]:
    """Raise nodes in ``func`` itself, excluding those in the nested functions it defines."""
    own = [node for node in ast.walk(func) if isinstance(node, ast.Raise)]
    for nested in _nested_funcs(func):
        for node in ast.walk(nested):
            if isinstance(node, ast.Raise) and node in own:
                own.remove(node)
    return own


def _nested_funcs(func: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not func
    ]


def _module_sites(tree: ast.Module, rel: str) -> list[dict[str, Any]]:
    """One record per raise site carrying a code-shaped literal, tagged with its document bucket."""
    sites: list[dict[str, Any]] = []

    def add(cls: str, outer: str, holder: str, node: ast.Raise) -> None:
        lit = _first_literal(node)
        if not lit:
            return
        sites.append(
            {
                "file": rel,
                "class": cls,
                "method": holder,
                "outer": outer,
                "bucket": outer if outer in DOCUMENT_METHODS else "other",
                "error": _error_name(node),
                "code": lit,
                "line": node.lineno,
            }
        )

    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [b for b in cls.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in _own_raises(fn):
                add(cls.name, fn.name, fn.name, node)
            for nested in _nested_funcs(fn):
                for node in _own_raises(nested):
                    add(cls.name, fn.name, f"{fn.name}.<nested:{nested.name}>", node)
    for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in _own_raises(fn):
            add("<module>", fn.name, fn.name, node)
    return sites


def _package_sites(provider: str) -> dict[str, Any]:
    """Raise sites in the provider package, bucketed, plus its raw text."""
    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    files = 0
    text: list[str] = []
    for module_path in sorted((PROVIDERS_DIR / provider).rglob("*.py")):
        files += 1
        body = module_path.read_text("utf-8")
        text.append(body)
        try:
            tree = ast.parse(body, filename=str(module_path))
        except SyntaxError:
            continue
        rel = str(module_path.relative_to(seam.REPO_ROOT))
        for site in _module_sites(tree, rel):
            buckets[site["bucket"]].append(site)
    return {"buckets": dict(buckets), "files": files, "text": "\n".join(text)}


def _resolve(packed: dict[str, Any], code: str, engine: dict[str, list[dict[str, Any]]]) -> str:
    """Where a claimed refusal code is raised: td, ed, other, engine, text_only, absent."""
    best = ""
    order = {"other": 1, "extract_data": 2, "transform_data": 3}
    for bucket, sites in packed["buckets"].items():
        for site in sites:
            lit = site["code"]
            same = code == lit or (len(lit) >= MIN_CODE_LEN and code.endswith(lit))
            if same and order.get(bucket, 0) > order.get(best, 0):
                best = bucket
    if best:
        return best
    hits = (s["code"] for sites in engine.values() for s in sites)
    if any(code == lit or code.endswith(lit) for lit in hits):
        return "engine"
    return "text_only" if code in packed["text"] else "absent"


def _engine_sites() -> dict[str, list[dict[str, Any]]]:
    """Raise sites in the shared engine: declarative specs get their guards there, not per package.

    ``_engine/http_json.py:629`` raises the composed
    ``ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")``, so the code a declarative row
    refuses with exists in no provider file at all. Reading that as
    "no guard exists" would be the same mistake as reading an unmet class as a met one.
    """
    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for module_path in sorted((PROVIDERS_DIR / "_engine").rglob("*.py")):
        try:
            tree = ast.parse(module_path.read_text("utf-8"), filename=str(module_path))
        except SyntaxError:
            continue
        rel = str(module_path.relative_to(seam.REPO_ROOT))
        for site in _module_sites(tree, rel):
            buckets[site["bucket"]].append(site)
    return dict(buckets)


def main() -> int:
    """Print the per-provider guard screen plus the three control arms."""
    report = seam._load_report()
    judged = seam._accepted_by_provider(report)
    packages = sorted(p.name for p in PROVIDERS_DIR.iterdir() if (p / "__init__.py").is_file())
    surface = seam._fake_surface()

    accepted_codes: list[tuple[str, str, str]] = []
    residual_rows: defaultdict[str, list[str]] = defaultdict(list)
    for entry in report["models"]:
        provider, _, model = str(entry["model_identity"]).partition("::")
        cf = (entry.get("phases") or {}).get("counterfact") or {}
        code = (cf.get("counterfact") or {}).get("expected", {}).get("refusal_code")
        status = str(entry.get("status") or "")
        if cf.get("claim_status") == "PASS" and code:
            accepted_codes.append((provider, model, str(code)))
        if status != "OFFLINE_ACCEPTED":
            residual_rows[provider].append(f"{model}:{status}")

    sites_by_provider = {p: _package_sites(p) for p in packages}
    engine = _engine_sites()
    empty = {"buckets": {}, "text": ""}
    resolution = {
        c[:2]: _resolve(sites_by_provider.get(c[0], empty), c[2], engine) for c in accepted_codes
    }
    resolved = Counter(resolution.values())
    codes_by_provider: defaultdict[str, list[str]] = defaultdict(list)
    for provider, _model, _code in accepted_codes:
        codes_by_provider[provider].append(resolution[(provider, _model)])

    print(
        "provider_screen: td=transform_data ed=extract_data other=query-path/helper "
        "(raise sites carrying a code-shaped literal; td>0 is a screen, not a verdict)"
    )
    residual_units = 0
    residual_rows_total = 0
    residual_rows_no_package = 0
    for provider in packages:
        statuses = judged.get(provider, {})
        total = sum(statuses.values())
        residual = total - statuses.get("OFFLINE_ACCEPTED", 0)
        if not residual:
            continue
        residual_units += 1
        residual_rows_total += residual
        buckets = sites_by_provider[provider]["buckets"]
        verbs = sorted(seam._transport_verbs(provider))
        reachable = bool(verbs) and not (set(verbs) - surface)
        td = len(buckets.get("transform_data", []))
        ed = len(buckets.get("extract_data", []))
        other = len(buckets.get("other", []))
        land = Counter(codes_by_provider.get(provider, []))
        if td:
            verdict = "GUARD_PRESENT"
        elif ed:
            verdict = "GUARD_DOCUMENT_ADJACENT"
        elif land.get("engine"):
            verdict = "GUARD_ENGINE_ONLY"
        else:
            verdict = "GUARD_STRICT_ZERO"
        print(
            f"guard_provider={provider} residual={residual} seam_verbs={verbs} "
            f"seam_reachable={reachable} td={td} ed={ed} other={other} "
            f"modules={sites_by_provider[provider]['files']} screen={verdict} "
            f"landed_code_resolutions={dict(sorted(land.items()))} "
            f"td_classes={sorted({s['class'] for s in buckets.get('transform_data', [])})[:4]} "
            f"residual_rows={sorted(residual_rows[provider])[:5]}"
        )

    missing_packages = sorted(set(judged) - set(packages))
    for provider in missing_packages:
        statuses = judged[provider]
        residual = sum(statuses.values()) - statuses.get("OFFLINE_ACCEPTED", 0)
        if not residual:
            continue
        residual_units += 1
        residual_rows_total += residual
        residual_rows_no_package += residual
        print(
            f"guard_provider={provider} residual={residual} seam_verbs=NA seam_reachable=False "
            f"td=NA ed=NA other=NA modules=0 screen=NO_PACKAGE_ON_DISK "
            f"landed_code_resolutions={{}} td_classes=[] "
            f"residual_rows={sorted(residual_rows[provider])[:5]}"
        )

    absent = sorted({c[:2] for c in accepted_codes if resolution[c[:2]] == "absent"})
    a_pass = not absent
    print(
        f"A_landed_codes: population={len(accepted_codes)} "
        f"distinct={len({c[2] for c in accepted_codes})} "
        f"providers={len({c[0] for c in accepted_codes})} "
        f"engine_sites={sum(len(v) for v in engine.values())} "
        f"td={resolved['transform_data']} ed={resolved['extract_data']} other={resolved['other']} "
        f"engine={resolved['engine']} text_only={resolved['text_only']} absent={len(absent)} "
        f"absent_sample={absent[:5]} PASS={a_pass}"
    )

    gov = [c for c in accepted_codes if c[0] == "government_us"]
    gov_doc = [c for c in gov if resolution[c[:2]] in ("transform_data", "extract_data")]
    gov_file = "models/weather_bulletin_download.py"
    gov_buckets = sites_by_provider["government_us"]["buckets"]
    wbd = [s for b in gov_buckets.values() for s in b if gov_file in s["file"]]
    wbd_td = [s for s in wbd if s["bucket"] == "transform_data"]
    w_b_pass = len(gov) > 0 and len(gov_doc) == len(gov) and not wbd_td
    print(
        f"B_two_sided: government_us accepted_rows={len(gov)} codes_in_document_methods="
        f"{len(gov_doc)} code_buckets={sorted({resolution[c[:2]] for c in gov})} "
        f"weather_bulletin_download.py td={len(wbd_td)} "
        f"ed={len([s for s in wbd if s['bucket'] == 'extract_data'])} "
        f"other={len([s for s in wbd if s['bucket'] == 'other'])} PASS={w_b_pass}"
    )

    planted = ("government_us", "CODE_THE_PROBE_JUST_MADE_UP")
    planted_read = _resolve(sites_by_provider["government_us"], planted[1], engine)
    c_pass = planted_read == "absent"
    print(
        f"C_planted_negative: planted={planted[1]} resolves_to={planted_read} "
        f"counter_can_read_nonzero={c_pass}"
    )

    face: Counter[str] = Counter()
    for provider in packages:
        statuses = judged.get(provider, {})
        if statuses.get("OFFLINE_ACCEPTED", 0) == sum(statuses.values()):
            continue
        td = len(sites_by_provider[provider]["buckets"].get("transform_data", []))
        face["strict_positive" if td else "strict_zero"] += 1
    arms_passed = int(a_pass) + int(w_b_pass) + int(c_pass)
    print(
        f"guard_face residual_units={residual_units} residual_rows={residual_rows_total} "
        f"residual_rows_no_package={residual_rows_no_package} "
        f"packages={len(packages)} strict_positive_providers={face['strict_positive']} "
        f"strict_zero_providers={face['strict_zero']} arms=3 arms_passed={arms_passed} "
        f"arms_failed={3 - arms_passed} "
        f"identity_set={str(report['inventory']['expected_identity_set_sha256'])[:16]}"
    )
    return 0 if arms_passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
