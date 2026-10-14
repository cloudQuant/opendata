"""C103: census the live-vs-pin ``scenario_present`` moves against the batch5 oracle.

``tests/test_provider_domains.py:1784`` asserts, per pinned identity row, that the live binding's
scenario presence equals ``row["scenario_present"]``.  The snapshot itself carries an
``iter2_census_rebase`` block that describes that field as a *documented diff* ("adding or removing
one scenario string moves scenario_present_total.live and field_move_counts.scenario_present
together"), while ``contract`` and ``verified`` are pinned at zero moves.  This probe re-measures
the diff from the live registry so the ruling on the guard rests on numbers, not on prose.

Read-only: it imports the registry and hashes nothing but itself.

Usage: python docs/evidence/C103/batch5-scenario-census/probes/scenario_census.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()
sys.path.insert(0, str(REPO))

SNAPSHOT = REPO / "tests/fixtures/registry_batch5_snapshot.json"


def main() -> int:
    from opendata.data.providers import list_providers, register_providers

    register_providers()
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = snapshot["provider_model_identities"]

    # Mirrors tests/test_provider_domains.py:1774-1779 exactly.
    static_bindings = {
        (provider.source, model_id): binding
        for provider in list_providers()
        for binding in provider.fetcher_bindings
        for model_id in (binding.canonical_model_ids or (binding.model_key,))
    }
    print(f"face: snapshot_rows={len(rows)} live_bindings={len(static_bindings)}")
    identity_sets_equal = set(static_bindings) == {(r["source"], r["model"]) for r in rows}
    print(f"face: identity_sets_equal={identity_sets_equal}")

    missing = [(r["source"], r["model"]) for r in rows if (r["source"], r["model"]) not in static_bindings]
    print(f"face: rows_without_live_binding={len(missing)}")

    moves: list[tuple[str, str, bool, bool]] = []
    for row in rows:
        key = (row["source"], row["model"])
        binding = static_bindings.get(key)
        if binding is None:
            continue
        live_scenario = binding.scenario is not None
        if live_scenario is not bool(row["scenario_present"]):
            moves.append((row["source"], row["model"], bool(row["scenario_present"]), live_scenario))

    # ``contract`` and ``verified`` are not fields of FetcherBinding, so a scenario re-base cannot
    # move them; printing a getattr fallback here would have asserted a zero without measuring one.
    import dataclasses

    print(f"face: binding_fields={sorted(f.name for f in dataclasses.fields(type(binding)))}")

    by_source = Counter(source for source, _m, _b, _l in moves)
    print(f"face: scenario_move_rows={len(moves)}")
    print(f"face: scenario_moves_by_source={dict(sorted(by_source.items()))}")
    print(f"face: live_scenario_present_total={sum(1 for r in rows if (r['source'], r['model']) in static_bindings and static_bindings[(r['source'], r['model'])].scenario is not None)}")
    print(f"face: pin_scenario_present_total={sum(1 for r in rows if r['scenario_present'])}")

    rebase = snapshot.get("iter2_census_rebase", {})
    documented = rebase.get("scenario_moves_by_source", {})
    totals = rebase.get("scenario_present_total", {})
    field_moves = rebase.get("field_move_counts", {})
    print(f"face: documented_scenario_moves_by_source={dict(sorted(documented.items()))}")
    print(f"face: documented_scenario_present_total={totals}")
    print(f"face: documented_field_move_counts={field_moves}")
    documented_matches_live = dict(sorted(by_source.items())) == dict(sorted(documented.items()))
    print(f"face: documented_matches_live={documented_matches_live}")
    print(f"face: move_directions={dict(Counter('on' if live and not pin else 'off' for _s, _m, pin, live in moves))}")
    for source, model, pin, live in moves[:6]:
        print(f"face: example {source}/{model} pin={pin} live={live}")

    population_ok = identity_sets_equal and not missing
    scenario_census_exit = 0 if population_ok else 1
    if not population_ok:
        print("verdict: REFUSED - the live registry does not cover the pinned identity set")
    else:
        print(
            f"verdict: MEASURED - {len(moves)} scenario moves over {len(rows)} pinned identities, "
            f"documented census {'matches' if documented_matches_live else 'does not match'} live"
        )
    print(f"scenario_census_exit={scenario_census_exit}")
    return scenario_census_exit


if __name__ == "__main__":
    raise SystemExit(main())
