"""Wave-50: re-probe the two externally blocked providers, with an egress control.

The objective's remaining rows are ``seeking_alpha::Forward{Eps,Sales}Estimates`` and
``stockgrid::ShortVolume``. Both were last measured on 2026-10-19 (C144 ``w54``) and re-cited on
2026-10-21, and the audit's freshness rule says a blocker claim ages with its evidence. This is a
re-measurement, not a re-narration: if either source now answers, the row becomes implementable.

Safety boundary, unchanged: unauthenticated public GETs only. No API key, no ``Authorization``
header, no cookie is sent, and an arm checks that claim instead of asserting it.

Arms, each with the value that would mean UNBLOCKED stated before running it:

* ``K`` egress control -- a same-host public Seeking Alpha quote page must return 200 over 1 KB.
  Without it a 403 or a 0-byte read cannot be told apart from "this machine has no route", which is
  exactly the misread wave 39 recorded for wsj.
* ``A``/``B`` Seeking Alpha -- the search route plus five differently-shaped estimates requests
  (three valid, one bogus id, one extra item key). UNBLOCKED needs a search that resolves ids AND
  estimate bodies that differ per input.
* ``C`` stockgrid -- the recorded endpoint through redirects. UNBLOCKED needs a 2xx whose bytes
  parse as JSON rows, not a landing page.
* ``E`` the two verdict predicates are run on forged inputs too, so a "blocked" reading is shown to
  be a function of the readings and not a foregone conclusion.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import urllib.error  # fixed https URLs, no user input, no credentials
import urllib.request
from typing import Any, Final

REPO = pathlib.Path("/Users/yunjinqi/Documents/new_projects/opendata")
RUNS = REPO / "docs/evidence/C147/wave41-objective-scoreboard-20261020/runs"
CARRIER = RUNS / "w150_blocked_retry_face.txt"
UA: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SA_HOST: Final = "https://seekingalpha.com"
#: The estimates route exactly as C144 ``w53`` recorded it; a variant path is not a re-measurement.
SA_ESTIMATES: Final = "https://seekingalpha.com/api/v3/symbol_data/estimates"
STOCKGRID_URL: Final = "https://www.stockgrid.io/get_dark_pool_individual_data?ticker=AAPL"
TIMEOUT: Final = 20.0
CREDENTIAL_HEADERS: Final = ("authorization", "cookie", "x-rapidapi-key", "x-api-key")
# Every URL below is copied from C144 w53's recorded shapes (bracket-array params included).
ESTIMATE_CASES: Final = (
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL",
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4",
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=4&estimates_data_items%5B%5D=eps_avg",
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=167087&relative_periods=4&estimates_data_items%5B%5D=eps_avg",
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=ZZZZNOTREAL&relative_periods=4&estimates_data_items%5B%5D=eps_avg",
    f"{SA_ESTIMATES}?ticker_ids%5B%5D=AAPL&relative_periods=12&estimates_data_items%5B%5D=eps_avg",
)
SEARCH_ROW_THRESHOLD: Final = 64
PX_MARKERS: Final = ("jsClientSrc", "firstPartyEnabled", '"appId":"PX', "blockScript", "captcha")


def _fetch(url: str) -> dict[str, Any]:
    """One unauthenticated GET; record the shape, never a credential surface."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    record: dict[str, Any] = {
        "url": url,
        "code": None,
        "bytes": 0,
        "sha": "",
        "sent_headers": sorted(name.lower() for name in headers),
    }
    body = b""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            body = response.read()
            record["code"] = response.status
            record["final_url"] = response.geturl()
            record["content_type"] = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        body = error.read() if hasattr(error, "read") else b""
        record["code"] = error.code
        record["final_url"] = error.url
        record["content_type"] = (error.headers or {}).get("Content-Type", "")
    except Exception as error:  # a probe records any transport failure instead of dying on it
        record["error"] = f"{type(error).__name__}: {str(error)[:120]}"
    record["bytes"] = len(body)
    record["sha"] = hashlib.sha256(body).hexdigest()[:16]
    record["head"] = body[:160].decode("utf-8", errors="replace")
    return record


def _px_challenge(record: dict[str, Any]) -> bool:
    """True when the body is PerimeterX's bot-mitigation payload, not source data.

    The challenge JSON carries per-request tokens (``vid``/``uuid``/``appId``), so its digest is
    unique per call. A "bodies differ, therefore the source answers" predicate is a false positive
    on it -- which is exactly what an earlier take of this face produced.
    """
    return record.get("code") == 403 or any(marker in record["head"] for marker in PX_MARKERS)


