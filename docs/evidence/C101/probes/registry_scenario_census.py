"""Grade every registry model for a consuming scenario, and show the judge bites.

``openbb_map._project_default_map`` refuses to build the compatibility map when a registered
binding declares no scenario (AC-10 admission). That refusal is all-or-nothing: it raises on the
first offender, so one missing scenario hides every other one and takes down each of the
351 parametrized conformance cases plus 9 map/registry tests. This probe enumerates the whole
population instead of stopping at the first raise, so the repair can be graded by "no_scenario
== 0" and not by one test file turning green.

Arm ``--arm`` copies one live OECD binding with ``scenario=None`` in memory and requires the
projection to raise ``OpenBBMapError`` mentioning "has no scenario": without it, an empty
offender list would be indistinguishable from a projection that never checks at all.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections import Counter
from typing import Any

from opendata.data.openbb_map import OpenBBMapError, load_openbb_map
from opendata.data.providers import catalog, register_providers
from opendata.data.registry import get_registry


def offenders() -> list[tuple[str, str]]:
    """Return every ``(source, model)`` whose resolved binding has no scenario."""
    bad: list[tuple[str, str]] = []
    for descriptor in get_registry().list_model_descriptors():
        try:
            provider = catalog.get_provider(descriptor.source)
        except Exception:
            bad.append((descriptor.source, descriptor.model))
            continue
        binding = provider.fetcher_dict.get(descriptor.model)
        if binding is None or binding.scenario is None:
            bad.append((descriptor.source, descriptor.model))
    return bad


def run_arm() -> int:
    """Blank one live OECD scenario in memory and require the projection to refuse."""
    provider = catalog.get_provider("oecd")
    model, binding = "CompositeLeadingIndicator", provider.fetcher_dict["CompositeLeadingIndicator"]
    blanked = dataclasses.replace(binding, scenario=None)
    patched = dataclasses.replace(
        provider,
        fetcher_bindings=tuple(
            blanked if item is binding else item for item in provider.fetcher_bindings
        ),
    )
    catalog._PROVIDERS_BY_SOURCE["oecd"] = patched
    try:
        try:
            load_openbb_map()
            outcome, detail = "NO_RAISE", ""
        except OpenBBMapError as exc:
            outcome, detail = "RAISED", str(exc)
    finally:
        catalog._PROVIDERS_BY_SOURCE["oecd"] = provider
    restored = catalog.get_provider("oecd").fetcher_dict[model]
    bites = (
        outcome == "RAISED"
        and "has no scenario" in detail
        and restored.scenario == binding.scenario
    )
    print(f"arm: oecd/{model} scenario=None -> {outcome}: {detail[:78]}")
    print(
        f"arm: restored_scenario_chars={len(restored.scenario or '')} -> "
        f"{'PASS' if bites else 'FAIL'}"
    )
    return 0 if bites else 1


def main(argv: list[str] | None = None) -> int:
    """Print the scenario census, or run the falsification arm."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="run the falsification arm only")
    args = parser.parse_args(argv)
    register_providers()

    if args.arm:
        return run_arm()

    bad = offenders()
    descriptors = len(get_registry().list_model_descriptors())
    capabilities = len(get_registry().capabilities())
    entries = load_openbb_map()
    covered: set[Any] = {(o.provider, o.domain) for e in entries for o in e.ours}
    registered = {(c.source, c.domain) for c in get_registry().capabilities()}
    print(
        f"population: descriptors={descriptors} capabilities={capabilities}"
        f" map_entries={len(entries)}"
    )
    print(
        f"face: no_scenario={len(bad)} covered_pairs={len(covered & registered)}"
        f" registry_pairs={len(registered)} uncovered={len(registered - covered)}"
        f" overcovered={len(covered - registered)}"
    )
    for source, model in sorted(bad):
        print(f"  OFFENDER {source}/{model}")
    if bad:
        print(f"offenders by source: {dict(Counter(s for s, _ in bad).most_common())}")
    return 0 if not bad and not (registered - covered) else 2


if __name__ == "__main__":
    raise SystemExit(main())
