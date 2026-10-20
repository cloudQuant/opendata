"""Wave-40 face: the catalog's two query-schema channels, and the two runtime defects wave 40 fixes.

``/api/v1/data/catalog`` publishes a capability's parameters from
``opendata.services.provider_models.resolve_query_model_type(fetcher)``, which reads the static
``query_model`` / ``query_type`` declaration and never evaluates an annotation expression.
``tests/test_data_catalog.py`` cross-checks that published surface against a *second* channel --
``get_type_hints(type(fetcher).transform_query)["return"]`` -- which does evaluate it. Where the
annotation names the concrete model the two agree; where it names ``BaseModel`` or a stale narrower
model they do not, and an annotation that references a ``TYPE_CHECKING``-only import cannot be
evaluated at all.

Six arms, each paired with something that has to be able to differ:

* A -- the population. Every registered model descriptor is compared on both channels, and the three
  outcome classes must sum to the denominator (an unexplained remainder is a bug in this probe).
* B -- the repaired defect class, reproduced on demand: re-evaluating an annotation with the runtime
  name deleted raises ``NameError``, and with it present resolves. That is the mechanism the two
  repaired adapters had, shown in both directions rather than remembered.
* C/D -- the divergence partition by provider and its price: how many merged claims go stale per
  provider if every diverging adapter in that provider were annotated concretely. This is the number
  an owner needs before choosing between a per-provider cadence and one tree-wide pass.
* E -- the two rows this wave actually repaired, read back through both channels.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any, get_type_hints

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
BUNDLE = "docs/evidence/C146/wave40-query-annotation-divergence-20261019"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w95_query_annotation_face.txt"
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
W76 = (
    REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/probes/w76_cboe_resign.py"
)
REPAIRED = {
    "tradingeconomics::EconomicCalendar": (
        "opendata/data/providers/tradingeconomics/models/economic_calendar.py",
        "_EconomicCalendarQuery",
    ),
    "benzinga::PriceTarget": (
        "opendata/data/providers/benzinga/models/price_target.py",
        "BaseModel",
    ),
}
BASE_CLASS = "pydantic.main.BaseModel"


def _params(model: Any) -> list[dict[str, Any]]:  # noqa: ANN401  # mirrors the catalog's own builder
    return [
        {
            "name": name,
            "type": getattr(field.annotation, "__name__", str(field.annotation)),
            "required": field.is_required(),
            "description": field.description,
        }
        for name, field in (getattr(model, "model_fields", {}) or {}).items()
    ]


def _channels() -> tuple[list[dict[str, str]], dict[str, list[str]], dict[str, int]]:
    """Compare both channels for every registered descriptor and classify the differences."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry
    from opendata.services.provider_models import resolve_query_model_type

    register_providers()
    registry = get_registry()
    rows: list[dict[str, str]] = []
    shapes: dict[str, list[str]] = {"base_model": [], "missing_fields": [], "other": []}
    outcomes: dict[str, int] = {}
    for descriptor in registry.list_model_descriptors():
        source, name = descriptor.source, descriptor.model
        key = f"{source}::{name}"
        fetcher = registry.resolve_model(source, name)
        try:
            published = _params(resolve_query_model_type(fetcher))
        except Exception as exc:  # the face reports the class, never a silent zero
            outcomes[f"published_channel:{type(exc).__name__}"] = (
                outcomes.get(f"published_channel:{type(exc).__name__}", 0) + 1
            )
            rows.append({"key": key, "class": "published_channel_error"})
            continue
        try:
            annotated = _params(get_type_hints(type(fetcher).transform_query).get("return"))
        except Exception as exc:
            outcomes[f"annotation_channel:{type(exc).__name__}"] = (
                outcomes.get(f"annotation_channel:{type(exc).__name__}", 0) + 1
            )
            rows.append({"key": key, "class": "annotation_unresolvable"})
            continue
        if published == annotated:
            outcomes["identical"] = outcomes.get("identical", 0) + 1
            rows.append({"key": key, "class": "identical"})
            continue
        outcomes["diverge"] = outcomes.get("diverge", 0) + 1
        published_names = [item["name"] for item in published]
        annotated_names = [item["name"] for item in annotated]
        if not annotated:
            shape = "base_model"
        elif set(annotated_names) < set(published_names):
            shape = "missing_fields"
        else:
            shape = "other"
        shapes[shape].append(key)
        rows.append(
            {
                "key": key,
                "class": "diverge",
                "shape": shape,
                "module": str(type(fetcher).__module__),
                "only_published": ",".join(n for n in published_names if n not in annotated_names),
            }
        )
    return rows, shapes, outcomes