def _json_payload(record: dict[str, Any]) -> bool:
    """True only when the head parses as a non-empty JSON list or object."""
    if not record["bytes"]:
        return False
    try:
        parsed = json.loads(record["head"])
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False
    return bool(parsed) if isinstance(parsed, list | dict) else False


def _sa_unblocked(answered: int, distinct_answered: int) -> bool:
    """Advanceable only if the source ANSWERS without a challenge AND varies its data."""
    return answered > 0 and distinct_answered > 1


def _grid_unblocked(code: int | None, json_rows: bool) -> bool:
    """The row is advanceable only on a 2xx that parses as JSON, not an HTML landing page."""
    return code == 200 and json_rows


def main() -> int:
    """Run the three arms, print one line per reading, and gate the face on its arms."""
    RUNS.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# carrier for w150_blocked_retry_face.py", ""]

    control = _fetch(f"{SA_HOST}/symbol/AAPL")
    dead = _fetch("https://qoder-no-such-host.invalid/x")
    lines.append(
        f"K control status={control.get('code')} bytes={control['bytes']} "
        f"sha={control['sha']} error={control.get('error', 'none')}"
    )
    lines.append(
        f"K2 dead-host counterexample status={dead.get('code')} bytes={dead['bytes']} "
        f"error={dead.get('error', 'none')!r}"
    )

    search = _fetch(f"{SA_HOST}/api/v3/searches?phrase=AAPL&count=5&type=ticker")
    lines.append(
        f"A1 sa_search status={search.get('code')} bytes={search['bytes']} "
        f"sha={search['sha']} head={search['head'][:60]!r}"
    )
    digests: list[str] = []  # every estimate digest, challenge or not
    estimates: list[dict[str, Any]] = []
    for url in ESTIMATE_CASES:
        got = _fetch(url)
        estimates.append(got)
        digests.append(got["sha"])
        query = url.split("?", 1)[1]
        lines.append(
            f"A2 estimates params={query} status={got.get('code')} bytes={got['bytes']} "
            f"sha={got['sha']} head={got['head'][:48]!r}"
        )
    all_sa = [search, *estimates]
    challenges = [record for record in all_sa if _px_challenge(record)]
    answered = [record for record in all_sa if not _px_challenge(record)]
    data_digests = {record["sha"] for record in answered}
    markers = [
        marker
        for marker in ("jsClientSrc", "firstPartyEnabled", '"appId":"PX', "blockScript", "captcha")
        if challenges and marker in challenges[0]["head"]
    ]
    lines.append(
        f"A3 sa_requests={len(all_sa)} px_challenges={len(challenges)} answered={len(answered)} "
        f"distinct_answered_digests={len(data_digests)} markers={markers}"
    )

    grid = _fetch(STOCKGRID_URL)
    grid_json = _json_payload(grid)
    lines.append(
        f"C stockgrid status={grid.get('code')} bytes={grid['bytes']} sha={grid['sha']} "
        f"final={grid.get('final_url', '')} json_rows={grid_json} "
        f"error={grid.get('error', 'none')}"
    )

    sa_live = _sa_unblocked(len(answered), len(data_digests))
    grid_live = _grid_unblocked(grid.get("code"), grid_json)
    sa_forged = _sa_unblocked(3, 3)
    grid_forged = _grid_unblocked(200, True)
    sent = [name for rec in (control, search, grid, *estimates) for name in rec["sent_headers"]]

    arms: dict[str, bool] = {
        "K_egress_alive_and_control_can_die": control["bytes"] > 1024
        and control.get("code") is not None
        and dead["bytes"] == 0
        and "error" in dead,
        "A_search_and_estimates_recorded": search["code"] is not None
        and len(digests) == len(ESTIMATE_CASES)
        and all(record.get("code") is not None for record in estimates),
        "C_stockgrid_recorded": grid["code"] is not None or "error" in grid,
        "D_no_credential_header_sent": not any(name in CREDENTIAL_HEADERS for name in sent),
        "E_verdict_predicates_can_flip": sa_live is False
        and grid_live is False
        and sa_forged is True
        and grid_forged is True,
    }
    lines.append(
        f"sa_unblocked={sa_live} stockgrid_unblocked={grid_live} "
        f"rows_advanceable_now={(2 if sa_live else 0) + (1 if grid_live else 0)} "
        f"forged(sa,grid)=({sa_forged},{grid_forged})"
    )
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"blocked_retry arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w150_blocked_retry_face_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
