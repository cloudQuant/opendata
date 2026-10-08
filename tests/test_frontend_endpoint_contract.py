"""Every endpoint the frontend declares must be one the backend registered (C31).

``frontend/src/api/*.ts`` is the browser's contract: a path it names but no
router answers is a button that always 404s. Three such calls were found by
measurement this round — ``PATCH /tasks/{id}/toggle``, ``GET /tasks/{id}/executions``
and ``POST /executions/{id}/retry`` — each with a green unit test behind it, and
``PUT /users/{id}/role`` behind a live admin action. None of them were in the
route table (``opendata/api/tasks.py`` registers GET/POST ``/``, GET/PUT/DELETE
``/{id}``, POST ``/{id}/trigger|cancel`` and nothing else).

So the comparison is asserted here, and so is the extractor that performs it:
an extraction that silently returned nothing would also report "no missing
endpoints", which is the failure mode this whole line of work exists to close.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_API = REPO_ROOT / "frontend" / "src" / "api"
REQUEST_MODULE = REPO_ROOT / "frontend" / "src" / "utils" / "request.ts"

# Path literals in the api modules, in the two syntaxes they are written with:
# ``request({url: '/x', method: 'GET'})`` and ``request.get('/x')``. The verb-call
# pattern is DOTALL because ``request.get<unknown, R | undefined>(`` legitimately
# breaks the line before its path literal.
URL_FIELD = re.compile(r"url:\s*[`'\"](/[^`'\"]*)[`'\"]")
VERB_CALL = re.compile(
    r"request\.(get|post|put|patch|delete)(?:<[^)]*>)?\(\s*[`'\"](/[^`'\"]*)[`'\"]", re.S
)
METHOD_FIELD = re.compile(r"method:\s*[`'\"](\w+)[`'\"]")
TEMPLATE_SEGMENT = re.compile(r"\$\{[^}]*\}")
BASE_URL_FIELD = re.compile(r"baseURL:\s*[`'\"]([^`'\"]+)[`'\"]")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

# The extractor's own floor: 43 calls across 8 modules as measured. The numbers
# sit low enough to survive a real refactor and high enough that if either
# syntax stopped matching, the run goes red instead of quietly reporting zero.
MIN_CALLS = 36
MIN_FILES = 8

# Per-module lower bounds, measured this round. The global floor alone cannot see
# a *partial* blindness: when 5 of scripts.ts's 9 calls were rewritten into a
# syntax the extractor doesn't know, the total fell to 38 and the guard stayed
# green (docs/evidence/C31/endpoint-guard-falsifiable.txt, M4 第一遍). Dropping
# below a bound is therefore its own finding, so removing an endpoint has to be
# written down here in the same change that removes it.
MIN_CALLS_PER_FILE = {
    "auth.ts": 5,
    "catalog.ts": 2,
    "data.ts": 8,
    "scripts.ts": 9,
    "settings.ts": 5,
    "tables.ts": 5,
    "tasks.ts": 5,
    "users.ts": 4,
}


def extract_calls(text: str) -> list[tuple[str, str]]:
    """(METHOD, path) pairs declared in one api module, in source order."""
    found: list[tuple[int, str, str]] = [
        (m.start(), m.group(1).upper(), m.group(2)) for m in VERB_CALL.finditer(text)
    ]
    for match in URL_FIELD.finditer(text):
        # The sibling `method:` key, looked for only inside this options object.
        rest = text[match.end() :]
        next_url = rest.find("url:")
        verb = METHOD_FIELD.search(rest if next_url == -1 else rest[:next_url])
        found.append((match.start(), verb.group(1).upper() if verb else "GET", match.group(1)))
    found.sort()
    return [(method, path) for _, method, path in found]


def read_frontend_calls() -> dict[str, list[tuple[str, str]]]:
    files = sorted(FRONTEND_API.glob("*.ts"))
    return {path.name: extract_calls(path.read_text(encoding="utf-8")) for path in files}


def frontend_base_url() -> str:
    """The prefix axios prepends to every declared path."""
    text = REQUEST_MODULE.read_text(encoding="utf-8")
    match = BASE_URL_FIELD.search(text)
    assert match, f"no literal baseURL found in {REQUEST_MODULE}"
    return match.group(1)


def as_regex(template: str) -> re.Pattern[str]:
    """FastAPI path template -> regex; ``${...}`` client holes count as segments."""
    return re.compile("^" + re.sub(r"\{[^/}]+\}|\$\{[^}]*\}", "[^/]+", template) + "$")


def openapi_routes_under(schema: dict, prefix: str) -> list[tuple[str, str]]:
    """Enumerate documented HTTP operations under ``prefix`` from OpenAPI."""
    return sorted(
        (method.upper(), path[len(prefix) :])
        for path, operations in schema.get("paths", {}).items()
        if path.startswith(prefix + "/")
        for method in operations
        if method.lower() in HTTP_METHODS
    )


def declared_backend_routes() -> list[tuple[str, str]]:
    """(METHOD, path template) registered on the app's api router."""
    from fastapi import FastAPI

    from opendata.api import api_router  # imported lazily: the app module is heavy

    prefix = frontend_base_url()
    probe = FastAPI()
    probe.include_router(api_router, prefix=prefix)
    return openapi_routes_under(probe.openapi(), prefix)