def _forge_arm(key: str) -> dict[str, Any]:
    """Show the repaired mechanism both ways: name present resolves, absent raises."""
    rel, name = REPAIRED[key]
    source, model_name = key.split("::")
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    fetcher = get_registry().resolve_model(source, model_name)
    function = type(fetcher).transform_query
    namespace = sys.modules[function.__module__].__dict__
    present = "resolved"
    try:
        get_type_hints(function).get("return")
    except NameError as exc:
        present = f"NameError:{exc}"
    held = namespace.pop(name, None)
    absent = "resolved"
    try:
        get_type_hints(function)
    except NameError as exc:
        absent = f"NameError:{str(exc).splitlines()[0]}"
    finally:
        if held is not None:
            namespace[name] = held
    return {
        "name": name,
        "with_name": present,
        "without_name": absent,
        "restored": namespace.get(name) is held or held is None,
    }


def _radius_over(claims: list[dict[str, Any]], paths: set[str]) -> dict[str, Any]:
    """Count pin sites of ``paths`` per owning provider over whatever claim list is given."""
    owners: dict[str, int] = {}
    sites = 0
    for claim in claims:
        for group in (claim.get("pins") or {}).values():
            entries = group if isinstance(group, list) else [group]
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("path") or "") in paths:
                    owners[str(claim.get("provider"))] = (
                        owners.get(str(claim.get("provider")), 0) + 1
                    )
                    sites += 1
    return {
        "diverging_modules": len(paths),
        "pin_sites": sites,
        "claims_by_provider": owners,
        "providers": len(owners),
        "total_claims": len(claims),
        "tree_wide": len(owners) == 1 and sites > 0,
    }


def _diverging_paths(rows: list[dict[str, str]]) -> set[str]:
    modules = {row["module"] for row in rows if row.get("class") == "diverge"}
    return {
        module.replace(".", "/") + ".py" for module in modules if module.startswith("opendata.")
    }


