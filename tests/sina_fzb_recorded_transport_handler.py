"""Strict, subprocess-local replay transport for the Sina FZB integration test."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

FIXTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
WITNESS_FILENAME = "sina-fzb-recorded-transport-witness.json"
ARCHIVE_DIRECTORY = "sina-fzb-recorded-transport-archive"
_CHILD_ENVIRONMENT = {
    "APP_ENV": "testing",
    "TESTING": "true",
    "REDIS_URL": "",
    "SENTRY_DSN": "",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_FIXED_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_EXPECTED_PARAMS = {
    "paperCode": "sh600519",
    "source": "fzb",
    "type": "0",
    "page": "1",
    "num": "1000",
}
_SUMMARY_PARAM_NAMES = {
    "paperCode": "paper_code",
    "source": "source",
    "type": "statement_type",
    "page": "page",
    "num": "page_size",
}
_UTC = timezone.utc
_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)


def _is_process_executor_child() -> bool:
    """Identify only the isolated process-executor script, never the pytest parent."""
    return Path(sys.argv[0]).name == "process_executor_worker.py"


def _restrict_child_environment() -> None:
    """Replace interpreter-injected and inherited variables with explicit test inputs."""
    os.environ.clear()
    os.environ.update(_CHILD_ENVIRONMENT)


if _is_process_executor_child():
    # Python may inject LC_CTYPE during startup even when the parent environment is clean.
    # Scrub before importing the provider/client modules used by this test handler.
    _restrict_child_environment()


def recorded_fixture() -> tuple[dict[str, object], bytes]:
    """Load and validate the single SHA-pinned Sina request and response."""
    from opendata.data.providers.sina.financial_statement_contract import (
        MAX_RAW_RESPONSE_BYTES,
        SINA_FZB_URL,
    )

    archive = _FIXTURE_PATH.read_bytes()
    if hashlib.sha256(archive).hexdigest() != FIXTURE_SHA256:
        raise RuntimeError("recorded Sina fixture digest mismatch")
    calls = json.loads(gzip.decompress(archive))
    if type(calls) is not list or len(calls) != 1 or type(calls[0]) is not dict:
        raise RuntimeError("recorded Sina fixture shape mismatch")
    call: dict[str, object] = calls[0]
    expected_fields = {
        "method",
        "url",
        "params",
        "status",
        "content_type",
        "encoding",
        "content_b64",
    }
    if set(call) != expected_fields:
        raise RuntimeError("recorded Sina fixture fields mismatch")
    if (
        call["method"] != "GET"
        or SINA_FZB_URL != _FIXED_URL
        or call["url"] != _FIXED_URL
        or call["params"] != _EXPECTED_PARAMS
        or call["status"] != 200
        or call["content_type"] != "application/json; charset=utf-8"
        or call["encoding"] != "utf-8"
        or type(call["content_b64"]) is not str
    ):
        raise RuntimeError("recorded Sina request metadata mismatch")
    body = base64.b64decode(call["content_b64"], validate=True)
    if not body or len(body) > MAX_RAW_RESPONSE_BYTES:
        raise RuntimeError("recorded Sina response body is outside the bounded contract")
    return call, body


class _ReplayState:
    """Keep a non-secret, child-local witness of transport and archive effects."""

    def __init__(self, *, cwd: Path, body: bytes) -> None:
        self.cwd = cwd
        self.body = body
        self.calls: list[dict[str, object]] = []
        self.fallbacks: list[dict[str, str]] = []
        self.archive: dict[str, str] | None = None

    def write_witness(self) -> None:
        """Create one mode-0600 witness beneath the test's temporary cwd."""
        payload = {
            "fixture_sha256": FIXTURE_SHA256,
            "response_sha256": hashlib.sha256(self.body).hexdigest(),
            "child_environment_keys": sorted(os.environ),
            "calls": self.calls,
            "network_fallbacks_blocked": self.fallbacks,
            "archive": self.archive,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        path = self.cwd / WITNESS_FILENAME
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("witness write made no progress")
                view = view[written:]
        finally:
            os.close(descriptor)


def sina_fzb_recorded_transport_handler(
    parameters: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Invoke production capture through one closed, fixture-only transport."""
    if _is_process_executor_child():
        _restrict_child_environment()

    import requests
    from urllib3.response import HTTPResponse
    from urllib3.util.retry import Retry

    from opendata.data.http_client import GovernedHttpClient
    from opendata.data.protocol import FetchContext
    from opendata.data.providers.sina import financial_statement_process as process_module
    from opendata.data.providers.sina.financial_statement_contract import (
        MAX_RAW_RESPONSE_BYTES,
        SINA_FZB_CANONICAL_MODEL,
        SINA_FZB_HOST,
        SINA_FZB_URL,
        SINA_SOURCE,
    )
    from opendata.data.request_budget import (
        GrantDecision,
        RequestBudget,
        RequestBudgetProfile,
        RequestGrant,
        RequestOperation,
    )
    from opendata.pipeline.raw_evidence import (
        RawResponseArtifactWriter,
        RawResponseSource,
        request_summary_sha256,
    )

    project_root = Path(__file__).resolve().parents[1]
    cwd = Path.cwd().resolve()
    if cwd == project_root or project_root in cwd.parents:
        raise RuntimeError("recorded transport requires a private temporary cwd")
    call, body = recorded_fixture()
    state = _ReplayState(cwd=cwd, body=body)

    class _TrackedRaw(HTTPResponse):
        """Expose only the pinned in-memory body through Requests streaming."""

        def __init__(self, response_body: bytes) -> None:
            super().__init__(body=io.BytesIO(response_body), preload_content=False)

    class _NetworkDenyAdapter(requests.adapters.BaseAdapter):
        """Reject every URL that did not match the exact recorded endpoint."""

        max_retries = Retry(total=0)

        def send(
            self,
            request: requests.PreparedRequest,
            **_kwargs: object,
        ) -> requests.Response:
            parsed = urlsplit(request.url or "")
            state.fallbacks.append({"host": parsed.hostname or "", "path": parsed.path or "/"})
            raise requests.ConnectionError("recorded replay adapter blocks network fallback")

        def close(self) -> None:
            """No socket exists to close."""

    class _RecordedReplayAdapter(requests.adapters.BaseAdapter):
        """Serve the frozen response only for its exact source request identity."""

        max_retries = Retry(total=0)

        def send(
            self,
            request: requests.PreparedRequest,
            **_kwargs: object,
        ) -> requests.Response:
            expected_url = f"{SINA_FZB_URL}?{urlencode(_EXPECTED_PARAMS)}"
            observed_url = request.url or ""
            observed_pairs = parse_qsl(urlsplit(observed_url).query, keep_blank_values=True)
            observed = {
                "method": request.method or "",
                "url": observed_url,
                "params": observed_pairs,
                "accept_encoding": request.headers.get("Accept-Encoding", ""),
                "authorization_present": "Authorization" in request.headers,
            }
            expected_pairs = list(_EXPECTED_PARAMS.items())
            accepted = (
                len(state.calls) == 0
                and request.method == call["method"]
                and observed_url == expected_url
                and observed_pairs == expected_pairs
                and request.headers.get("Accept-Encoding") == "identity"
                and "Authorization" not in request.headers
            )
            state.calls.append({"accepted": accepted, **observed})
            if not accepted:
                raise requests.ConnectionError("recorded replay request identity mismatch")

            summary_parameters = {
                safe_name: _EXPECTED_PARAMS[wire_name]
                for wire_name, safe_name in _SUMMARY_PARAM_NAMES.items()
            }
            summary_sha256 = request_summary_sha256(
                summary_parameters,
                allowed_fields=tuple(summary_parameters),
            )
            artifact = RawResponseArtifactWriter(
                cwd / ARCHIVE_DIRECTORY,
                max_response_bytes=MAX_RAW_RESPONSE_BYTES,
            ).capture(
                (body,),
                source=RawResponseSource(
                    provider_id="sina",
                    dataset_id="sina_fzb_report_observations",
                    operation_id="fzb_capture",
                ),
                request_summary_sha256=summary_sha256,
                http_status=200,
                retention_class="unknown",
                source_history_limit="unknown",
            )
            state.archive = {
                "artifact_path": str(artifact.artifact_path.relative_to(cwd)),
                "manifest_path": str(artifact.manifest_path.relative_to(cwd)),
                "request_summary_sha256": artifact.manifest.request_summary_sha256,
                "response_sha256": artifact.manifest.response_sha256,
            }

            response = requests.Response()
            response.status_code = 200
            response.url = observed_url
            response.request = request
            response.connection = self
            response.encoding = "utf-8"
            response.headers["Content-Type"] = str(call["content_type"])
            response.headers["Content-Encoding"] = "identity"
            response.headers["Content-Length"] = str(len(body))
            response.raw = _TrackedRaw(body)
            response._content = False
            response._content_consumed = False
            return response

        def close(self) -> None:
            """No socket exists to close."""

    def test_only_context() -> FetchContext:
        """Grant exactly one bounded test replay, with no unresolved conditions."""
        grant = RequestGrant(
            source=SINA_SOURCE,
            canonical_model=SINA_FZB_CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            decision=GrantDecision.ALLOWED,
            rights_evidence="test-only fixture replay; no live network entitlement",
            task_attempts=1,
            source_attempts=1,
            allowed_hosts=frozenset((SINA_FZB_HOST,)),
            expires_at=datetime.max.replace(tzinfo=_UTC),
            conditions=(),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
        budget = RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
        return FetchContext(
            timeout=20.0,
            request_budget=budget,
            operation=RequestOperation.QUERY,
        )

    def session_factory(_client: GovernedHttpClient) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.auth = None
        session.proxies.clear()
        deny_adapter = _NetworkDenyAdapter()
        session.mount("https://", deny_adapter)
        session.mount("http://", deny_adapter)
        session.mount(SINA_FZB_URL, _RecordedReplayAdapter())
        return session

    original_context_factory = process_module._default_denied_context
    original_session_factory = GovernedHttpClient._get_session
    process_module._default_denied_context = test_only_context
    GovernedHttpClient._get_session = session_factory
    try:
        return process_module.fetch_sina_fzb_capture(parameters)
    finally:
        process_module._default_denied_context = original_context_factory
        GovernedHttpClient._get_session = original_session_factory
        state.write_witness()