def app_routes_under(prefix: str) -> list[tuple[str, str]]:
    """(METHOD, template) the assembled app actually answers, made relative to ``prefix``."""
    from opendata.main import app

    return openapi_routes_under(app.openapi(), prefix)


def backend_table() -> list[tuple[str, re.Pattern[str]]]:
    return [(method, as_regex(template)) for method, template in declared_backend_routes()]


def judge_coverage(calls: dict[str, list[tuple[str, str]]]) -> tuple[str, ...]:
    """Findings about the measurement itself — the plane, not the contract."""
    reasons: list[str] = []
    total = sum(len(declared) for declared in calls.values())
    if len(calls) < MIN_FILES:
        reasons.append(f"VACUOUS: only {len(calls)} api modules read, expected >= {MIN_FILES}")
    if total < MIN_CALLS:
        reasons.append(f"VACUOUS: only {total} declared calls extracted, expected >= {MIN_CALLS}")
    for name, bound in sorted(MIN_CALLS_PER_FILE.items()):
        read = len(calls.get(name, []))
        if read < bound:
            reasons.append(f"PLANE_SHRANK: api/{name} 只读到 {read} 条声明，下限 {bound}")
    return tuple(reasons)


def judge_contract(
    calls: dict[str, list[tuple[str, str]]], table: list[tuple[str, re.Pattern[str]]]
) -> tuple[str, ...]:
    """Findings: declared calls the registered route table cannot answer."""
    reasons: list[str] = []
    if not table:
        reasons.append("VACUOUS: the backend route table is empty")
    for name, declared in sorted(calls.items()):
        if not declared:
            reasons.append(f"NO_CALLS_READ: {name} declares no endpoint")
        for method, path in declared:
            probe = TEMPLATE_SEGMENT.sub("1", path)
            hits = [hit for hit, rx in table if rx.match(probe)]
            if not hits:
                reasons.append(f"NOT_REGISTERED: {method} {path}  <- api/{name}")
            elif method not in hits:
                reasons.append(
                    f"VERB_NOT_REGISTERED: {method} {path}  <- api/{name}"
                    f"（路由表里该路径只有 {'/'.join(sorted(set(hits)))}）"
                )
    return tuple(reasons)


def judge(
    calls: dict[str, list[tuple[str, str]]], table: list[tuple[str, re.Pattern[str]]]
) -> tuple[str, ...]:
    return judge_coverage(calls) + judge_contract(calls, table)


class TestShippedContract:
    def test_every_declared_endpoint_is_registered(self) -> None:
        assert judge(read_frontend_calls(), backend_table()) == ()

    def test_the_extractor_reads_the_whole_api_surface(self) -> None:
        calls = read_frontend_calls()
        total = sum(len(v) for v in calls.values())

        assert len(calls) >= MIN_FILES
        assert total >= MIN_CALLS
        # One call per client syntax: if either form stops matching, `total`
        # drops and the shipped assertion goes red rather than silent.
        assert ("GET", "/data/catalog") in calls["catalog.ts"]
        assert ("POST", "/pipeline/retry-failed") in calls["data.ts"]

    def test_the_browser_prefix_is_a_real_mount(self) -> None:
        """The judge compares *router-relative* paths; the browser sends baseURL + path.

        If the app stopped mounting what axios prepends, all 43 calls would 404
        at once while the comparison above stayed green.
        """
        prefix = frontend_base_url()
        mounted = app_routes_under(prefix)

        assert prefix.startswith("/")
        assert sorted(mounted) == sorted(declared_backend_routes())
        assert len(mounted) >= 50