def _pin_radius(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Count the merged claims that pin each diverging module, grouped by owning provider."""
    claims = json.loads(CLAIMS.read_text(encoding="utf-8")).get("claims") or []
    return _radius_over(claims, _diverging_paths(rows))


def _forged_radius(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Re-read the same predicate over a claim set owned by one provider, so tree-wide is definable.

    If every merged claim belonged to a single provider and pinned every diverging module, the
    repair price would be one tree-wide pass. The forged set says exactly that; the real set must
    disagree with it for "provider-local" to be a measurement rather than a hope.
    """
    claims = json.loads(CLAIMS.read_text(encoding="utf-8")).get("claims") or []
    paths = _diverging_paths(rows)
    forged = [
        {"provider": "synthetic_owner", "pins": {"source": [{"path": path} for path in sorted(paths)]}}
        for _claim in claims
    ]
    return _radius_over(forged, paths)


def _repaired_rows() -> dict[str, Any]:
    out: dict[str, Any] = {}
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry
    from opendata.services.provider_models import resolve_query_model_type

    register_providers()
    registry = get_registry()
    for key in REPAIRED:
        source, model_name = key.split("::")
        fetcher = registry.resolve_model(source, model_name)
        try:
            annotated = get_type_hints(type(fetcher).transform_query).get("return")
            state = "resolvable"
        except NameError as exc:
            annotated = None
            state = f"NameError:{exc}"
        published = _params(resolve_query_model_type(fetcher))
        derived = _params(annotated) if annotated is not None else []
        out[key] = {
            "state": state,
            "annotation_class": getattr(annotated, "__qualname__", str(annotated)),
            "published_fields": len(published),
            "annotated_fields": len(derived),
            "channels_agree": published == derived,
        }
    return out


def _w76_reader() -> Any:  # noqa: ANN401  # wave 39's corrected pin reader is this face's instrument
    spec = importlib.util.spec_from_file_location("w76_pin_reader", W76)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import pin reader {W76}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w76_pin_reader"] = module
    spec.loader.exec_module(module)
    return module


def _repair_digests() -> dict[str, Any]:
    """Prove the two repaired files are pinned by their own provider only, and freshly signed."""
    reader = _w76_reader()
    reader.PIN_TARGETS = tuple(rel for rel, _name in REPAIRED.values())
    face: dict[str, Any] = reader._stale_pin_face()
    face["live_digests"] = {rel: reader._live_pins(rel)[:16] for rel in reader.PIN_TARGETS}
    return face


def main() -> int:
    """Run the six arms and write the carrier."""
    RUNS.mkdir(parents=True, exist_ok=True)
    rows, shapes, outcomes = _channels()
    total = len(rows)
    lines: list[str] = []
    arms: dict[str, bool] = {}

    summed = sum(outcomes.values())
    lines.append(
        f"A_population descriptors={total} outcomes={json.dumps(outcomes, sort_keys=True)}"
    )
    lines.append(
        "A_divergence_shapes "
        + json.dumps({shape: len(keys) for shape, keys in shapes.items()}, sort_keys=True)
    )
    arms["A_denominator_closes"] = summed == total == 350

    forges = {key: _forge_arm(key) for key in REPAIRED}
    for key, reading in forges.items():
        lines.append(f"B_forge {key} {json.dumps(reading, sort_keys=True)}")
    arms["B_mechanism_bites_both_ways"] = all(
        reading["without_name"].startswith("NameError")
        and reading["with_name"] == "resolved"
        and reading["restored"]
        for reading in forges.values()
    )

    provider_diverge: dict[str, int] = {}
    for row in rows:
        if row.get("class") == "diverge":
            source = row["key"].split("::")[0]
            provider_diverge[source] = provider_diverge.get(source, 0) + 1
    lines.append(f"C_diverge_by_provider={json.dumps(provider_diverge, sort_keys=True)}")
    arms["C_no_unexplained_divergence_class"] = sum(provider_diverge.values()) == outcomes.get(
        "diverge", -1
    ) and sum(len(keys) for keys in shapes.values()) == outcomes.get("diverge", -2)

    radius = _pin_radius(rows)
    lines.append(f"D_pin_radius={json.dumps(radius, sort_keys=True, default=str)}")
    forged = _forged_radius(rows)
    lines.append(f"D_forged_radius={json.dumps(forged, sort_keys=True, default=str)}")
    arms["D_radius_is_provider_local_not_tree_wide"] = (
        radius["providers"] >= 2
        and radius["pin_sites"] == sum(radius["claims_by_provider"].values())
        and max(radius["claims_by_provider"].values()) < radius["total_claims"]
        # The same predicate over a claim set in which one provider pins every diverging module has
        # to read tree-wide; without that arm "provider-local" would be a guess about the future.
        and forged["tree_wide"] is True
    )

    repaired = _repaired_rows()
    for key, reading in repaired.items():
        lines.append(f"E_repaired {key} {json.dumps(reading, sort_keys=True)}")
    arms["E_repaired_rows_resolve"] = all(
        reading["state"] == "resolvable" for reading in repaired.values()
    )
    arms["E_calendar_rows_now_agree"] = repaired["tradingeconomics::EconomicCalendar"][
        "channels_agree"
    ]

    digests = _repair_digests()
    lines.append(f"F_signed_face={json.dumps(digests, sort_keys=True, default=str)}")
    arms["F_repaired_files_signed_and_provider_local"] = digests[
        "stale_sites_total"
    ] == 0 and sorted(digests["claims_touching_targets"]) == ["benzinga", "tradingeconomics"]

    head = subprocess.run(  # nosec B603
        ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    lines.insert(0, f"head={head} python={PY}")
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"query_annotation_divergence arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w95_query_annotation_face_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w95_query_annotation_face_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
