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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


class WitnessFailure(AssertionError):  # noqa: N818  # verdict vocabulary, not a retry error
    """The witness ran and the checked property did not hold."""


class WitnessBlocked(Exception):  # noqa: N818  # verdict vocabulary, not a retry error
    """A precondition is missing, so the witness could not honestly run."""


def _grant_context(provider: str, model: str, hosts: list[str], *, sends: int = 3) -> FetchContext:
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
    if isinstance(body, str):
        return body.encode("utf-8"), "text/plain"
    if isinstance(body, list) and all(isinstance(line, str) for line in body):
        return "\n".join(body).encode("utf-8"), "text/html"
    return json.dumps(body).encode("utf-8"), "application/json"


class _FakeSharedClient:
    """Serves the fixture envelope through the governed client's own ``get`` surface."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

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
        self.calls.append(url)
        for spec in self.responses:
            needle = spec.get("url_contains")
            if isinstance(needle, str) and needle in url:
                import requests

                body, default_type = _seam_body_bytes(spec.get("body"))
                response = requests.Response()
                response.status_code = int(spec.get("status", 200))
                response._content = body  # the documented requests fill-in point
                response.url = url
                response.reason = "OK"
                response.headers.update({"Content-Type": spec.get("content_type", default_type)})
                return response
        raise WitnessFailure(f"client seam fixture covers no response for url={url!r}")


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
    client = _FakeSharedClient([spec for spec in responses if isinstance(spec, dict)])
    attr = seam["attr"]
    restored: list[tuple[Any, str, Any]] = []  # module objects
    for name in seam["modules"]:
        module = importlib.import_module(name)
        if not hasattr(module, attr):
            raise WitnessBlocked(f"client seam module {name!r} publishes no {attr!r} to patch")
        restored.append((module, attr, getattr(module, attr)))
        setattr(module, attr, lambda _c=client: _c)
    try:
        yield client
    finally:
        for module, attr_name, original in restored:
            setattr(module, attr_name, original)


def _seam_guard(client: _FakeSharedClient) -> None:
    """A fetch that never asked the fake anything is not an offline witness."""
    if not client.calls:
        raise WitnessFailure(
            "the fetch completed without ever calling the declared offline client seam"
        )


def _run_direct(
    provider: str,
    model: str,
    entry: dict,
    document: Any,  # noqa: ANN401  # fixture JSON
) -> tuple[list, Any, Any]:  # rows and transport are dynamic by nature
    """Fetch through the offline seam on a fresh fetcher instance; return rows."""
    fetcher_type = _resolve_fetcher_type(provider, model)
    transport = FixedResponseTransport(HttpResponse(200, copy.deepcopy(document)))
    probe_class = type(
        f"Probed{fetcher_type.__name__}", (fetcher_type,), {"http_transport": transport}
    )
    instance = probe_class()
    ctx = _grant_context(provider, model, entry["hosts"])
    rows = list(instance.fetch(ctx=ctx, **entry["query"]))
    if not transport.calls:
        raise WitnessFailure("the fetch completed without ever calling the offline transport seam")
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
) -> tuple[list, Any, Any]:  # rows and transport are dynamic by nature
    """Run :func:`_run_direct`, or the declared client seam instead of the engine one.

    Providers whose fetchers call ``get_shared_http_client()`` directly have no ``http_transport``
    to honour; ``transport.calls`` would then be empty and the engine path would fail -- which is
    exactly the refusal that kept those rows unwitnessed. The seam substitutes the governed client
    for the modules the witness names, so the parsing, normalization and refusal code that ships
    is still the code under test.
    """
    if "client_seam" not in entry:
        return _run_direct(provider, model, entry, document)
    fetcher_type = _resolve_fetcher_type(provider, model)
    instance = fetcher_type()
    ctx = _grant_context(provider, model, entry["hosts"])
    with _client_seam(entry, document) as client:
        rows = list(instance.fetch(ctx=ctx, **entry["query"]))
        _seam_guard(client)
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
    ctx = _grant_context(provider, model, entry["hosts"])

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
    try:
        _run_direct_with_seam(provider, model, entry, tampered)
    except (WitnessFailure, WitnessBlocked):
        raise
    except Exception as exc:  # the refusal under test is whatever the provider ships
        observed = _refusal_shape(exc, provider, entry["refusal_code"])
    else:
        raise WitnessFailure(
            "counterfact did not bite: the tampered fixture still produced rows"
        ) from None
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
    for name, value in config.get("credential_env", {}).items():
        if not os.environ.get(name):
            os.environ[name] = value  # fixture token, never sent anywhere real

    try:
        if args.phase != "implementation":
            entry["document"] = _fixture_document(evidence_root, args.provider, args.model)
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
