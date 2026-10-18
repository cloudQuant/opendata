r"""Judge the runner's fake governed client against the real one, before paying the re-sign.

Wave 28 step 3b priced the *substitution shape* problem for yfinance's rows and ruled the verb
set out as their blocker, but the same generation printed ``new_verbs=['request']`` with
``flipped_providers={'deribit': 5, 'tmx': 24}``: 29 rows whose fetchers call
``client.request(...)`` while the harness fake answered only ``get``. The runner is pinned as
``probe`` by 1656 receipt slots, so widening that fake is the one edit in this campaign whose
price is known and large. This instrument is the pre-payment reading, and it decides without
touching a canonical receipt:

* ``A_real_surface`` - the class the fake stands in for publishes exactly two verbs, so the
  literal ``{"get", "request"}`` is a measurement and not a guess;
* ``B_head_gap_was_real`` - HEAD's own ``_FakeSharedClient`` published only ``get``, read from
  ``git show HEAD:`` bytes rather than from prose;
* ``C_widening_is_present`` / ``D_get_signature_parity`` - the worktree module adds ``request``
  without changing the keyword arguments ``get`` accepts;
* ``E_get_path_parity_json`` / ``E_get_path_parity_text`` - for the two bodies ``get`` already
  served (a JSON envelope, a multi-part HTML list) live and HEAD return the same status,
  Content-Type, bytes and call log, so no already-accepted receipt can move;
* ``F_post_served_and_head_refuses_it`` - POST is answered live while the same call under HEAD
  raises ``AttributeError``: the counterfact that the old surface could not serve it;
* ``G_sequence_exhaustion_message_parity`` / ``H_no_needle_message_parity`` - the two refusal
  strings that receipts already quote are verbatim-equal to HEAD's;
* ``I_declared_method_refuses_other_verb`` - widening does not turn a POST-shaped fixture into a
  free GET, and a lower-case ``post`` still matches;
* ``J_cursor_shared_across_verbs`` - one paginated fixture advances one cursor whether the source
  uses ``get`` or ``request``, so a page cannot be served twice;
* ``K_call_site_vocabulary_covered`` - every keyword that a real ``<client>.request(...)`` call site
  under ``opendata/`` passes is a *named* parameter of the fake (not swallowed by ``**_extra``), and
  the fake's named set equals the governed client's, so adding the verb also serves its call shape;
* ``L_diff_confined_to_seam_surface`` - every line the worktree changed lies inside the
  ``_FakeSharedClient`` class body or the new ``_echo_request_id`` helper, so the judge, the receipt
  writer and the seam guard are untouched;
* ``N_echo_absent_leaves_bytes_alone`` - a spec that does not declare the echo is encoded exactly as
  HEAD's encoder encodes it, even when the call carries a payload with a different id, which is what
  keeps every receipt already on disk reproducible;
* ``O_echo_fills_only_the_id`` / ``S_echo_with_sequence`` - an echo entry serves the authored body
  with one key replaced, and with a ``sequence`` it serves page 1 then page 2 at the ids asked;
* ``P_echo_refuses_payloadless_verb`` / ``Q_echo_refuses_non_integer_id`` /
  ``R_echo_refuses_non_envelope_json`` / ``R2_echo_refuses_non_json_body`` - the four ways an echo
  declaration can be un-honourable all refuse loudly, rather than serving a body the client would
  then reject for a reason that has nothing to do with the fixture;
* ``T_phase_judges_byte_identical`` - the five witness phases, the seam installer, the seam guard,
  the send accountant and ``main`` are byte-for-byte HEAD's text, which is the claim arm L used to
  make indirectly and is now measured directly;
* ``M_forged_old_surface_refuses_post`` - deleting ``request`` from the loaded class (in this
  process only, never on disk) makes the POST call refuse and the verb set shrink back to
  ``{get}``, and the restore is re-measured: this is the arm that proves the ones above differ.

Every fixture here is built in memory; nothing under ``docs/evidence/C101`` is read or written.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import inspect
import json
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
#: The only module-level definitions wave 29 is allowed to touch: the fake governed client and the
#: echo helper it calls. Arm L charges every other changed line, and arm T re-reads the judges.
ALLOWED_EDIT_REGIONS = ("_FakeSharedClient", "_echo_request_id")
sys.path.insert(0, str(REPO_ROOT))
# The refusal text receipts already carry, copied out of HEAD's source; arm H requires both
# generations to raise exactly this string for the same absent-needle call.
LEGACY_NO_NEEDLE = "client seam fixture covers no response for url='https://h/absent'"

FAILURES: list[str] = []
PASSING: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    """Record and print one arm verdict; the face line at the end counts these, not prose."""
    (PASSING if ok else FAILURES).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")


def _public_verbs(cls: type) -> set[str]:
    return {n for n in vars(cls) if not n.startswith("_") and callable(vars(cls)[n])}


def _module_from_source(name: str, path: Path) -> ModuleType:
    """Execute one file of the runner, so HEAD's bytes and the worktree can be compared."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_live() -> ModuleType:
    return _module_from_source("live_runner", REPO_ROOT / RUNNER_REL)


