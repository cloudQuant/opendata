"""Wave 21 falsification probe for the ``sec::InsiderTrading`` carrier.

Six arms, each printing a verdict line rather than raising, because a reading that escapes as a
traceback is a crash report and not evidence:

* ``A_control`` -- the carrier as committed, run through the runner's own declared seam. The face
  is the count: 24 seam legs and 11 rows.
* ``B_grant`` -- delete one archive leg. The carrier's request grant is derived from its own
  answerable bodies (``iter2_claims_probe_runner._carrier_sends``), so 23 legs is below the
  ``1 + MAX_FEED_PAGES + MAX_FORM4_FILINGS * 2`` the fetcher preflights, and the source must refuse
  with ``SEC_REQUEST_BUDGET_EXCEEDED`` *before* the first request. This is the arm that turns
  ``seam_requests=24`` from a coincidence into a bound.
* ``C_page_shape`` -- grow the third Atom page to the full ``MAX_FEED_PAGE_SIZE``. Nothing then ends
  the pagination walk, so the fetcher must refuse with ``SEC_INSIDER_WINDOW_INCOMPLETE``: the
  100/100/40 shape of the committed carrier is load-bearing, and the third page is really asked for.
* ``D1_needle_census`` -- a static census: the twenty archive needles must be exactly the twenty
  URLs the source builds from its own references (``_filing_urls`` plus the index-resolved document
  name), and the URL formed by borrowing the next filing's document name must not be among them.
* ``D2_live_arm`` -- repoint one filing's index at that borrowed name and fetch. The carrier refuses
  it. One face here is worth stating rather than smoothing over: the fake client raises a
  ``WitnessFailure`` naming the uncarried URL, but ``fetch_bytes`` wraps every non-budget exception
  of the governed-client branch into ``SEC_TRANSPORT_ERROR``, so what the provider reports is a
  transport refusal. The claim under test -- a document that another filing published cannot
  enter the row set -- holds either way, and the consequence is that a mis-declared carrier is
  attributable only as a transport code, not as the seam's own message.
* ``E_pairing_census`` -- walk every produced row: ``security_title`` carries the accession of the
  filing that published it (in both ownership tables), ``accession_number`` has ten distinct values
  equal to the ten the carrier carries, and the two ``security_type`` literals both occur. A
  permuted carrier would keep producing 11 rows and fail exactly this census.

Run with ``PYTHONPATH=. python`` followed by this file's path.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

from scripts.quality import iter2_claims_fixtures as fixtures  # noqa: E402
from scripts.quality import iter2_claims_probe_runner as runner  # noqa: E402

PROVIDER: str = "sec"
MODEL: str = "InsiderTrading"
SHARD = REPO_ROOT / "scripts/quality/iter2_witnesses/_parts/sec/insider_rows.py"


def _load_shard() -> Any:  # noqa: ANN401  # the shard is a witness file loaded by location
    spec = importlib.util.spec_from_file_location("w21_insider_shard", SHARD)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"shard {SHARD} has no importable spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry() -> dict[str, Any]:
    """One witness entry exactly as the probe runner builds it: hosts and credentials included."""
    config = fixtures.provider_config(PROVIDER)
    entry = fixtures.model_entry(PROVIDER, MODEL)
    entry["hosts"] = config["allowed_hosts"]
    for name, value in config.get("credential_env", {}).items():
        import os

        os.environ.setdefault(name, value)
    return entry


def _fetch(entry: dict[str, Any], document: Any) -> tuple[list[Any], int]:  # noqa: ANN401
    """Run the committed fetcher against one carrier through the declared seam; return rows/legs."""
    fetcher_type = runner._resolve_fetcher_type(PROVIDER, MODEL)
    ctx = runner._grant_context(
        PROVIDER, MODEL, entry["hosts"], sends=runner._carrier_sends(entry, document)
    )
    with runner._client_seam(entry, document) as client:
        rows = list(fetcher_type().fetch(ctx=ctx, **entry["query"]))
    return rows, len(client.calls)


def _verdict(arm: str, note: str, ok: bool) -> bool:
    print(f"{arm}: {'PASS' if ok else 'FAIL'} {note}")
    return ok


def main() -> int:
    """Run every arm, print one verdict line each, and exit 0 only when all six pass."""
    runner._block_real_sockets()
    shard = _load_shard()
    entry = _entry()
    base = entry["fixture_document"]()
    results: list[bool] = []

    # --- A: the committed carrier -------------------------------------------------------------
    rows, legs = _fetch(entry, base)
    grant = runner._carrier_sends(entry, base)
    preflight = 1 + shard.MAX_FEED_PAGES + shard.MAX_FORM4_FILINGS * 2
    results.append(
        _verdict(
            "A_control",
            f"seam_legs={legs} rows={len(rows)} carrier_sends={grant} preflight_demands={preflight}"
            f" grant_equals_preflight={grant == preflight == legs}",
            legs == preflight and grant == preflight and len(rows) == 11,
        )
    )

    # --- B: one leg short of the preflight ----------------------------------------------------
    short = copy.deepcopy(base)
    short["responses"].pop()
    try:
        _fetch(entry, short)
    except Exception as exc:  # the refusal under test is the source's own
        code = getattr(exc, "code", None)
        results.append(
            _verdict(
                "B_grant",
                f"carrier_sends={runner._carrier_sends(entry, short)}"
                f" refused_with={type(exc).__name__}/{code}",
                code == "SEC_REQUEST_BUDGET_EXCEEDED",
            )
        )
    else:
        results.append(_verdict("B_grant", "a 23-leg carrier still completed the fetch", False))

    # --- C: a third page that never ends the walk ---------------------------------------------
    tail = copy.deepcopy(base)
    page = tail["responses"][1]["sequence"][2]
    extra = [
        shard._feed_entry(
            "10-K",
            shard._iso(5),
            f"{shard._ISSUER_CIK}-26-{800 + i:06d}",
        )
        for i in range(shard.MAX_FEED_PAGE_SIZE - shard._TAIL_PAGE_ROWS)
    ]
    tail["responses"][1]["sequence"][2] = page[:-1] + extra + page[-1:]
    try:
        _fetch(entry, tail)
    except Exception as exc:
        code = getattr(exc, "code", None)
        results.append(
            _verdict(
                "C_page_shape",
                f"page_rows={[len(p) - 4 for p in tail['responses'][1]['sequence']]}"
                f" refused_with={type(exc).__name__}/{code}",
                code == "SEC_INSIDER_WINDOW_INCOMPLETE",
            )
        )
    else:
        results.append(_verdict("C_page_shape", "three full pages still ended the walk", False))

    # --- D: a document that is not the one its filing publishes -------------------------------
    # D1 is a static census over the carrier: every archive needle must be the URL that filing's
    # own index resolves to, and a borrowed name must therefore address a URL nobody declared.
    needles = [spec["url_contains"] for spec in base["responses"][2:]]
    own_urls = []
    for filing in shard._filings():
        prefix = shard._archive_prefix(filing)
        own_urls.append(f"{prefix}/index.json")
        own_urls.append(f"{prefix}/{filing['primary_document']}")
    borrowed = shard._filings()[1]["primary_document"]
    borrowed_url = f"{shard._archive_prefix(shard._filings()[0])}/{borrowed}"
    results.append(
        _verdict(
            "D1_needle_census",
            f"declared_archive_needles={len(needles)} urls_the_fetcher_builds={len(own_urls)}"
            f" needles_equal_urls={sorted(needles) == sorted(own_urls)}"
            f" borrowed_url_carried={borrowed_url in needles}",
            len(needles) == len(own_urls) == 2 * shard.MAX_FORM4_FILINGS
            and sorted(needles) == sorted(own_urls)
            and borrowed_url not in needles,
        )
    )

    mispaired = copy.deepcopy(base)
    first_index = mispaired["responses"][2]
    first_index["body"]["directory"]["item"][1]["name"] = borrowed
    try:
        _fetch(entry, mispaired)
    except Exception as exc:
        # The seam's own WitnessFailure names the uncarried URL, but fetch_bytes wraps every
        # non-budget exception of the governed-client branch as SEC_TRANSPORT_ERROR, so that name
        # is what the provider reports, not what the fake said. Both faces are printed here: the
        # claim under test is that a borrowed document cannot enter the row set, and the readable
        # consequence of the catch-all is that a mis-declared carrier is attributable only as a
        # transport refusal.
        results.append(
            _verdict(
                "D2_live_arm",
                f"raised={type(exc).__name__} code={getattr(exc, 'code', None)} "
                f"seam_message_swallowed={type(exc) is not runner.WitnessFailure}",
                type(exc).__name__ == "SecProviderError"
                and getattr(exc, "code", None) == "SEC_TRANSPORT_ERROR",
            )
        )
    else:
        results.append(_verdict("D2_live_arm", "a borrowed document still answered", False))

    # --- E: the pairing census over every produced row ----------------------------------------
    titles = [row.security_title for row in rows]
    accessions = [row.accession_number for row in rows]
    carried = {filing["accession"] for filing in shard._filings()}
    types = Counter(row.security_type for row in rows)
    paired = all(title.endswith(acc) for title, acc in zip(titles, accessions, strict=True))
    results.append(
        _verdict(
            "E_pairing_census",
            f"rows={len(rows)} distinct_accessions={len(set(accessions))}"
            f" carried_filings={len(carried)} title_names_own_accession={paired}"
            f" security_type={dict(types)}",
            paired
            and len(set(accessions)) == len(carried) == shard.MAX_FORM4_FILINGS
            and set(accessions) == carried
            and set(types) == {"non_derivative", "derivative"},
        )
    )

    passed = sum(1 for ok in results if ok)
    failed = len(results) - passed
    print(f"carrier_arms={len(results)} carrier_passed={passed} carrier_failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
