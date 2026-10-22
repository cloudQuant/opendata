"""Wave-58 stockgrid blocker face: is the endpoint gap an information gap? No, as measured.

The owner decision list says stockgrid's old endpoint 301-redirects to `axlfi.com` and "the landing
page is not a data source". This face tests the stronger question -- does the route the **reference
implementation** uses still serve data -- and answers it on the recorded route rather than on prose.

Sources, all recorded rather than invented:

* the route comes from the local reference checkout,
  `openbb_platform/providers/stockgrid/openbb_stockgrid/models/short_volume.py:62`;
* the live-JSON control route is imported from this repo's own product spec
  (`opendata/data/providers/wsj/specs.py`: `WSJ_BASE_URL` + `WSJ_MOVERS_PATH`), not typed here;
* nothing sends a credential: every request is a public unauthenticated GET with a browser
  User-Agent, and no body value is printed -- only statuses, byte counts, digests, key names.

Arms (each one can differ from its expected value, and every "dead" reading is paired with a
"live" reading taken by the same detector):

1. `reference_route_not_json`  -- the recorded stockgrid route answers non-JSON.
2. `redirect_leaves_the_source_host` -- its final URL is no longer a stockgrid.io host.
3. `successor_paths_are_one_page` -- three distinct successor paths return byte-identical bodies,
   i.e. a catch-all page rather than three pages.
4. `hard_404_control` -- the same site does answer a real 404 for `/api`, so arm 3's 200s mean
   "catch-all" and not "the probe cannot reach the site".
5. `live_json_control` -- the repo's recorded public JSON route still yields parseable JSON with
   rows, which is the arm that proves arms 1 and 3 are findings about the source, not defects here.
6. `short_volume_has_one_reference_provider` -- exactly one reference provider implements the
   ShortVolume fetcher, plus a control census over another standard model that must return > 1 so
   the count is not an artifact of a broken matcher.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
REF_ROOT = Path("/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers")
RUNS = REPO / "docs/evidence/C147/wave41-objective-scoreboard-20261020/runs"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
#: Recorded in the reference implementation at models/short_volume.py:62 (one route, one param).
REF_ROUTE = "https://www.stockgrid.io/get_dark_pool_individual_data?ticker=AAPL"
SUCCESSOR_BASE = "https://axlfi.com"
SUCCESSOR_PATHS = ("/stockgrid", "/dark-pool", "/data")
#: The site does answer a hard 404 here, which is what makes the catch-all reading meaningful.
SUCCESSOR_HARD_404 = "/api"
SOURCE_HOST_MARKER = "stockgrid.io"  # host substring, not a credential


def _fetch(url: str) -> tuple[int, str, str, bytes]:
    """Public unauthenticated GET; returns (status, final url, content type, body)."""
    req = urllib.request.Request(  # nosec B310  # noqa: S310  # url comes from a recorded carrier
        url,
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json, text/html;q=0.9"},
    )
    with urllib.request.urlopen(  # nosec B310  # noqa: S310
        req, timeout=30, context=ssl.create_default_context()
    ) as resp:
        return resp.status, resp.geturl(), str(resp.headers.get("content-type") or ""), resp.read()


def _classify(body: bytes) -> tuple[bool, str]:
    """Say whether the body parses as a JSON object/array, and name the top keys when it does."""
    text = body.decode("utf-8", "replace")
    if text.lstrip()[:1] not in ("{", "["):
        return False, ""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return False, ""
    if isinstance(data, dict):
        return True, ",".join(sorted(data)[:6])
    if isinstance(data, list):
        return True, f"list_n={len(data)}"
    return True, "scalar"


def _probe(label: str, url: str) -> dict[str, Any]:
    """Fetch one URL and record the shape-only reading; failures are data, never raised."""
    try:
        status, final, ctype, body = _fetch(url)
        is_json, keys = _classify(body)
        reading: dict[str, Any] = {
            "label": label,
            "url": url,
            "status": status,
            "final": final,
            "ctype": ctype.split(";")[0],
            "bytes": len(body),
            "sha8": hashlib.sha256(body).hexdigest()[:8],
            "is_json": is_json,
            "top_keys": keys,
            "error": "",
        }
    except urllib.error.HTTPError as exc:
        reading = {
            "label": label,
            "url": url,
            "status": exc.code,
            "final": url,
            "ctype": str(exc.headers.get("content-type") or "").split(";")[0],
            "bytes": 0,
            "sha8": "",
            "is_json": False,
            "top_keys": "",
            "error": f"HTTPError:{exc.code}",
        }
    except Exception as exc:  # a probe reports the transport, it does not launder it
        reading = {
            "label": label,
            "url": url,
            "status": -1,
            "final": url,
            "ctype": "",
            "bytes": 0,
            "sha8": "",
            "is_json": False,
            "top_keys": "",
            "error": f"{type(exc).__name__}:{exc}",
        }
    print(
        f"probe {label} status={reading['status']} ctype={reading['ctype'] or '-'} "
        f"bytes={reading['bytes']} sha8={reading['sha8'] or '-'} is_json={reading['is_json']} "
        f"error={reading['error'] or '-'} final_host={reading['final'].split('/')[2]}",
        flush=True,
    )
    return reading


def _live_control_url() -> str:
    """Build the control URL from this repo's own product spec constants."""
    sys.path.insert(0, str(REPO))
    from opendata.data.providers.wsj import specs  # needs REPO on sys.path

    spec = specs.ETF_ACTIVE  # the recorded mover table, query included
    query = urlencode(tuple(spec.static_query))
    return f"{spec.base_url}{spec.path}?{query}"