class TestGuardIsFalsifiable:
    ROUTES = [("GET", as_regex("/tasks/")), ("GET", as_regex("/tasks/{task_id}"))]

    def test_an_endpoint_the_backend_never_registered_is_named(self) -> None:
        reasons = judge_contract({"tasks.ts": [("PATCH", "/tasks/1/toggle")]}, self.ROUTES)

        assert reasons == ("NOT_REGISTERED: PATCH /tasks/1/toggle  <- api/tasks.ts",)

    def test_a_wrong_verb_on_a_real_path_is_named(self) -> None:
        reasons = judge_contract({"tasks.ts": [("DELETE", "/tasks/7")]}, self.ROUTES)

        assert len(reasons) == 1
        assert reasons[0].startswith("VERB_NOT_REGISTERED: DELETE /tasks/7")
        assert "只有 GET" in reasons[0]

    def test_a_client_template_path_still_matches_the_route_template(self) -> None:
        reasons = judge_contract({"tasks.ts": [("GET", "/tasks/${taskId}")]}, self.ROUTES)

        assert reasons == ()

    def test_an_extractor_that_reads_nothing_cannot_report_green(self) -> None:
        assert any(r.startswith("VACUOUS:") for r in judge_coverage({}))

    def test_a_module_that_goes_partially_blind_is_named(self) -> None:
        """The M4 形状: 5 of scripts.ts's 9 calls rewritten into an unknown syntax.

        43-5=38 still clears the global floor, so without the per-module bound the
        guard reads green while a fifth of one module has gone invisible.
        """
        calls = {name: [("GET", "/tasks/")] * bound for name, bound in MIN_CALLS_PER_FILE.items()}
        calls["scripts.ts"] = calls["scripts.ts"][:-5]

        reasons = judge_coverage(calls)

        assert "PLANE_SHRANK: api/scripts.ts 只读到 4 条声明，下限 9" in reasons
        assert not any(r.startswith("VACUOUS:") for r in reasons)

    def test_the_declared_plane_is_consistent_with_the_floor(self) -> None:
        assert sum(MIN_CALLS_PER_FILE.values()) >= MIN_CALLS
        assert len(MIN_CALLS_PER_FILE) >= MIN_FILES

    def test_an_api_module_that_yields_no_calls_is_named(self) -> None:
        calls = {f"{n}.ts": [("GET", "/tasks/")] for n in "abcdefgh"}
        calls["quiet.ts"] = []

        reasons = judge_contract(calls, self.ROUTES)

        assert "NO_CALLS_READ: quiet.ts declares no endpoint" in reasons

    def test_an_empty_route_table_cannot_confirm_anything(self) -> None:
        reasons = judge_contract({"tasks.ts": [("GET", "/tasks/")]}, [])

        assert reasons[0] == "VACUOUS: the backend route table is empty"

    def test_the_two_client_syntaxes_are_both_understood(self) -> None:
        text = (
            "const a = request({\n  url: '/tables/warehouse',\n  method: 'GET',\n})\n"
            "const b = await request.post<unknown, R>('/pipeline/retry-failed')\n"
            "const c = await request.get<unknown, R | undefined>(\n  '/data/catalog',\n)\n"
        )

        assert sorted(extract_calls(text)) == sorted(
            [
                ("GET", "/tables/warehouse"),
                ("POST", "/pipeline/retry-failed"),
                ("GET", "/data/catalog"),
            ]
        )

    def test_a_method_key_from_the_next_call_is_not_borrowed(self) -> None:
        text = (
            "const a = request({ url: '/tasks/' })\n"
            "const b = request({ url: '/users/', method: 'DELETE' })\n"
        )

        assert extract_calls(text) == [("GET", "/tasks/"), ("DELETE", "/users/")]


def test_backend_table_is_not_empty() -> None:
    """A guard over an empty route table would report every call as missing."""
    table = backend_table()

    assert len(table) > 50
    assert any(m == "GET" and rx.match("/data/catalog") for m, rx in table)
