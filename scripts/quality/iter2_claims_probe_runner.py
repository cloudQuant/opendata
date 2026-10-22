"""Run one offline evidence-phase witness for a provider model and verdict it.

This is the subprocess the claims producer actually executes per phase; the
exit code it returns is the exit code recorded in the claim, and nothing here
consults a network, a database, or an ``OPENDATA_ALLOW_LIVE_E2E`` leg. All
fetch legs run through an offline witness seam and none of them can open a socket:
engine-driven fetchers go through ``http_transport`` / ``FixedResponseTransport``
from ``opendata.data.providers._engine.testing``, while fetchers that call the
governed client directly go through the entry's declared ``client_seam`` -- and
``_block_real_sockets()`` refuses any socket use in this process, so a mis-declared
seam fails loudly instead of silently fetching.

Verdict protocol on stdout (parsed by the producer):
    VERDICT=PASS | VERDICT=FAIL reason=<text> | VERDICT=BLOCKED reason=<text>
    COUNTERFACT_JSON={"expected": {...}, "observed": {...}, "reason": "..."}

Exit codes: 0 = PASS, 1 = FAIL, 3 = BLOCKED.

Usage:
    python scripts/quality/iter2_claims_probe_runner.py \
        --provider benzinga --model PriceTarget --phase direct \
        --evidence-root docs/evidence/C101

The ``counterfact`` phase runs its two legs apart: the untampered control in this process (it is
the first fetch here, so nothing can have cached an answer for it yet) and the tampered one in a
fresh child of the same interpreter, re-entered through ``--tampered-leg-child`` with the tampered
document on stdin. That isolation is what makes the counterfact a counterfact: a third-party SDK
that caches by URL in its own process can otherwise hand the tampered leg the *control's* answer
without ever asking the fake, so the tamper is never read and the refusal being reported was not
caused by it. Measured on yfinance 0.2.66 (``YfData`` is a process singleton, ``YfData.cache_get``
is ``@lru_cache(maxsize=64)`` keyed on ``(self, url, params, timeout)`` and a statement leg's url
is fixed by symbol/type/period1 plus a day-ceiled period2): control 2 seam calls / 12 rows,
tampered leg in the same process 0 seam calls / the control's 12 rows, ``cache_get.cache_info() ==
CacheInfo(hits=1, misses=1)``. Clearing that lru by hand is not the fix -- enumerating every cache
a provider's SDK owns is not decidable -- so the leg is isolated instead, and its own request count
through the fake is asserted (see :func:`_tampered_leg_report`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import importlib
import json
import os
import socket
import subprocess  # nosec B404  # runs the five witness phases as child processes
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import iter2_claims_fixtures as witnesses  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.protocol import FetchContext  # noqa: E402
from opendata.data.providers._engine.http_json import (  # noqa: E402
    HttpResponse,
    ProviderEngineError,
)
from opendata.data.providers._engine.testing import FixedResponseTransport  # noqa: E402
from opendata.data.providers.catalog import fetchers_for, register_provider  # noqa: E402
from opendata.data.registry import ProviderRegistry  # noqa: E402
from opendata.data.request_budget import (  # noqa: E402
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query  # noqa: E402

PHASES = ("implementation", "direct", "service", "contract", "counterfact")

#: The leg name the counterfact phase gives its tampered run. Every leg that asks the offline
#: carrier an answer records its requests under the same reading; this one additionally has to show
#: that the answer it got was *its own*, which is the reading a shared-process leg cannot give.
TAMPERED_LEG: Final = "tampered"

#: Re-entry flag: this process runs nothing but the counterfact phase's tampered leg and prints one
#: ``TAMPERED_LEG_JSON=<report>`` line instead of a verdict. Spawned by :func:`_spawn_tampered_leg`.
TAMPERED_LEG_CHILD_FLAG: Final = "--tampered-leg-child"

#: The one stdout line the parent of an isolated tampered leg reads back.
TAMPERED_LEG_MARKER: Final = "TAMPERED_LEG_JSON="

#: A child that never answers is a harness fault, not a pass: bound it and refuse loudly.
_TAMPERED_LEG_CHILD_TIMEOUT: Final = 300.0


class WitnessFailure(AssertionError):  # noqa: N818  # verdict vocabulary, not a retry error
    """The witness ran and the checked property did not hold."""


class WitnessBlocked(Exception):  # noqa: N818  # verdict vocabulary, not a retry error
    """A precondition is missing, so the witness could not honestly run."""


#: The grant a witness that declares no carrier gets, exactly as it has always been.
_DEFAULT_SENDS: Final = 3


def _carrier_sends(entry: dict, document: Any) -> int:  # noqa: ANN401  # the fixture document is arbitrary JSON
    """Budget the sends the offline carrier can actually answer -- never a free-hand number.

    A fetcher that preflights its own fan-out against ``authorize_request`` asks the process how
    many sends its grant covers before it sends anything (``models/sloos.py:61``), so a six-leg
    selection under a three-send grant refuses with ``FRED_QUERY_LIMIT`` before any fixture is
    read. The carrier already states how many sends it can serve: one body per ``url_contains``
    entry, or one per page of its ``sequence``. The witness borrows that count instead of declaring
    a budget, so an entry cannot grant itself more sends than it could answer, and an entry that
    declares no seam keeps the shipped three.
    """
    if "client_seam" not in entry or not isinstance(document, dict):
        return _DEFAULT_SENDS
    responses = document.get("responses")
    if not isinstance(responses, list):
        return _DEFAULT_SENDS
    answerable = 0
    for spec in responses:
        if not isinstance(spec, dict):
            continue
        sequence = spec.get("sequence")
        answerable += len(sequence) if isinstance(sequence, list) and sequence else 1
    return max(_DEFAULT_SENDS, answerable)


def _grant_context(
    provider: str, model: str, hosts: list[str], *, sends: int = _DEFAULT_SENDS
) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=provider,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=sends,
        source_attempts=sends,
        allowed_hosts=set(hosts),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=sends,
        source_attempts=sends,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=10.0, request_budget=budget, operation=RequestOperation.QUERY)


def _resolve_fetcher_type(provider: str, model: str) -> Any:  # noqa: ANN401  # heterogeneous fetcher classes
    for fetcher in fetchers_for(provider):
        if getattr(fetcher, "canonical_model", None) == model:
            return type(fetcher)
    raise WitnessBlocked(f"provider {provider!r} binds no fetcher for model {model!r}")


def _fixture_document(evidence_root: Path, provider: str, model: str) -> Any:  # noqa: ANN401  # the fixture document is arbitrary JSON
    path = evidence_root / "fixtures" / provider / f"{model}.json"
    if not path.is_file():
        raise WitnessBlocked(f"fixture file {path} was not materialized by the producer")
    return json.loads(path.read_text(encoding="utf-8"))


class RealSocketAttempt(RuntimeError):  # noqa: N818  # verdict vocabulary, not a retry error
    """A leg tried to reach a socket inside a witness that declared an offline-only seam."""


def _block_real_sockets() -> None:
    """Make any real outbound socket attempt in this process raise, for every phase.

    The engine seam already cannot reach a socket, so this is not a workaround for it; it is the
    enforceable half of the claim for providers witnessed through ``client_seam``. Without it a
    mis-declared seam would be indistinguishable from a silent real fetch.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RealSocketAttempt("offline witness: real socket use is refused by the runner")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def _seam_body_bytes(body: Any) -> tuple[bytes, str]:  # noqa: ANN401  # fixture JSON body
    """Encode one fixture response body the way the source itself would publish it."""
    if isinstance(body, bytes):
        return body, "application/octet-stream"
    if isinstance(body, str):
        return body.encode("utf-8"), "text/plain"
    if isinstance(body, list) and body and all(isinstance(line, str) for line in body):
        return "\n".join(body).encode("utf-8"), "text/html"
    return json.dumps(body).encode("utf-8"), "application/json"


