"""Per-row transport and domain-key map for the tmx residue, measured on both codebases.

Why both sides matter
---------------------
This is a clean-room implementation: the upstream package is the *output contract* reference,
not the code we ship.  So "does this row need a new transport?" is a question about OUR fetchers,
and it is answered by measuring them -- while the upstream call graph answers the different
question "where does the data actually come from", which is a design input, not a constraint.
The two are not the same: two of the eight rows this provider has already landed
(``EquitySearch``, ``IndexConstituents``) scrape ``www.tsx.com`` / ``tmxinfoservices.com`` upstream
yet were landed here through the one GraphQL host our client knows.  A first take of this probe
classified on upstream hosts alone and its own calibration arm caught that
(``landed_served_on_seam=6 of_accepted=8``); that carrier was overwritten
before it was named, so arm 7 re-derives the divergence from files on every run.  The shipped
instrument therefore measures our transport and reports upstream separately.

What is measured
----------------
* Ours: every landed row has a ``<Model>Fetcher`` class in ``models/fetchers.py`` and its only
  call is the seam method (``client.graphql``); every residue row has no fetcher class at all --
  which is what ``implement_missing_model`` means.  The client file's host census is printed, so
  the seam is a measured property of the shipped code, not of this narrative.
* Upstream: model -> fetcher (from the CSV-pinned ``fetcher_dict`` block) -> module -> the helper
  functions it actually calls -> request hosts, following helper->helper calls transitively.  Only
  the ``https://`` literals inside code are counted: triple-quoted blocks and ``Origin``/``Referer``
  header lines are dropped, because a docstring that names a page is not a request to it.
* Domain keys: the predicate is ``tmx_<snake(model)>``, the exact shape of all eight keys this
  provider has, calibrated on those eight (all must hit) and reported for the residue (none may
  hit).  Sibling providers' keys for the same model are listed with their declared ``contract``
  classes, so "no reusable key exists" is shown rather than asserted.

Unmapped models, unresolved classes, empty host sets and missing fetcher modules are counted and
fail the run instead of reading as zeros.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

from scripts.quality import iter2_model_evidence as judge  # noqa: E402

CLAIMS_REL = "docs/evidence/C101/claims.merged.json"
DOMAINS_REL = "opendata/data/domains.yaml"
WITNESS_REL = "scripts/quality/iter2_witnesses/tmx.py"
OUR_FETCHERS_REL = "opendata/data/providers/tmx/models/fetchers.py"
OUR_CLIENT_REL = "opendata/data/providers/tmx/models/client.py"
UPSTREAM_PLATFORM = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform")
HOST_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")
ENTRY_RE = re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*([A-Za-z_][\w]*)')
DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][\w]*)", re.M)
SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")
DOCSTRING_RE = re.compile(r'"""(?:.*?|)"""|\'\'\'(?:.*?|)\'\'\'', re.S)
#: Header lines that name a page for the server but are not the host being requested.
HEADER_NOISE_RE = re.compile(r"\b(origin|referer)\b", re.I)


def _snake(name: str) -> str:
    return SNAKE_RE.sub("_", name).lower()


def _load_witness_seam(path: Path) -> tuple[str, str]:
    """Import the shipped witness module and read its seam host + intercepted attribute from it."""
    spec = importlib.util.spec_from_file_location("witness_tmx_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"witness module not importable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._GQL_HOST), str((module._SEAM or {}).get("attr"))


def _csv_shape_files(provider: str) -> dict[str, str]:
    """Read the upstream shape-file column the judge's 3-field InventoryRow drops."""
    raw = (REPO_ROOT / judge.DEFAULT_INVENTORY).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    return {
        r["upstream_model"]: r["upstream_shape_file"] for r in reader if r["provider"] == provider
    }


