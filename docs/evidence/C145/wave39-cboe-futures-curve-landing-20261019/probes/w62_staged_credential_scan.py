"""Staged-payload credential scan for the wave-39 commit, on wave 38's shipped instrument.

The instrument reads the payload the way git will commit it (``git show :<path>``), materializes
it to
scratch, and runs three rule sets over it twice each -- shipped config, allow-list-stripped config,
built-in rules -- with one fabricated token planted into a copy so a reported zero means a scan that
could have spoken. Re-using it keeps that discipline intact instead of restating it.

Only two constants are re-pointed, and both are armed here before the scan runs: the carrier path
(the instrument computes it from its own ``__file__``, so without this the wave-39 reading would
overwrite wave 38's committed carrier) and the declared wave path set, which is what makes the
``scope`` arm mean "nothing outside this wave is staged" rather than a restatement of the diff.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO
    / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes"
    / "w41b_staged_credential_scan.py"
)
#: Wave 39's declared path set: the landed cboe row's model and adapter, the provider descriptor and
#: the refusal ledger it empties, the domain key, the new witness shard, the two cboe
#: enumerations its
#: registration rotates, the fence and its four consumers, the regenerated claim tree, and the two
#: evidence bundles this wave writes.
WAVE_PREFIXES: tuple[str, ...] = (
    "opendata/data/domains.yaml",
    "opendata/data/models/",
    "opendata/data/providers/cboe/",
    "scripts/quality/iter2_witnesses/_parts/cboe/futures.py",
    "tests/test_cboe_engine_provider.py",
    "tests/test_congress_cboe_iter2_cboe.py",
    "tests/test_authority_reconciliation.py",
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
    "tests/fixtures/registry_batch5_snapshot.json",
    "docs/evidence/C101/",
    "docs/evidence/C144/",
    "docs/evidence/C145/",
)


def _load_instrument() -> Any:  # noqa: ANN401  # wave 38's scanner is this probe's instrument
    """Import the shipped scanner and re-point its carrier and declared path set."""
    spec = importlib.util.spec_from_file_location("w38_staged_scan", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import staged credential scanner {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_staged_scan"] = module
    spec.loader.exec_module(module)
    module.CARRIER = RUNS / "w62_staged_credential_scan.txt"
    module.WAVE_PREFIXES = WAVE_PREFIXES
    return module


class _Tee:
    """Keep the run's stdout on the terminal and in memory, so the carrier is the printed face."""

    def __init__(self, stream: Any) -> None:  # noqa: ANN401  # the replaced sys.stdout stream
        self.stream = stream
        self.buffer: list[str] = []

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.buffer.append(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


def main() -> int:
    """Arm the re-point, run the shipped scan over whatever is staged, and keep its stdout."""
    RUNS.mkdir(parents=True, exist_ok=True)
    instrument = _load_instrument()
    carrier = Path(str(instrument.CARRIER))
    inside = str(carrier).startswith(str(RUNS))
    prefixes = tuple(instrument.WAVE_PREFIXES)
    armed = prefixes == WAVE_PREFIXES and inside
    print(
        f"re_point carrier={carrier} inside_bundle={inside} wave_prefixes={len(prefixes)} "
        f"instrument={INSTRUMENT.name}",
        flush=True,
    )
    if not armed:
        print(f"w62_staged_credential_scan_exit=1 # re_point_failed inside={inside}")
        return 1
    # The shipped scanner tees its own stdout into a carrier only under ``__main__``; imported and
    # called, it prints and writes nothing. Re-pointing its ``CARRIER`` alone therefore produced a
    # scan with no archive, so the same discipline is applied here: collect the run verbatim and
    # write it under this bundle's name.
    tee = _Tee(sys.stdout)
    sys.stdout = tee
    try:
        rc = int(instrument.main())
    finally:
        sys.stdout = sys.__stdout__
        header = (
            "# carrier for w62_staged_credential_scan.py (scanner stdout, verbatim)\n"
            f"# argv: {sys.executable} {Path(__file__).resolve()}\n"
        )
        carrier.write_text(header + "".join(tee.buffer) + f"w62_staged_credential_scan_exit={rc}\n")
    print(f"w62_reused_instrument={INSTRUMENT.name} scan_returned={rc}", flush=True)
    print(f"w62_staged_credential_scan_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