def _spec_body(spec: dict[str, Any]) -> tuple[bytes, str]:
    """Resolve one fixture entry's payload, including base64 carriers for non-text documents."""
    encoded = spec.get("body_b64")
    if encoded is not None:
        import base64

        if not isinstance(encoded, str):
            raise WitnessFailure("body_b64 must be a base64 string")
        try:
            return base64.b64decode(encoded, validate=True), "application/octet-stream"
        except Exception:
            raise WitnessFailure("body_b64 is not valid base64") from None
    return _seam_body_bytes(spec.get("body"))


def _echo_request_id(body: bytes, json_body: Any, *, needle: str) -> bytes:  # noqa: ANN401
    """Rewrite one envelope's ``id`` with the id the client actually asked with.

    A JSON-RPC server answers the request it was given, and a provider that parses the reply checks
    the echo (``deribit/client.py`` refuses unless ``response_id == request_id``), where the id came
    from a process-global counter. A fixture carrying a fixed id can therefore serve exactly *one*
    fetch generation per process, and the counterfact phase runs two in a row -- its untampered
    control consumes the first ids, so the tampered run asks an id the fixture cannot echo and
    refuses at the envelope, never reaching the cell the tamper broke. Substituting the echo is the
    faithful reading, and it is opt-in per fixture entry so every spec on disk keeps the bytes it
    was received under. Only that one key moves: every other byte the fixture declares is served as
    the module's own encoder produced it, and a payload with no integer id means the entry declared
    an echo the call cannot honour.
    """
    if not isinstance(json_body, dict):
        raise WitnessFailure(
            f"client seam fixture entry url_contains={needle!r} declares echo_request_id "
            "but the call carried no JSON payload to echo"
        )
    asked = json_body.get("id")
    if type(asked) is not int:
        raise WitnessFailure(
            f"client seam fixture entry url_contains={needle!r} declares echo_request_id "
            f"but the payload carries id={asked!r}, which is not an integer"
        )
    try:
        document = json.loads(body)
    except ValueError:
        raise WitnessFailure(
            f"echo_request_id is declared for a body that is not JSON at all "
            f"(url_contains={needle!r})"
        ) from None
    if not isinstance(document, dict) or document.get("jsonrpc") != "2.0":
        raise WitnessFailure(
            f"echo_request_id is declared for a body that is not a JSON-RPC 2.0 envelope "
            f"(url_contains={needle!r}, body={type(document).__name__})"
        )
    document["id"] = asked
    return json.dumps(document).encode("utf-8")