def _head_bytes() -> bytes:
    return subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{RUNNER_REL}"],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout


def _load_head(blob: bytes) -> ModuleType:
    with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as handle:
        handle.write(blob)
        path = Path(handle.name)
    return _module_from_source("head_runner", path)


def _top_level_range(source: str, name: str) -> tuple[int, int]:
    """Line span of a module-level class or function, decorators included."""
    for node in ast.parse(source).body:
        kinds = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        if isinstance(node, kinds) and node.name == name:
            first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            return first, node.end_lineno or first
    raise AssertionError(f"module-level {name!r} not found")


def _top_level_segment(source: str, name: str) -> str:
    """The exact source text of one module-level definition, for arm T's byte comparison."""
    lines = source.splitlines(keepends=True)
    start, end = _top_level_range(source, name)
    return "".join(lines[start - 1 : end])


def _changed_live_lines(head_text: str, live_text: str) -> list[int]:
    """Line numbers (live-file coordinates) that differ from HEAD; empty means no edit at all."""
    matcher = difflib.SequenceMatcher(None, head_text.splitlines(), live_text.splitlines())
    return [
        line
        for tag, _before, _after, start, stop in matcher.get_opcodes()
        if tag != "equal"
        for line in range(start + 1, stop + 1)
    ]


def _face(fake: Any, response: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """What a receipt-visible caller can observe about one served call."""
    return (
        response.status_code,
        dict(response.headers).get("Content-Type"),
        response.content,
        list(fake.calls),
    )


def _named_params(parameters: dict[str, Any]) -> set[str]:
    """Parameter names a call may pass by keyword; excludes *args/**kwargs catch-alls."""
    return {
        name
        for name, kind in parameters.items()
        if kind.kind in (kind.POSITIONAL_OR_KEYWORD, kind.KEYWORD_ONLY)
    }


def _provider_request_sites() -> list[tuple[str, int, tuple[str, ...]]]:
    """Every ``<client>.request(...)`` call site under ``opendata/``, with the keywords it passes.

    Measured by AST rather than grepped so that the arm's population is the set the fake must serve;
    ``_vendor`` is excluded the same way the gate's own walks exclude it.
    """
    sites: list[tuple[str, int, tuple[str, ...]]] = []
    for path in sorted((REPO_ROOT / "opendata").rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request"
            ):
                keywords = tuple(sorted(k.arg for k in node.keywords if k.arg))
                sites.append((str(path.relative_to(REPO_ROOT)), node.lineno, keywords))
    return sites


def main() -> int:
    """Run every arm and print one face line whose counts come from check(), not from prose."""
    live = _load_live()
    head_blob = _head_bytes()
    head = _load_head(head_blob)
    head_sha = hashlib.sha256(head_blob).hexdigest()[:16]

    from opendata.data.http_client import GovernedHttpClient

    real = {n: v for n, v in vars(GovernedHttpClient).items() if not n.startswith("_")}
    check(
        "A_real_surface",
        set(real) == {"get", "request"} and all(callable(v) for v in real.values()),
        f"real_verbs={sorted(real)} public_count={len(real)}",
    )
    head_verbs = _public_verbs(head._FakeSharedClient)
    live_verbs = _public_verbs(live._FakeSharedClient)
    check(
        "B_head_gap_was_real",
        head_verbs == {"get"},
        f"head_verbs={sorted(head_verbs)} head_sha16={head_sha}",
    )
    check(
        "C_widening_is_present",
        {"get", "request"} <= live_verbs,
        f"live_verbs={sorted(live_verbs)}",
    )
    check(
        "D_get_signature_parity",
        str(inspect.signature(head._FakeSharedClient.get))
        == str(inspect.signature(live._FakeSharedClient.get)),
        f"get_signature={inspect.signature(live._FakeSharedClient.get)}",
    )

    json_spec = {"url_contains": "/quote", "body": {"ok": True}, "status": 200}
    text_spec = {"url_contains": "/page", "body": ["<html>", "<body>"], "status": 201}
    seq_spec = {"url_contains": "/pages", "sequence": [{"p": 1}, {"p": 2}]}

    def specs() -> list[dict[str, Any]]:
        return [dict(json_spec), dict(text_spec)]

    for label, url in (("json", "https://h/quote"), ("text", "https://h/page")):
        live_fake = live._FakeSharedClient(specs())
        head_fake = head._FakeSharedClient(specs())
        live_face = _face(live_fake, live_fake.get(url))
        head_face = _face(head_fake, head_fake.get(url))
        check(
            f"E_get_path_parity_{label}",
            live_face == head_face,
            f"live={live_face[:3]} head={head_face[:3]} calls_equal={live_face[3] == head_face[3]}",
        )

    post_fake = live._FakeSharedClient(specs())
    post_face = _face(post_fake, post_fake.request("POST", "https://h/quote"))
    # The expected body/type comes from the module's own encoder, not from a literal I typed:
    # arm E already pins that encoder against HEAD, so arm F only asks whether request() shares it.
    expected_body, expected_type = live._seam_body_bytes(json_spec["body"])
    try:
        head._FakeSharedClient(specs()).request("POST", "https://h/quote")
        head_refusal = "no-raise"
    except AttributeError as exc:
        head_refusal = f"AttributeError:{exc}"
    check(
        "F_post_served_and_head_refuses_it",
        post_face[:3] == (json_spec["status"], expected_type, expected_body)
        and head_refusal.startswith("AttributeError"),
        f"live={post_face[:3]} live_calls={post_face[3]} live_methods={post_fake.methods} "
        f"head_refusal={head_refusal!r}",
    )

    def exhausted(fake: Any) -> tuple[str, str]:  # noqa: ANN401
        try:
            for _ in range(3):
                fake.get("https://h/pages")
        except Exception as exc:
            return type(exc).__name__, str(exc)
        return "no-raise", ""

    live_g, head_g = (
        exhausted(live._FakeSharedClient([dict(seq_spec)])),
        exhausted(head._FakeSharedClient([dict(seq_spec)])),
    )
    check(
        "G_sequence_exhaustion_message_parity",
        live_g == head_g,
        f"exc={live_g[0]} text_equal={live_g[1] == head_g[1]}",
    )

    def no_needle(client_cls: type) -> str:
        try:
            client_cls([dict(json_spec)]).get("https://h/absent")
        except Exception as exc:
            return str(exc)
        return "no-raise"

    needle_live, needle_head = no_needle(live._FakeSharedClient), no_needle(head._FakeSharedClient)
    check(
        "H_no_needle_message_parity",
        needle_live == needle_head == LEGACY_NO_NEEDLE,
        f"message={needle_live!r} both_equal_literal={needle_live == needle_head}",
    )

    declared = {"url_contains": "/quote", "body": {"ok": True}, "method": "POST"}
    refused = ""
    try:
        live._FakeSharedClient([dict(declared)]).get("https://h/quote")
    except live.WitnessFailure as exc:
        refused = str(exc)
    lower = live._FakeSharedClient([dict(declared)])
    lower_face = _face(lower, lower.request("post", "https://h/quote"))
    check(
        "I_declared_method_refuses_other_verb",
        "declare method" in refused and lower_face[0] == 200,
        f"refused={refused[:40]!r} lower_case_post_status={lower_face[0]}",
    )

    shared = live._FakeSharedClient([dict(seq_spec)])
    pages = [
        shared.get("https://h/pages").content,
        shared.request("GET", "https://h/pages").content,
    ]
    check(
        "J_cursor_shared_across_verbs",
        pages == [b'{"p": 1}', b'{"p": 2}'] and len(shared.calls) == 2,
        f"pages={pages} calls={len(shared.calls)} methods={shared.methods}",
    )

    sites = _provider_request_sites()
    fake_params = inspect.signature(live._FakeSharedClient.request).parameters
    real_params = inspect.signature(GovernedHttpClient.request).parameters
    fake_named = _named_params(fake_params) - {"self", "method", "url"}
    real_named = _named_params(real_params) - {"self", "method", "url"}
    site_kwargs = sorted({k for _path, _lineno, kwargs in sites for k in kwargs})
    var_keyword = [name for name, kind in fake_params.items() if kind.kind is kind.VAR_KEYWORD]
    provider_sites = [s for s in sites if not s[0].endswith("data/http_client.py")]
    check(
        "K_call_site_vocabulary_covered",
        bool(provider_sites) and set(site_kwargs) <= fake_named and fake_named == real_named,
        f"sites_provider={len(provider_sites)} sites_internal={len(sites) - len(provider_sites)} "
        f"site_kwargs={site_kwargs} fake_named={sorted(fake_named)} "
        f"fake_var_keyword={var_keyword} surface_equal={fake_named == real_named}",
    )
    for site_path, site_line, kwargs in sites:
        print(f"  site {site_path}:{site_line} positional=2 kwargs={list(kwargs)}")

    live_text = (REPO_ROOT / RUNNER_REL).read_text(encoding="utf-8")
    head_text = head_blob.decode("utf-8")
    live_lines = live_text.splitlines()
    changed = _changed_live_lines(head_text, live_text)
    regions = [_top_level_range(live_text, name) for name in ALLOWED_EDIT_REGIONS]
    # Inserting a definition shifts the blank separator lines that pycodestyle requires between
    # module-level objects, and difflib counts those as changed. Only a line with text on it can
    # land outside the seam surface, and the number of lines this lets through is printed, not
    # assumed -- arm T is the direct claim that no judge moved, and it does not use this allowance.
    outside_region = [
        line for line in changed if not any(start <= line <= end for start, end in regions)
    ]
    outside = [line for line in outside_region if live_lines[line - 1].strip()]
    blank_excluded = len(outside_region) - len(outside)
    check(
        "L_diff_confined_to_seam_surface",
        bool(changed) and not outside,
        f"regions={regions} changed_lines={len(changed)} outside={outside} "
        f"blank_lines_excluded={blank_excluded}",
    )

    echo_spec = {
        "url_contains": "/rpc",
        "method": "POST",
        "echo_request_id": True,
        "body": {"jsonrpc": "2.0", "id": None, "result": {"ok": 1}, "usOut": 1},
    }
    plain_spec = {"url_contains": "/rpc", "body": {"jsonrpc": "2.0", "id": 7, "result": {}}}

    plain = live._FakeSharedClient([dict(plain_spec)])
    plain_face = plain.request("POST", "https://h/rpc", json_body={"id": 99}).content
    check(
        "N_echo_absent_leaves_bytes_alone",
        plain_face == head._seam_body_bytes(plain_spec["body"])[0],
        f"served={plain_face!r} head_encoder_equal=True",
    )

    echoed = live._FakeSharedClient([dict(echo_spec)])
    echoed_face = echoed.request("POST", "https://h/rpc", json_body={"id": 42}).content
    check(
        "O_echo_fills_only_the_id",
        echoed_face == live._seam_body_bytes({**echo_spec["body"], "id": 42})[0],
        f"served={echoed_face!r}",
    )

    def refused(client: Any, **call: Any) -> str:  # noqa: ANN401  # arm helper drives one call shape
        try:
            client.request("POST", "https://h/rpc", **call)
        except live.WitnessFailure as exc:
            return str(exc)
        return "no-raise"

    payloadless = refused(live._FakeSharedClient([dict(echo_spec)]))
    string_id = refused(live._FakeSharedClient([dict(echo_spec)]), json_body={"id": "42"})
    list_body = refused(
        live._FakeSharedClient([{**echo_spec, "body": [1, 2]}]), json_body={"id": 1}
    )
    non_json = refused(
        live._FakeSharedClient([{**echo_spec, "body": ["plain", "text"]}]), json_body={"id": 1}
    )
    check(
        "P_echo_refuses_payloadless_verb",
        payloadless.startswith("client seam fixture entry") and "no JSON payload" in payloadless,
        f"refusal={payloadless[:60]!r}",
    )
    check(
        "Q_echo_refuses_non_integer_id",
        "not an integer" in string_id,
        f"refusal={string_id[:60]!r}",
    )
    check(
        "R_echo_refuses_non_envelope_json",
        "not a JSON-RPC 2.0 envelope" in list_body,
        f"refusal={list_body[:60]!r}",
    )
    check(
        "R2_echo_refuses_non_json_body",
        "not JSON at all" in non_json,
        f"refusal={non_json[:60]!r}",
    )

    paged = live._FakeSharedClient(
        [
            {
                "url_contains": "/rpc",
                "method": "POST",
                "echo_request_id": True,
                "sequence": [
                    {"jsonrpc": "2.0", "id": None, "page": 1},
                    {"jsonrpc": "2.0", "id": None, "page": 2},
                ],
            }
        ]
    )
    pages = [
        json.loads(paged.request("POST", "https://h/rpc", json_body={"id": asked}).content)
        for asked in (5, 9)
    ]
    check(
        "S_echo_with_sequence",
        [p["id"] for p in pages] == [5, 9] and [p["page"] for p in pages] == [1, 2],
        f"pages={pages} calls={len(paged.calls)}",
    )

    judged = [
        "witness_implementation",
        "witness_direct",
        "witness_service",
        "witness_contract",
        "witness_counterfact",
        "_client_seam",
        "_seam_guard",
        "_carrier_sends",
        "_run_direct",
        "_typed_provider_refusal",
        "_refusal_shape",
        "main",
    ]
    drifted = [
        name
        for name in judged
        if _top_level_segment(live_text, name) != _top_level_segment(head_text, name)
    ]
    check(
        "T_phase_judges_byte_identical",
        not drifted,
        f"judged_definitions={len(judged)} drifted={drifted}",
    )

    # M re-arms the old surface *in this process only* (the file is never rewritten), so the arms
    # above are shown to be able to differ and the run is not a one-sided reading.
    saved_request = live._FakeSharedClient.request
    del live._FakeSharedClient.request
    forged_verbs = _public_verbs(live._FakeSharedClient)
    try:
        live._FakeSharedClient(specs()).request("POST", "https://h/quote")
        forged_refusal = "no-raise"
    except AttributeError as exc:
        forged_refusal = type(exc).__name__
    live._FakeSharedClient.request = saved_request
    restored_verbs = _public_verbs(live._FakeSharedClient)
    check(
        "M_forged_old_surface_refuses_post",
        forged_verbs == {"get"}
        and forged_refusal == "AttributeError"
        and restored_verbs == live_verbs,
        f"forged_verbs={sorted(forged_verbs)} forged_refusal={forged_refusal!r} "
        f"restored_verbs={sorted(restored_verbs)} restored_equal={restored_verbs == live_verbs}",
    )

    print(
        f"fake_client_verb_parity_face arms={len(FAILURES) + len(PASSING)} "
        f"arms_passed={len(PASSING)} arms_failed={len(FAILURES)} failed={FAILURES} "
        f"head_sha16={head_sha} "
        f"live_sha16={hashlib.sha256(live_text.encode('utf-8')).hexdigest()[:16]} "
        f"changed_lines={len(changed)} parity_verdict={'GREEN' if not FAILURES else 'RED'}"
    )
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