def _reference_census(fetcher_token: str) -> tuple[int, list[str]]:
    """Count reference providers declaring a fetcher for one standard-model family."""
    hits: list[str] = []
    for module in sorted(REF_ROOT.glob("*/*/models/*.py")):
        text = module.read_text(encoding="utf-8", errors="replace")
        if re.search(rf"\b\w*{fetcher_token}\b", text):
            hits.append(module.parts[-3])
    return len(hits), sorted(set(hits))


def main() -> int:
    """Run the six arms and print the verdict face."""
    arms: list[tuple[str, bool, str]] = []
    ref = _probe("reference_route", REF_ROUTE)
    pages = [_probe(f"successor_{p.strip('/')}", f"{SUCCESSOR_BASE}{p}") for p in SUCCESSOR_PATHS]
    hard = _probe("successor_hard_404", f"{SUCCESSOR_BASE}{SUCCESSOR_HARD_404}")
    live = _probe("live_json_control", _live_control_url())

    arms.append(
        (
            "reference_route_not_json",
            ref["is_json"] is False and ref["status"] == 200,
            f"is_json={ref['is_json']} status={ref['status']} ctype={ref['ctype']}",
        )
    )
    final_host = ref["final"].split("/")[2] if ref["final"] else ""
    arms.append(
        (
            "redirect_leaves_the_source_host",
            SOURCE_HOST_MARKER not in final_host,
            f"final_host={final_host} source_marker={SOURCE_HOST_MARKER}",
        )
    )
    digests = {p["sha8"] for p in pages}
    arms.append(
        (
            "successor_paths_are_one_page",
            len(pages) == 3 and len(digests) == 1,
            f"distinct_digests={len(digests)} of {len(pages)} pages digests={sorted(digests)}",
        )
    )
    arms.append(
        (
            "hard_404_control",
            hard["status"] == 404,
            f"status={hard['status']} error={hard['error'] or '-'}",
        )
    )
    arms.append(
        (
            "live_json_control",
            live["is_json"] is True and live["status"] == 200,
            f"is_json={live['is_json']} status={live['status']} top_keys={live['top_keys']}",
        )
    )
    sv_count, sv_providers = _reference_census("ShortVolumeFetcher")
    ctrl_count, _ = _reference_census("PriceTargetConsensusFetcher")
    arms.append(
        (
            "short_volume_has_one_reference_provider",
            sv_count == 1 and ctrl_count > 1,
            f"short_volume_providers={sv_providers} control_count={ctrl_count}",
        )
    )
    passed = sum(1 for _, ok, _ in arms if ok)
    failed = [name for name, ok, _ in arms if not ok]
    for name, ok, detail in arms:
        print(f"arm {name} ok={ok} {detail}", flush=True)
    print(
        f"stockgrid_reference_route_face arms={len(arms)} arms_passed={passed} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}",
        flush=True,
    )
    rc = 0 if not failed else 1
    print(f"w156_stockgrid_reference_route_face_exit={rc}", flush=True)
    return rc


if __name__ == "__main__":
    OUT = RUNS / "w156_stockgrid_reference_route_face.txt"
    log: list[str] = []
    console = sys.stdout  # captured before the swap, so it is not Optional

    class _Tee:
        def write(self, text: str) -> int:  # stdout passthrough
            log.append(text)
            console.write(text)
            return len(text)

        def flush(self) -> None:
            console.flush()

    sys.stdout = _Tee()
    try:
        code = main()
    finally:
        sys.stdout = sys.__stdout__
    OUT.write_text("".join(log), encoding="utf-8")
    print(f"carrier={OUT} rc={code}")
    sys.exit(code)