class _FakeSharedClient:
    """Serves the fixture envelope through the governed client's own ``get``/``request`` surface.

    ``GovernedHttpClient`` publishes exactly two public verbs (measured by introspection: ``get``
    and ``request``), so a fake that answers only ``get`` silently blocks every provider whose
    fetcher posts. A fixture spec may declare ``method`` (a string or a list of strings) and the
    fake then refuses to serve that spec under another verb, so widening the surface does not turn a
    POST-shaped fixture into a free GET. Specs that declare nothing keep the old behaviour, which is
    what every receipt on disk was produced under.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.methods: list[str] = []
        self._cursors: dict[int, int] = {}

    @staticmethod
    def _method_matches(spec: dict[str, Any], method: str) -> bool:
        declared = spec.get("method")
        if declared is None:
            return True
        wanted = [declared] if isinstance(declared, str) else list(declared)
        return any(str(name).upper() == method.upper() for name in wanted)

    def get(
        self,
        url: str,
        *,
        params: Any = None,  # noqa: ANN401  # governed client passes a mapping through
        headers: Any = None,  # noqa: ANN401
        timeout: Any = None,  # noqa: ANN401
        source: str = "unspecified",
        **_extra: Any,  # noqa: ANN401  # cache_context / use_cache stay unread offline
    ) -> Any:  # noqa: ANN401  # returns a requests.Response
        """Return the first fixture response whose ``url_contains`` matches, or fail loudly."""
        return self._respond("GET", url)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,  # noqa: ANN401
        headers: Any = None,  # noqa: ANN401
        timeout: Any = None,  # noqa: ANN401
        source: str = "unspecified",
        cache_context: Any = None,  # noqa: ANN401
        json_body: Any = None,  # noqa: ANN401
        use_cache: bool = True,
        **_extra: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401  # returns a requests.Response
        """Mirror ``GovernedHttpClient.request``, the verb several providers actually call."""
        return self._respond(str(method), url, json_body=json_body)

    def _respond(self, method: str, url: str, *, json_body: Any = None) -> Any:  # noqa: ANN401
        """Serve one fixture envelope entry, recording the call the way ``get`` always did."""
        self.calls.append(url)
        self.methods.append(method.upper())
        mismatched: list[Any] = []
        for index, spec in enumerate(self.responses):
            needle = spec.get("url_contains")
            if not (isinstance(needle, str) and needle in url):
                continue
            if not self._method_matches(spec, method):
                mismatched.append(spec.get("method"))
                continue
            sequence = spec.get("sequence")
            if isinstance(sequence, list) and sequence:
                cursor = self._cursors.get(index, 0)
                if cursor >= len(sequence):
                    raise WitnessFailure(
                        f"seam sequence for url_contains={needle!r} exhausted after "
                        f"{len(sequence)} page(s); the source asked for more than the fixture "
                        "declares"
                    )
                self._cursors[index] = cursor + 1
                body, default_type = _seam_body_bytes(sequence[cursor])
            else:
                body, default_type = _spec_body(spec)
            if spec.get("echo_request_id"):
                body = _echo_request_id(body, json_body, needle=str(needle))
                default_type = "application/json"
            import requests

            response = requests.Response()
            response.status_code = int(spec.get("status", 200))
            response._content = body  # the documented requests fill-in point
            response.url = url
            response.reason = "OK"
            response.headers.update({"Content-Type": spec.get("content_type", default_type)})
            return response
        if mismatched:
            raise WitnessFailure(
                f"client seam fixture entries matched url={url!r} but declare "
                f"method={mismatched!r}, and the source asked {method.upper()}"
            )
        raise WitnessFailure(f"client seam fixture covers no response for url={url!r}")


#: Seam kind for a call site that consumes the patched attribute as ``with attr(x) as session:``
#: (yfinance's ``_sdk.governed_yfinance_session(sdk)``, measured at ``_sdk.py:78``). The landed
#: ``shared_http_client`` kind patches a 0-positional factory whose result is used as ``.get``; a
#: witness may not reuse it for a one-positional context-manager site -- the old lambda swallows
#: the positional and hands the product a bare client, which is the silent passthrough that
#: ``docs/evidence/C128/.../yfinance_shard_entry_price.py`` arm F prices. This kind substitutes a
#: fake that (i) records every invocation of the patched attribute together with the SDK object it
#: received and (ii) is itself the session the SDK talks to, so both enforceability arms below --
#: "attr never invoked" and "fake never asked" -- are read off what the fetch actually did.
SDK_SESSION_SEAM_KIND: Final = "governed_sdk_session"


def _build_sdk_session_fake(responses: list[dict[str, Any]]) -> Any:  # noqa: ANN401  # dynamic curl subclass
    """Build the fake governed-yfinance-session for ``SDK_SESSION_SEAM_KIND``.

    ``yfinance.data.YfData._set_session`` refuses any object that is not a ``curl_cffi`` session,
    so the fake is a real ``curl_cffi.requests.Session`` subclass with the send verbs replaced --
    the SDK's own request construction, crumb/cookie orchestration and JSON parsing all run on it,
    and only the wire is fake. Responses are matched against the *effective* URL (the request URL
    plus its encoded ``params``), because a yfinance chart leg is discriminated by parameters,
    not by path.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # the optional SDK generation is itself governed offline
        raise WitnessBlocked(
            f"governed_sdk_session seam needs curl_cffi for the session type: {exc}"
        ) from None

    class _FakeSdkSession(curl_requests.Session, _FakeSharedClient):  # type: ignore[misc]
        def __init__(self) -> None:
            curl_requests.Session.__init__(self, impersonate="chrome", trust_env=False)
            _FakeSharedClient.__init__(self, responses)
            self.invoked: list[Any] = []  # the SDK object handed to the seam

        def __enter__(self) -> Any:  # noqa: ANN401  # ``with attr(sdk) as session`` bind point
            return self

        def __exit__(self, *_exc: object) -> Literal[False]:
            return False

        def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> Any:  # noqa: ANN401
            params = kwargs.get("params")
            effective = url
            if isinstance(params, dict) and params:
                from urllib.parse import urlencode

                effective = f"{url}?{urlencode(sorted(params.items()), doseq=True)}"
            return self._respond(method, effective, json_body=kwargs.get("json"))

        def get(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401  # curl-shaped **kwargs
            return self._answer("GET", str(url), kwargs)

        def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
            return self._answer("POST", str(url), kwargs)

    return _FakeSdkSession()


@contextlib.contextmanager
def _client_seam(entry: dict, document: Any) -> Iterator[_FakeSharedClient]:  # noqa: ANN401
    """Patch the named provider modules to the fake client, then restore them.

    The declaration is what makes this auditable: a witness module must name the modules whose
    ``get_shared_http_client`` it replaces, so a provider that never reads the patched attribute
    cannot report a clean run.
    """
    seam = entry["client_seam"]
    responses = document.get("responses") if isinstance(document, dict) else None
    if not isinstance(responses, list) or not responses:
        raise WitnessBlocked(
            "client seam fixture must be an envelope {'responses': [...]} with at least one entry"
        )
    kind = seam.get("kind", "shared_http_client")
    if kind == SDK_SESSION_SEAM_KIND:
        client: Any = _build_sdk_session_fake(  # _FakeSdkSession or _FakeSharedClient
            [spec for spec in responses if isinstance(spec, dict)]
        )
    elif kind == "shared_http_client":
        client = _FakeSharedClient([spec for spec in responses if isinstance(spec, dict)])
    else:
        raise WitnessBlocked(f"client seam kind {kind!r} is not served by this runner")
    attr = seam["attr"]
    restored: list[tuple[Any, str, Any]] = []  # module objects
    for name in seam["modules"]:
        module = importlib.import_module(name)
        if not hasattr(module, attr):
            raise WitnessBlocked(f"client seam module {name!r} publishes no {attr!r} to patch")
        restored.append((module, attr, getattr(module, attr)))
        if kind == SDK_SESSION_SEAM_KIND:
            # The product site is ``with attr(sdk) as session:`` -- exactly one positional. A
            # second positional or a keyword would mean the witness misdeclared the call shape.
            def substitute(_sdk_arg: Any, _c: Any = client) -> Any:  # noqa: ANN401
                if len(_c.invoked) and _c.invoked[-1] is not _sdk_arg:
                    raise WitnessFailure(
                        "the governed session seam was re-invoked with a different SDK object; "
                        "the witness fake does not survive that call shape"
                    )
                _c.invoked.append(_sdk_arg)
                return _c

            setattr(module, attr, substitute)
        else:
            setattr(module, attr, lambda _c=client: _c)
    try:
        yield client
    finally:
        for module, attr_name, original in restored:
            setattr(module, attr_name, original)


def _no_request_failure(leg: str, surface: str) -> WitnessFailure:
    """The refusal for a leg that answered without asking the offline carrier anything.

    For an ordinary leg an empty request log means the seam was mis-declared. For the tampered leg
    it means something worse: the leg completed on an answer recorded before it -- a cache the
    harness cannot enumerate -- so the tampered bytes were never read and whatever the leg did next
    was not caused by the tamper. Both readings refuse; only the second one names the masking.
    """
    if leg != TAMPERED_LEG:
        return WitnessFailure(f"the fetch completed without ever calling the declared {surface}")
    return WitnessFailure(
        f"the tampered leg completed without ever calling the declared {surface}, so an answer "
        "recorded before it served that leg and the tampered document was never read"
    )


def _seam_guard(client: _FakeSharedClient, *, leg: str = "direct") -> None:
    """A fetch that never asked the fake anything is not an offline witness."""
    if not client.calls:
        raise _no_request_failure(leg, "offline client seam")
    # The second enforceability arm, for the seam kinds that can measure it: a context-manager
    # fake also refuses when the patched attribute was never invoked at the product call site.
    invoked = getattr(client, "invoked", None)
    if invoked is not None and not invoked:
        if leg == TAMPERED_LEG:
            raise WitnessFailure(
                "the tampered leg never invoked the patched governed session attribute, so an "
                "answer recorded before it served that leg and the tampered document was never read"
            )
        raise WitnessFailure(
            "the fetch completed without ever invoking the patched governed session attribute"
        )


def _run_direct(
    provider: str,
    model: str,
    entry: dict,
    document: Any,  # noqa: ANN401  # fixture JSON
    *,
    leg: str = "direct",
    handle: list[Any] | None = None,  # the carrier, handed back even when the leg refuses
) -> tuple[list, Any, Any]:  # rows and transport are dynamic by nature
    """Fetch through the offline seam on a fresh fetcher instance; return rows."""
    fetcher_type = _resolve_fetcher_type(provider, model)
    transport = FixedResponseTransport(HttpResponse(200, copy.deepcopy(document)))
    if handle is not None:
        handle.append(transport)
    probe_class = type(
        f"Probed{fetcher_type.__name__}", (fetcher_type,), {"http_transport": transport}
    )
    instance = probe_class()
    ctx = _grant_context(provider, model, entry["hosts"], sends=_carrier_sends(entry, document))
    rows = list(instance.fetch(ctx=ctx, **entry["query"]))
    if not transport.calls:
        raise _no_request_failure(leg, "offline transport seam")
    if not rows:
        raise WitnessFailure("the offline fetch produced zero rows")
    for row in rows:
        if type(row) is not fetcher_type.row_model:
            raise WitnessFailure(f"row {type(row).__name__} is not the declared contract row class")
    return rows, transport, fetcher_type


def _run_direct_with_seam(
    provider: str,
    model: str,
    entry: dict,
    document: Any,  # noqa: ANN401  # fixture JSON
    *,
    leg: str = "direct",
    handle: list[Any] | None = None,  # the carrier, handed back even when the leg refuses
) -> tuple[list, Any, Any]:  # rows and transport are dynamic by nature
    """Run :func:`_run_direct`, or the declared client seam instead of the engine one.

    Providers whose fetchers call ``get_shared_http_client()`` directly have no ``http_transport``
    to honour; ``transport.calls`` would then be empty and the engine path would fail -- which is
    exactly the refusal that kept those rows unwitnessed. The seam substitutes the governed client
    for the modules the witness names, so the parsing, normalization and refusal code that ships
    is still the code under test.

    ``handle`` exists because a leg that *refuses* still has to be measured: the caller cannot read
    the carrier's request log off a return value that never came back, and the tampered leg's
    request count is exactly the reading that catches an SDK cache replaying another leg's answer.
    """
    if "client_seam" not in entry:
        return _run_direct(provider, model, entry, document, leg=leg, handle=handle)
    fetcher_type = _resolve_fetcher_type(provider, model)
    instance = fetcher_type()
    ctx = _grant_context(provider, model, entry["hosts"], sends=_carrier_sends(entry, document))
    with _client_seam(entry, document) as client:
        if handle is not None:
            handle.append(client)
        rows = list(instance.fetch(ctx=ctx, **entry["query"]))
        _seam_guard(client, leg=leg)
    if not rows:
        raise WitnessFailure("the offline fetch produced zero rows")
    for row in rows:
        if type(row) is not fetcher_type.row_model:
            raise WitnessFailure(f"row {type(row).__name__} is not the declared contract row class")
    return rows, client, fetcher_type


def _apply_tamper(
    document: Any,  # noqa: ANN401  # deep-copies arbitrary fixture JSON
    tamper: dict,
) -> Any:  # noqa: ANN401  # the tampered fixture stays dynamic
    tampered = copy.deepcopy(document)
    cursor = tampered
    for step in tamper["path"][:-1]:
        cursor = cursor[step]
    leaf = tamper["path"][-1]
    if tamper["op"] == "drop":
        if isinstance(cursor, list):
            del cursor[leaf]
        else:
            if leaf not in cursor:
                raise WitnessFailure(f"tamper target {leaf!r} is not published by the fixture")
            del cursor[leaf]
    elif tamper["op"] == "set":
        cursor[leaf] = tamper["value"]
    else:  # pragma: no cover - configuration bug
        raise WitnessFailure(f"unknown tamper op {tamper['op']!r}")
    return tampered


def _typed_provider_refusal(exc: BaseException, provider: str) -> str | None:
    """Return the refusal code when ``exc`` is this provider's own typed, code-bearing error.

    The engine path refuses with ``ProviderEngineError`` and carries a full field attribution.
    A provider that owns its transport refuses with its own error class, which has to earn the
    same standing: declared by the provider package (a shared ``HttpFetchError`` or a bare
    ``ValueError`` is not a provider's refusal contract), carrying a non-empty string ``code``
    and a ``status`` face. Anything else is not a refusal shape the witness may declare.
    """
    refusal_type = type(exc)
    if not refusal_type.__module__.startswith(f"opendata.data.providers.{provider}."):
        return None
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        return None
    if not hasattr(exc, "status"):
        return None
    return code


def _refusal_shape(
    exc: BaseException,
    provider: str = "",
    expected_code: str = "",
) -> dict[str, Any]:
    """Assert the typed refusal shape and return its machine-readable face."""
    if isinstance(exc, ProviderEngineError):
        for attribute in ("code", "status", "url", "rejected", "missing", "invalid"):
            if not hasattr(exc, attribute):
                raise WitnessFailure(f"refusal lost the typed attribute {attribute!r}")
        return {
            "refusal_class": type(exc).__name__,
            "refusal_code": exc.code,
            "status": exc.status,
            "rejected": list(exc.rejected),
            "missing": list(exc.missing),
            "invalid": list(exc.invalid),
        }
    code = _typed_provider_refusal(exc, provider) if provider else None
    if code is None:
        raise WitnessFailure(
            f"refusal {type(exc).__name__} is not a ProviderEngineError nor a typed, "
            "code-bearing error declared by this provider package"
        )
    if expected_code and code != expected_code:
        raise WitnessFailure(
            f"the shipped refusal raises code {code!r}, not the declared {expected_code!r}"
        )
    return {
        "refusal_class": type(exc).__name__,
        "refusal_code": code,
        "status": getattr(exc, "status", None),
        "rejected": [],
        "missing": [],
        "invalid": [],
    }


def witness_implementation(provider: str, model: str, entry: dict) -> None:
    """Assert the registration truth: descriptor domain, capability, contract row, stages."""
    registry = ProviderRegistry()
    register_provider(provider, registry)
    descriptor = _find_descriptor(registry, provider, model)
    fetcher = registry.resolve_model(provider, model)
    capability = fetcher.capability
    if getattr(fetcher, "canonical_model", None) != model:
        raise WitnessFailure("resolved fetcher's canonical_model disagrees with the inventory row")
    if capability.source != provider:
        raise WitnessFailure("resolved fetcher's capability source disagrees with the row")
    if descriptor.domain != capability.domain:
        raise WitnessFailure("registered descriptor domain disagrees with the fetcher capability")
    row_model = getattr(fetcher, "row_model", None)
    if not (isinstance(row_model, type) and issubclass(row_model, ContractModel)):
        raise WitnessFailure("the bound fetcher publishes no typed contract row model")
    if not hasattr(fetcher, "transform_query") or not hasattr(fetcher, "extract_data"):
        raise WitnessFailure("the bound fetcher lacks the query/extract pipeline stages")
    print(
        "implementation: registered domain="
        f"{descriptor.domain} row_model={row_model.__name__} "
        f"verified={capability.verified} transport=offline-only"
    )


def _find_descriptor(registry: ProviderRegistry, provider: str, model: str) -> Any:  # noqa: ANN401  # the descriptor type lives behind the service helper
    from opendata.services.provider_models import find_provider_model

    return find_provider_model(registry, provider, model)


def witness_direct(provider: str, model: str, entry: dict) -> None:
    """Fetch through the offline seam and require the declared contract rows came back."""
    rows, transport, fetcher_type = _run_direct_with_seam(provider, model, entry, entry["document"])
    print(
        "direct: fetcher="
        f"{fetcher_type.__name__} seam_requests={len(transport.calls)} "
        f"rows={len(rows)} row_class={fetcher_type.row_model.__name__} "
        f"first_row_sha256={_row_digest(rows[0])}"
    )


def _row_digest(row: ContractModel) -> str:
    import hashlib

    payload = json.dumps(row.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def witness_service(provider: str, model: str, entry: dict) -> None:
    """Run the real service query path and require serialized frozen-contract rows."""
    registry = ProviderRegistry()
    register_provider(provider, registry)
    fetcher = registry.resolve_model(provider, model)
    ctx = _grant_context(
        provider, model, entry["hosts"], sends=_carrier_sends(entry, entry["document"])
    )

    domain = fetcher.capability.domain

    class Principal:
        def allows_domain(self, candidate: str) -> bool:
            return candidate == domain

    def run_query() -> Any:  # noqa: ANN401  # the service returns a serialized row mapping
        return asyncio.run(
            provider_model_query.query_provider_model(
                registry=registry,
                source=provider,
                model=model,
                query=dict(entry["query"]),
                principal=Principal(),
                ctx=ctx,
            )
        )

    if "client_seam" in entry:
        with _client_seam(entry, entry["document"]) as seam:
            response = run_query()
            seam_requests = len(seam.calls)
    else:
        transport = FixedResponseTransport(HttpResponse(200, copy.deepcopy(entry["document"])))
        # in-memory seam only; no file is touched
        fetcher.http_transport = transport  # type: ignore[attr-defined]
        response = run_query()
        seam_requests = len(transport.calls)
    results = response.get("results")
    if response.get("source") != provider or response.get("model") != model:
        raise WitnessFailure("service response echoes a different provider/model identity")
    if response.get("domain") != domain:
        raise WitnessFailure("service response reports a different domain than the capability")
    if not isinstance(results, list) or not results:
        raise WitnessFailure("service response carries no serialized contract rows")
    row_model = getattr(fetcher, "row_model", None)
    if row_model is None:
        raise WitnessFailure("the resolved fetcher publishes no contract row model")
    field_names = set(row_model.model_fields)
    for row in results:
        if not isinstance(row, dict) or not set(row) <= field_names:
            raise WitnessFailure("service returned a row outside the frozen contract fields")
    if not seam_requests:
        raise WitnessFailure("the service leg completed without using the offline seam")
    print(
        "service: query_provider_model executed for real (registry + reviewed domain "
        f"semantics), rows={len(results)} first_row_sha256={_json_digest(results[0])}"
    )


def _json_digest(value: Any) -> str:  # noqa: ANN401  # any JSON-serializable row
    import hashlib

    payload = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def witness_contract(provider: str, model: str, entry: dict) -> None:
    """Revalidate each produced row against the frozen contract, strict and extra-forbid."""
    rows, _, fetcher_type = _run_direct_with_seam(provider, model, entry, entry["document"])
    row_model = fetcher_type.row_model
    if not row_model.__module__.startswith("opendata.data.models."):
        raise WitnessFailure(
            f"row class {row_model.__name__} is not a frozen model under opendata/data/models/"
        )
    for row in rows:
        if type(row) is not row_model:
            raise WitnessFailure("produced row is not the frozen contract class itself")
        dumped = row.model_dump(mode="python", by_alias=False)
        if set(dumped) - set(row_model.model_fields):
            raise WitnessFailure("row serialization carries fields the contract never declared")
        again = row_model.model_validate(dumped, strict=True)
        if again.model_dump(mode="python") != dumped:
            raise WitnessFailure("strict contract round-trip disagrees with the produced row")
        try:
            row_model.model_validate({**dumped, "zz_not_declared": "x"}, strict=True)
        except ValidationError:
            pass
        else:
            raise WitnessFailure("frozen contract accepted an undeclared extra field")
    print(
        f"contract: {row_model.__name__} validated {len(rows)} produced row(s); strict "
        "round-trip and extra-forbid boundaries hold"
    )


def _guard_against_copied_refusal(entry: dict, expected: dict) -> None:
    """Refuse a declared refusal detail that was copied from a run instead of derived.

    An engine shape refusal attributes the offending contract column, so a witness may declare
    that detail -- but only if it names the cell the tamper broke. A ``set`` tamper on a source
    key that differs from the column name declares ``column`` explicitly; a code-only refusal
    with empty detail stays allowed.
    """
    tamper = entry["tamper"]
    declared = tamper.get("column")
    if declared:
        leaf = str(declared)
    else:
        named = [str(step) for step in reversed(tamper["path"]) if isinstance(step, str)]
        leaf = named[0] if named else ""
    if not leaf:
        return
    for face in ("rejected", "missing", "invalid"):
        for item in expected[face]:
            if leaf not in str(item):
                raise WitnessBlocked(
                    f"expected {face} entry {item!r} does not name the tampered cell {leaf!r}; "
                    "declare the refusal from the contract, not from a captured run"
                )


def _carrier_requests(handle: list[Any]) -> int:  # transport or fake client
    """How many requests the leg actually put through its offline carrier."""
    if not handle:
        return 0
    return len(getattr(handle[0], "calls", ()))


def _run_tampered_leg(provider: str, model: str, entry: dict, tampered: Any) -> dict[str, Any]:  # noqa: ANN401  # tampered fixture JSON
    """Run one leg against the tampered document and report what the carrier saw.

    This is the whole of what an isolated tampered leg can tell its parent, and the parent verdicts
    on it: the outcome (refusal / rows / harness failure / blocked), the refusal face the shipped
    code produced, and -- the part no shared-process leg could prove -- the number of requests the
    tampered document itself got through the fake.
    """
    handle: list[Any] = []  # the carrier the leg answered through
    report: dict[str, Any]
    try:
        rows, _, _ = _run_direct_with_seam(
            provider, model, entry, tampered, leg=TAMPERED_LEG, handle=handle
        )
    except (WitnessFailure, WitnessBlocked) as exc:
        # A harness-level refusal is not a provider refusal: it must never be read as the
        # tamper biting, whichever side of the process boundary the leg ran on.
        report = {
            "outcome": "blocked" if isinstance(exc, WitnessBlocked) else "failure",
            "reason": str(exc),
        }
    except Exception as exc:  # the refusal under test is whatever the provider ships
        try:
            observed = _refusal_shape(exc, provider, entry["refusal_code"])
        except WitnessFailure as shape_failure:
            report = {"outcome": "failure", "reason": str(shape_failure)}
        else:
            report = {"outcome": "refusal", "observed": observed}
    else:
        report = {"outcome": "rows", "rows": len(rows)}
    report["seam_requests"] = _carrier_requests(handle)
    return report


def _tampered_leg_child_main(provider: str, model: str, entry: dict) -> int:
    """Serve the parent's isolated tampered leg: read the document on stdin, report on stdout."""
    tampered = json.loads(sys.stdin.read())
    try:
        report = _run_tampered_leg(provider, model, entry, tampered)
    except Exception as exc:  # a leg that cannot even be reported is still a loud refusal
        report = {"outcome": "crash", "reason": f"{type(exc).__name__}: {exc}", "seam_requests": 0}
    try:
        payload = json.dumps(report, sort_keys=True)
    except (TypeError, ValueError) as exc:
        # The face has to travel as the values the shipped code raised, so no lossy fallback:
        # a refusal face this runner cannot transport is reported instead of guessed at.
        payload = json.dumps(
            {
                "outcome": "failure",
                "reason": f"the tampered leg's refusal face is not transportable: {exc}",
                "seam_requests": report.get("seam_requests", 0),
            },
            sort_keys=True,
        )
    print(TAMPERED_LEG_MARKER + payload)
    return 0


def _spawn_tampered_leg(provider: str, model: str, entry: dict, tampered: Any) -> dict[str, Any]:  # noqa: ANN401  # tampered fixture JSON
    """Run the tampered leg in a process nothing has cached in, and read its report back.

    The child is this same file with :data:`TAMPERED_LEG_CHILD_FLAG`, under the same interpreter,
    the same credential environment ``main`` populated and the same socket block, so the seam, the
    fetcher and the refusal path it runs are the shipped ones -- only the accumulated process state
    differs, which is the point.
    """
    if "evidence_root_arg" not in entry:
        raise WitnessBlocked(
            "the counterfact phase isolates its tampered leg in a child, which needs the "
            "evidence root main() resolved; call the phase through main()"
        )
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--provider",
        provider,
        "--model",
        model,
        "--phase",
        "counterfact",
        "--evidence-root",
        str(entry["evidence_root_arg"]),
        TAMPERED_LEG_CHILD_FLAG,
    ]
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable and this file's own path
            argv,
            input=json.dumps(tampered),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            timeout=_TAMPERED_LEG_CHILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise WitnessFailure(
            f"the isolated tampered leg did not finish within {exc.timeout:.0f}s"
        ) from None
    for line in (result.stdout or "").splitlines():
        if line.startswith(TAMPERED_LEG_MARKER):
            report = json.loads(line[len(TAMPERED_LEG_MARKER) :])
            if not isinstance(report, dict) or "seam_requests" not in report:
                raise WitnessFailure(f"the isolated tampered leg reported {report!r}")
            return report
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = " | ".join(detail[-3:]) if detail else "no output"
    raise WitnessFailure(
        f"the isolated tampered leg reported nothing (exit={result.returncode}): {tail}"
    )


def witness_counterfact(provider: str, model: str, entry: dict) -> None:
    """Tamper the fixture and require the declared typed refusal, with an untampered control."""
    rows, _, _ = _run_direct_with_seam(provider, model, entry, entry["document"])
    control_note = f"untampered control produced {len(rows)} row(s) through the same seam"
    tampered = _apply_tamper(entry["document"], entry["tamper"])
    expected = {
        "refusal_class": entry["refusal_class"],
        "refusal_code": entry["refusal_code"],
        "status": entry.get("expected_status"),
        "rejected": list(entry.get("expected_rejected", [])),
        "missing": list(entry.get("expected_missing", [])),
        "invalid": list(entry.get("expected_invalid", [])),
    }
    _guard_against_copied_refusal(entry, expected)
    report = _spawn_tampered_leg(provider, model, entry, tampered)
    # The invariant, read off the tampered leg itself: it must put at least one request through the
    # carrier built from *its own* document. A leg that answered without asking anything was served
    # by an answer recorded before it, so the tamper never reached the product and any refusal after
    # it is not the tamper's. This is the counter assertion the shared-process phase could not make.
    seam_requests = int(report.get("seam_requests", 0))
    if seam_requests < 1:
        raise WitnessFailure(
            f"the tampered leg made {seam_requests} request(s) through the offline carrier, so the "
            f"control leg's answer served it and the tamper was never read "
            f"(leg outcome={report.get('outcome')!r} reason={report.get('reason', '')!r})"
        )
    outcome = report.get("outcome")
    if outcome == "blocked":
        raise WitnessBlocked(str(report.get("reason")))
    if outcome in ("failure", "crash"):
        raise WitnessFailure(str(report.get("reason")))
    if outcome == "rows":
        raise WitnessFailure(
            f"counterfact did not bite: the tampered fixture still produced {report['rows']} row(s)"
        )
    if outcome != "refusal":
        raise WitnessFailure(f"the isolated tampered leg reported an unknown outcome {outcome!r}")
    observed = report["observed"]
    if observed != expected:
        raise WitnessFailure(f"refusal disagreement: expected {expected} observed {observed}")
    print(
        "COUNTERFACT_JSON="
        + json.dumps(
            {
                "expected": expected,
                "observed": observed,
                "reason": f"{entry['tamper_reason']}; {control_note}",
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Run one phase witness and print the verdict line the claims producer parses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        TAMPERED_LEG_CHILD_FLAG,
        action="store_true",
        help=(
            "internal: run only the counterfact phase's tampered leg, reading the tampered "
            "document on stdin and printing TAMPERED_LEG_JSON=<report> (spawned by this runner)"
        ),
    )
    args = parser.parse_args(argv)
    _block_real_sockets()  # every phase is offline by construction, not by convention

    try:
        config = witnesses.provider_config(args.provider)
    except KeyError:
        print(
            "VERDICT=BLOCKED reason=no offline witness configuration "
            f"for provider {args.provider!r}"
        )
        return 3
    try:
        entry = dict(witnesses.model_entry(args.provider, args.model))
    except KeyError:
        print(
            f"VERDICT=BLOCKED reason=provider {args.provider!r} "
            f"declares no witness for {args.model!r}"
        )
        return 3

    evidence_root = REPO_ROOT / args.evidence_root
    entry["hosts"] = config["allowed_hosts"]
    # What the isolated tampered leg has to be re-entered with; a path is not picklable-friendly
    # context for a child, and the child resolves it the same way this process does.
    entry["evidence_root_arg"] = args.evidence_root
    for name, value in config.get("credential_env", {}).items():
        if not os.environ.get(name):
            os.environ[name] = value  # fixture token, never sent anywhere real

    try:
        if args.phase != "implementation":
            entry["document"] = _fixture_document(evidence_root, args.provider, args.model)
        if args.tampered_leg_child:
            return _tampered_leg_child_main(args.provider, args.model, entry)
        runner = {
            "implementation": witness_implementation,
            "direct": witness_direct,
            "service": witness_service,
            "contract": witness_contract,
            "counterfact": witness_counterfact,
        }[args.phase]
        runner(args.provider, args.model, entry)
    except WitnessBlocked as blocked:
        print(f"VERDICT=BLOCKED reason={blocked}")
        return 3
    except WitnessFailure as failure:
        print(f"VERDICT=FAIL reason={failure}")
        return 1
    except Exception as exc:
        print(f"VERDICT=FAIL reason=unclassified witness crash {type(exc).__name__}: {exc}")
        return 1
    print("VERDICT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