def _functions(text: str) -> dict[str, str]:
    """Split a module into function bodies keyed by name (module level gets key ``<module>``)."""
    marks = [(m.group(1), m.start()) for m in DEF_RE.finditer(text)]
    out: dict[str, str] = {"<module>": text[: marks[0][1]] if marks else text}
    for index, (name, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        out[name] = text[start:end]
    return out


def _request_hosts(body: str) -> set[str]:
    hosts: set[str] = set()
    for line in DOCSTRING_RE.sub("", body).splitlines():
        if HEADER_NOISE_RE.search(line):
            continue
        hosts.update(h.rstrip(".").lower() for h in HOST_RE.findall(line))
    return hosts


def _closure(roots: set[str], bodies: dict[str, dict[str, str]]) -> set[str]:
    """Follow helper->helper calls transitively so a wrapper's host is not missed."""
    seen: set[str] = set()
    known = {name for names in bodies.values() for name in names}
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        body = next((b[name] for b in bodies.values() if name in b), "")
        calls = re.findall(r"\b([a-z_][\w]*)\s*\(", body)
        stack.extend([call for call in calls if call in known])
    return seen


def _our_transport(models: list[str]) -> dict[str, dict[str, Any]]:
    """Measure, in our repo, which fetcher class exists and which client methods it calls."""
    source = (REPO_ROOT / OUR_FETCHERS_REL).read_text(encoding="utf-8")
    marks = [(m.group(1), m.start()) for m in re.finditer(r"^class\s+(\w+)", source, re.M)]
    bodies: dict[str, str] = {}
    for index, (name, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(source)
        bodies[name] = source[start:end]
    out: dict[str, dict[str, Any]] = {}
    for model in models:
        cls = f"{model}Fetcher"
        body = bodies.get(cls, "")
        calls = sorted(set(re.findall(r"client\.(\w+)\(", body))) if body else []
        out[model] = {
            "class": cls,
            "exists": cls in bodies,
            "client_calls": calls,
            "lines": len(body.splitlines()),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    """Measure both transports and print the seven-arm face line."""
    ap = argparse.ArgumentParser(description="tmx residue transport + domain map")
    ap.add_argument("--provider", default="tmx")
    ap.add_argument("--upstream-root", default=str(UPSTREAM_PLATFORM))
    args = ap.parse_args(argv)

    upstream_root = Path(args.upstream_root)
    seam_host, seam_attr = _load_witness_seam(REPO_ROOT / WITNESS_REL)
    client_source = (REPO_ROOT / OUR_CLIENT_REL).read_text(encoding="utf-8")
    client_hosts = sorted(_request_hosts(client_source))
    client_methods = sorted(
        {
            m
            for m in re.findall(r"^(?:    )?def (\w+)", client_source, re.M)
            if not m.startswith("_")
        }
    )
    print(f"our_client file={OUR_CLIENT_REL} hosts={client_hosts} methods={client_methods}")
    print(
        f"witness_seam host={seam_host} attr={seam_attr} witness={WITNESS_REL} "
        f"host_in_client={seam_host in client_hosts}"
    )

    claims_path = REPO_ROOT / CLAIMS_REL
    doc = json.loads(claims_path.read_text(encoding="utf-8"))
    inventory = judge.load_inventory(REPO_ROOT / judge.DEFAULT_INVENTORY)
    report = judge.build_report(
        inventory,
        doc,
        claims_sha256=judge.sha256_bytes(claims_path.read_bytes()),
        claims_path=CLAIMS_REL,
    )
    accepted = {
        m["upstream_model"]
        for m in report["models"]
        if m["status"] == "OFFLINE_ACCEPTED" and m["provider"] == args.provider
    }
    rows = [r for r in inventory["rows"] if r["provider"] == args.provider]
    residue = [r for r in rows if r["upstream_model"] not in accepted]
    print(
        f"population: provider={args.provider} inventory_rows={len(rows)} "
        f"accepted={len(accepted)} residue={len(residue)}"
    )

    domains = (yaml.safe_load((REPO_ROOT / DOMAINS_REL).read_text(encoding="utf-8")) or {}).get(
        "domains"
    ) or {}
    provider_keys = sorted(k for k in domains if k.startswith(f"{args.provider}_"))
    print(f"domain_keys: total={len(domains)} provider_keys={len(provider_keys)}")

    shape_map = _csv_shape_files(args.provider)
    judge_ids = {r["upstream_model"] for r in rows}
    shape_files = sorted({shape_map[m] for m in judge_ids if m in shape_map})
    print(
        f"population_join: csv={len(shape_map)} judge={len(judge_ids)} "
        f"set_equal={set(shape_map) == judge_ids} shape_files={shape_files}"
    )
    shape_file = shape_files[0]

    pkg_dir = upstream_root / Path(shape_file).parent
    models_dir = pkg_dir / "models"
    bodies: dict[str, dict[str, str]] = {}
    hosts: dict[str, dict[str, set[str]]] = {}
    for key, rel in (("helpers", "utils/helpers.py"), ("gql", "utils/gql.py")):
        path = pkg_dir / rel
        if not path.is_file():
            continue
        parsed = _functions(path.read_text(encoding="utf-8"))
        bodies[key] = parsed
        hosts[key] = {name: _request_hosts(body) for name, body in parsed.items()}
        print(
            f"upstream_source {key} file={rel} functions={len(parsed) - 1} "
            f"module_hosts={sorted(hosts[key].get('<module>', []))}"
        )

    init_text = (pkg_dir / Path(shape_file).name).read_text(encoding="utf-8")
    start = init_text.find("fetcher_dict={")
    block = init_text[start : init_text.find("}", start)] if start >= 0 else ""
    fetchers = dict(ENTRY_RE.findall(block))
    unmapped = sorted(m for m in judge_ids if m not in fetchers)
    print(
        f"fetcher_dict_parse: entries={len(fetchers)} inventory_rows={len(rows)} "
        f"unmapped={unmapped} models_dir={models_dir.is_dir()}"
    )

    ours = _our_transport(sorted(judge_ids))

    def classify(model: str) -> dict[str, Any]:
        fetcher = fetchers.get(model, "")
        module = None
        if fetcher:
            pattern = re.compile(rf"^class\s+{re.escape(fetcher)}\b", re.M)
            for file in sorted(models_dir.glob("*.py")):
                if pattern.search(file.read_text(encoding="utf-8")):
                    module = file
                    break
        roots: list[str] = []
        up_hosts: set[str] = set()
        if module is not None:
            source = module.read_text(encoding="utf-8")
            roots = sorted(
                {
                    name
                    for key in bodies
                    for name in bodies[key]
                    if name != "<module>" and re.search(rf"\b{re.escape(name)}\s*\(", source)
                }
            )
            closure = _closure(set(roots), bodies)
            up_hosts = _request_hosts(source) | {
                h for name in closure for key in hosts for h in hosts[key].get(name, set())
            }
        return {
            "model": model,
            "up_fetcher": fetcher,
            "up_module": module.name if module else "MISSING",
            "roots": roots,
            "up_hosts": up_hosts,
            "up_graphql": seam_host in up_hosts,
            "ours": ours[model],
        }

    def line(item: dict[str, Any]) -> str:
        key = f"{args.provider}_{_snake(item['model'])}"
        siblings = sorted(
            k for k in domains if k.endswith(f"_{_snake(item['model'])}") and k != key
        )
        contracts = sorted({str((domains[k] or {}).get("contract")) for k in siblings})
        return (
            f"row={item['model']} ours_exists={item['ours']['exists']} "
            f"ours_lines={item['ours']['lines']} "
            f"ours_client_calls={item['ours']['client_calls']} "
            f"| upstream fetcher={item['up_fetcher']} "
            f"module={item['up_module']} roots={item['roots']} hosts={sorted(item['up_hosts'])} "
            f"upstream_uses_seam_host={item['up_graphql']} "
            f"| domain_key={key} exists={key in domains} "
            f"sibling_keys={siblings} sibling_contracts={contracts}"
        )

    print("-- landed rows (calibration set, measured in our repo) --")
    landed = [classify(m) for m in sorted(accepted)]
    for item in landed:
        print(line(item))
    print("-- residue rows --")
    items = [classify(r["upstream_model"]) for r in residue]
    for item in items:
        print(line(item))

    landed_implemented = [
        i["model"]
        for i in landed
        if i["ours"]["exists"]
        and i["ours"]["client_calls"]
        and set(i["ours"]["client_calls"]) <= {"graphql"}
    ]
    residue_implemented = [i["model"] for i in items if i["ours"]["exists"]]
    arm2 = (
        len(landed_implemented) == len(accepted) and not residue_implemented and len(accepted) > 0
    )
    print(
        f"arm2_ours: landed_fetcher_on_graphql={len(landed_implemented)} "
        f"of_accepted={len(accepted)} "
        f"residue_with_fetcher={len(residue_implemented)} of_residue={len(items)}"
    )

    arm3 = len(client_hosts) == 1 and client_hosts[0] == seam_host
    print(f"arm3_client: hosts={client_hosts} seam={seam_host} single_host_client={arm3}")

    up_gql = [i["model"] for i in items if i["up_graphql"]]
    up_other = [i["model"] for i in items if not i["up_graphql"]]
    arm4 = len(up_gql) > 0 and len(up_other) > 0 and len(up_gql) + len(up_other) == len(items)
    print(
        f"arm4_upstream_source: via_seam_host={len(up_gql)} "
        f"via_other_host_only={len(up_other)} of={len(items)} "
        f"gql_ids={up_gql} other_ids={up_other}"
    )

    landed_without_key = [
        m for m in sorted(accepted) if f"{args.provider}_{_snake(m)}" not in domains
    ]
    residue_with_key = [
        i["model"] for i in items if f"{args.provider}_{_snake(i['model'])}" in domains
    ]
    arm5 = not landed_without_key and not residue_with_key and len(accepted) > 0
    print(
        f"arm5_domain_key: landed_without_key={landed_without_key} "
        f"residue_with_key={residue_with_key} "
        f"residue_without_key={len(items) - len(residue_with_key)} of={len(items)}"
    )

    unresolved = sorted(i["model"] for i in landed + items if i["up_module"] == "MISSING")
    no_hosts = sorted(i["model"] for i in landed + items if not i["up_hosts"])
    no_roots = sorted(i["model"] for i in landed + items if not i["roots"])
    arm6 = not unmapped and not unresolved and not no_hosts and not no_roots
    print(
        f"arm6_resolution: unmapped={unmapped} unresolved={unresolved} "
        f"no_hosts={no_hosts} no_roots={no_roots}"
    )

    # Arm 7 keeps the clean-room divergence on disk forever.  A previous take classified landed
    # rows by *upstream* hosts alone and its calibration arm read
    # landed_served_on_seam=6 of_accepted=8; that
    # carrier was overwritten before it was named, so this arm re-derives the same number from
    # files instead of leaving it quoted from a console.  Divergent rows are ones we landed through
    # the GraphQL seam although upstream scrapes another host -- proof that upstream transport is a
    # design input, not a constraint, and the reason the residue's "extra hosts" list is not a
    # blocker list.
    divergent = sorted(i["model"] for i in landed if not i["up_graphql"])
    arm7 = 0 < len(divergent) < len(accepted)
    print(
        f"arm7_clean_room_divergence: landed_rows_upstream_not_on_seam_host={divergent} "
        f"count={len(divergent)} of_accepted={len(accepted)} PASS={arm7}"
    )

    passed = sum([arm2, arm3, arm4, arm5, arm6, arm7, len(residue) + len(accepted) == len(rows)])
    extra = sorted({h for i in items for h in i["up_hosts"] if h != seam_host})
    print(
        f"transport_map_face provider={args.provider} residue={len(items)} "
        f"needs_new_domain_key={len(items) - len(residue_with_key)} "
        f"residue_without_our_fetcher={len(items) - len(residue_implemented)} "
        f"upstream_via_seam_host={len(up_gql)} upstream_via_other_host={len(up_other)} "
        f"extra_hosts={extra} "
        f"arms=7 arms_passed={passed} arms_failed={7 - passed} PASS={passed == 7}"
    )
    print(f"tmx_residue_transport_map_exit={0 if passed == 7 else 1}")
    return 0 if passed == 7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
