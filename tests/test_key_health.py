"""Graded credential health tests (AC-19 / NFR-5, C28).

AC-19 recorded the quota/expiry/ban half of the Key plane as a 演进项 because
"Key 服务无到期字段主动源". These tests do not pretend otherwise. What they hold
the plane to is that every failure the patrol *can* see is classified into a
class with an owner, that the two confusions which would make the plane
worthless are impossible, that a configured Key is never thereby healthy, and
that nothing the plane renders can carry a credential.

The injected shapes are the ones actually archived under ``docs/evidence/`` -
fuyao's stable codes, the macro providers' ``status=`` messages, the ported
tree's ``raise_for_status`` text, a bare ``TimeoutError`` - so every assertion
describes a failure that has really happened here, not a plausible one. The
httpx/requests shapes are the two that *would* carry a Key in their message:
those tests assert the danger is present in the raw text before asserting the
plane hides it.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest
from requests.exceptions import HTTPError

from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.ths.models._client import ThsProviderError
from opendata.data.providers.ths.transport.errors import (
    error_for_transport,
    error_for_upstream_code,
)
from opendata.pipeline.key_health import (
    CLASS_CREDENTIAL_REJECTED,
    CLASS_LEVELS,
    CLASS_LOCAL_REQUEST,
    CLASS_NO_DATA,
    CLASS_NOT_CONFIGURED,
    CLASS_OWNERS,
    CLASS_PATROL_GAP,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_SOURCE_DEGRADED,
    CLASS_TRANSIENT_BLIP,
    CLASS_UNCLASSIFIED,
    CREDENTIAL_CLASSES,
    LEVEL_ALERT,
    LEVEL_INFO,
    LEVEL_NOT_APPLICABLE,
    LEVEL_PRESENCE_ONLY,
    LEVEL_WARN,
    REDACTED_MARKER,
    UNVERIFIED_WITHOUT_ACTIVE_CHECK,
    FailureObservation,
    KeyReport,
    attribution_of,
    build_report,
    classify_failure,
    redact,
)
from opendata.pipeline.patrol import credential_health

if TYPE_CHECKING:
    from collections.abc import Callable

#: Stands in for a real Key value. The tests assert its absence from everything
#: the plane renders; no real credential is read or printed here.
CANARY = "CANARY-SECRET-do-not-render"

#: A Key-in-query source's URL as httpx and requests write it into an exception
#: message after ``raise_for_status()`` - query, and so credential, included.
LEAKY_URL = f"https://api.stlouisfed.org/fred/series/observations?series_id=CPIA&api_key={CANARY}"
LEAKY_ORIGIN = "https://api.stlouisfed.org/fred/series/observations"

ALL_CLASSES = (
    CLASS_NOT_CONFIGURED,
    CLASS_CREDENTIAL_REJECTED,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_SOURCE_DEGRADED,
    CLASS_TRANSIENT_BLIP,
    CLASS_NO_DATA,
    CLASS_LOCAL_REQUEST,
    CLASS_PATROL_GAP,
    CLASS_UNCLASSIFIED,
)


class _Settings:
    """Settings stub whose Key values are the canary.

    Attributes:
        fuyao_api_key: The canary, standing in for a configured ths Key.
        fred_api_key: Empty, standing in for a Key that was never configured.
    """

    fuyao_api_key = CANARY
    fred_api_key = ""


class _Leg:
    """The two fields ``credential_health`` reads, without running a probe.

    Attributes:
        source: Source the probe ran against.
        failure_class: Classified shape of the failure it kept.
        attribution: Metadata-only description of that failure.
    """

    def __init__(self, source: str, failure_class: str, attribution: str | None) -> None:
        """Describe one graded leg."""
        self.source = source
        self.failure_class = failure_class
        self.attribution = attribution


def _httpx_status_error(status: int, reason: str, url: str = LEAKY_URL) -> httpx.HTTPStatusError:
    """Build the exception ``raise_for_status()`` really raises on httpx.

    Args:
        status: HTTP status of the stub response.
        reason: Reason phrase the message carries.
        url: Request URL, query and canary credential included.

    Returns:
        The exception, with its response attached - which is how it arrives
        from a transport rather than being written by hand.
    """
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request, headers={"content-type": "text/plain"})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


def _fred(status: int | None = None, url: str | None = None) -> FredProviderError:
    """Build a FRED provider failure.

    Args:
        status: HTTP status, when the failure came from one.
        url: Request URL the client put in the message.

    Returns:
        The provider error.
    """
    return FredProviderError("FRED_HTTP_ERROR", status=status, url=url)


class TestArchivedShapes:
    """Each class is reachable, from a failure this repo has already recorded."""

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda: error_for_upstream_code(2001), CLASS_CREDENTIAL_REJECTED),
            (lambda: error_for_upstream_code(2003), CLASS_CREDENTIAL_REJECTED),
            (lambda: error_for_transport("http", detail="401"), CLASS_CREDENTIAL_REJECTED),
            (lambda: error_for_upstream_code(4001), CLASS_QUOTA_EXHAUSTED),
            (lambda: error_for_transport("rate_limited", detail="429"), CLASS_QUOTA_EXHAUSTED),
            (lambda: error_for_upstream_code(5001), CLASS_SOURCE_DEGRADED),
            (lambda: error_for_transport("timeout"), CLASS_TRANSIENT_BLIP),
            (lambda: error_for_transport("network", detail="ConnectError"), CLASS_TRANSIENT_BLIP),
            (lambda: error_for_upstream_code(3001), CLASS_NO_DATA),
            (lambda: error_for_upstream_code(3002), CLASS_NO_DATA),
            (lambda: error_for_upstream_code(1001), CLASS_LOCAL_REQUEST),
            (
                lambda: error_for_transport("envelope_invalid", detail="ticker_code_mismatch"),
                CLASS_LOCAL_REQUEST,
            ),
            (lambda: FredProviderError("FRED_API_KEY_MISSING"), CLASS_NOT_CONFIGURED),
            (lambda: ThsProviderError("THS_NOT_CONFIGURED"), CLASS_NOT_CONFIGURED),
            (lambda: ThsProviderError("THS_EMPTY_RESPONSE"), CLASS_NO_DATA),
            (lambda: ThsProviderError("THS_SYMBOL_UNRESOLVED"), CLASS_LOCAL_REQUEST),
            (
                lambda: EcbProviderError(
                    "ECB_HTTP_ERROR", status=400, url="https://data-api.ecb.europa.eu/service/x"
                ),
                CLASS_LOCAL_REQUEST,
            ),
            (lambda: TimeoutError(), CLASS_TRANSIENT_BLIP),
            (
                lambda: HTTPError(
                    "502 Server Error: Bad Gateway for url: "
                    "https://push2delay.eastmoney.com/api/qt/stock/kline/get?secid=1.600519"
                ),
                CLASS_SOURCE_DEGRADED,
            ),
        ],
        ids=[
            "fuyao-2001",
            "fuyao-2003",
            "fuyao-http-401",
            "fuyao-4001",
            "fuyao-transport-429",
            "fuyao-5001",
            "fuyao-timeout",
            "fuyao-network",
            "fuyao-3001",
            "fuyao-3002",
            "fuyao-1001",
            "fuyao-envelope",
            "fred-key-missing",
            "ths-not-configured",
            "ths-empty",
            "ths-symbol",
            "ecb-400",
            "bare-timeout",
            "em-502",
        ],
    )
    def test_a_recorded_shape_lands_in_one_class(
        self, build: Callable[[], BaseException], expected: str
    ) -> None:
        assert classify_failure(build()) == expected

    def test_a_ths_failure_with_detail_text_still_names_its_code(self) -> None:
        # The adapter raises one code with caller-controlled detail; the detail
        # stays in the message and never reaches what the plane reports.
        error = ThsProviderError(f"THS_SYMBOL_INVALID: {CANARY}")
        assert classify_failure(error) == CLASS_LOCAL_REQUEST
        assert CANARY not in attribution_of(error)

    def test_a_refused_status_outranks_a_code_naming_another_cause(self) -> None:
        # A client that could not parse an empty 401 body reports a parse
        # failure. Reading that as "our request was wrong" would file a banned
        # Key under the porting layer instead of the credential holder.
        error = EcbProviderError("ECB_BAD_RESPONSE", status=401, url="https://data-api.ecb.eu")
        assert classify_failure(error) == CLASS_CREDENTIAL_REJECTED


class TestBlipIsNeverABan:
    """Reverse guard one half: a blip is not a credential problem."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: TimeoutError(),
            lambda: httpx.ConnectError("connection reset"),
            lambda: ConnectionError("remote end closed connection"),
            lambda: HTTPError(
                "502 Server Error: Bad Gateway for url: https://push2delay.eastmoney.com/a"
            ),
            lambda: _httpx_status_error(503, "Service Unavailable", LEAKY_ORIGIN),
            lambda: error_for_transport("timeout"),
            lambda: error_for_transport("network", detail="ConnectError"),
            lambda: error_for_upstream_code(5001),
        ],
        ids=[
            "timeout",
            "httpx-connect",
            "connection-error",
            "em-502",
            "httpx-503",
            "fuyao-timeout",
            "fuyao-network",
            "fuyao-5001",
        ],
    )
    def test_a_blip_is_not_a_credential_class(self, build: Callable[[], BaseException]) -> None:
        assert classify_failure(build()) not in CREDENTIAL_CLASSES

    def test_a_blip_does_not_ask_anyone_to_touch_a_key(self) -> None:
        report = _report_for("ths", (classify_failure(TimeoutError()),))
        assert report.level == LEVEL_INFO
        assert report.owner == CLASS_OWNERS[CLASS_TRANSIENT_BLIP]


class TestBanIsNeverABlip:
    """The other half: a refusal is not something to retry away."""

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda: _fred(401, LEAKY_ORIGIN), CLASS_CREDENTIAL_REJECTED),
            (
                lambda: EcbProviderError("ECB_HTTP_ERROR", status=403, url=None),
                CLASS_CREDENTIAL_REJECTED,
            ),
            (lambda: error_for_upstream_code(2001), CLASS_CREDENTIAL_REJECTED),
            (lambda: _httpx_status_error(401, "Unauthorized"), CLASS_CREDENTIAL_REJECTED),
        ],
        ids=["fred-401", "ecb-403", "fuyao-2001", "httpx-401"],
    )
    def test_a_refusal_is_a_credential_class(
        self, build: Callable[[], BaseException], expected: str
    ) -> None:
        assert classify_failure(build()) == expected

    def test_a_refusal_pages_and_names_the_credential_holder(self) -> None:
        report = _report_for("fred", (CLASS_CREDENTIAL_REJECTED,))
        assert report.level == LEVEL_ALERT
        assert report.owner == CLASS_OWNERS[CLASS_CREDENTIAL_REJECTED]


class TestRejectedKeyIsNotMissingData:
    """Reverse guard two: 401 must not read as "this source has no data"."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: _fred(401),
            lambda: error_for_upstream_code(2003),
            lambda: _httpx_status_error(403, "Forbidden", LEAKY_ORIGIN),
        ],
        ids=["fred-401", "fuyao-2003", "httpx-403"],
    )
    def test_a_refusal_is_never_recorded_as_no_data(
        self, build: Callable[[], BaseException]
    ) -> None:
        assert classify_failure(build()) != CLASS_NO_DATA

    @pytest.mark.parametrize(
        "build",
        [
            lambda: error_for_upstream_code(3001),
            lambda: ThsProviderError("THS_EMPTY_RESPONSE"),
            lambda: _httpx_status_error(404, "Not Found", LEAKY_ORIGIN),
        ],
        ids=["fuyao-3001", "ths-empty", "httpx-404"],
    )
    def test_an_empty_answer_is_never_a_credential_problem(
        self, build: Callable[[], BaseException]
    ) -> None:
        assert classify_failure(build()) == CLASS_NO_DATA

    def test_no_data_does_not_ask_for_a_new_key(self) -> None:
        report = _report_for("ths", (CLASS_NO_DATA,))
        assert report.owner == CLASS_OWNERS[CLASS_NO_DATA]
        assert report.level != LEVEL_ALERT


class TestUnreadableIsNeverAPass:
    """What the plane cannot read must be the loudest thing on the page."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: RuntimeError("asset 42 rows were dropped"),
            lambda: ValueError("no status here"),
            lambda: KeyError("FRED_HTTP_ERROR"),
            lambda: AttributeError("ths said something new"),
        ],
        ids=["digits-in-text", "no-status", "code-as-key", "brand-new-type"],
    )
    def test_an_unknown_failure_is_unclassified(self, build: Callable[[], BaseException]) -> None:
        assert classify_failure(build()) == CLASS_UNCLASSIFIED

    def test_unclassified_warns_and_blames_nobody(self) -> None:
        report = _report_for("ths", (CLASS_UNCLASSIFIED,))
        assert report.level == LEVEL_WARN
        assert report.owner == CLASS_OWNERS[CLASS_UNCLASSIFIED]

    def test_a_row_count_in_the_text_is_not_read_as_a_status(self) -> None:
        # The reason the text fallback is anchored to the two message formats
        # the transports actually write: an unanchored match would grade "42
        # rows" as a 402 and bill it to the credential.
        assert classify_failure(RuntimeError("42 rows, 1001 columns")) == CLASS_UNCLASSIFIED

    @pytest.mark.parametrize("status_code", [200, 204, 302])
    def test_a_status_none_of_the_tables_name_is_not_guessed_at(self, status_code: int) -> None:
        # 2xx/3xx arriving as a failure is a shape this repo has never seen.
        # Reading it as "the closest known class" would retire the one line
        # that says the plane is out of date.
        exc = EcbProviderError(
            "ECB_HTTP_ERROR", status=status_code, url="https://data-api.ecb.europa.eu/service/x"
        )
        assert classify_failure(exc) == CLASS_UNCLASSIFIED


class TestPlaneIsClosed:
    """A class without an owner or a level would be a hole, not a gap."""

    def test_every_class_has_exactly_one_owner_and_one_level(self) -> None:
        assert set(CLASS_OWNERS) == set(ALL_CLASSES)
        assert set(CLASS_LEVELS) == set(ALL_CLASSES)

    def test_the_credential_classes_are_the_ones_named_at_all(self) -> None:
        assert CREDENTIAL_CLASSES.issubset(CLASS_OWNERS)
        assert CLASS_CREDENTIAL_REJECTED in CREDENTIAL_CLASSES
        assert CLASS_QUOTA_EXHAUSTED in CREDENTIAL_CLASSES
        assert CLASS_NOT_CONFIGURED in CREDENTIAL_CLASSES


class TestPresenceIsNotHealth:
    """The 演进项 made machine-visible: no level may be earned by presence."""

    def test_a_configured_key_never_probed_is_presence_only(self) -> None:
        report = build_report(
            "ths",
            required=True,
            configured=True,
            endpoint="fuyao.aicubes.cn",
            observations=(),
        )
        assert report.level == LEVEL_PRESENCE_ONLY
        assert report.classes == ()
        assert set(UNVERIFIED_WITHOUT_ACTIVE_CHECK) <= set(report.unverified)
        assert "key-expiry" in report.unverified

    def test_presence_only_never_counts_as_something_to_act_on(self) -> None:
        # If presence ever graded as alert-worthy - or as healthy - the report
        # would claim a check no source here can run.
        report = build_report(
            "ths",
            required=True,
            configured=True,
            endpoint="fuyao.aicubes.cn",
            observations=(),
        )
        assert report.level not in {LEVEL_ALERT, LEVEL_WARN}
        assert report.owner is None

    def test_a_429_grades_the_budget_but_never_measures_what_is_left(self) -> None:
        # The quota half of AC-19 is the sentence most likely to be over-read:
        # a 429 says the budget ran out *once*, not that 0% remains.
        report = _report_for("fred", (CLASS_QUOTA_EXHAUSTED, CLASS_QUOTA_EXHAUSTED))
        assert report.level == CLASS_LEVELS[CLASS_QUOTA_EXHAUSTED]
        assert report.owner == "容量/采购"
        assert "2 次" in report.note
        assert "无余量主动源" in report.note
        assert "quota-left" in report.unverified

    def test_a_keyless_source_says_nothing_about_credentials(self) -> None:
        report = build_report(
            "akshare", required=False, configured=True, endpoint="n/a", observations=()
        )
        assert report.level == LEVEL_NOT_APPLICABLE
        assert report.unverified == ()
        assert report.owner is None

    def test_a_missing_required_key_is_a_deployment_alert(self) -> None:
        report = build_report(
            "ths",
            required=True,
            configured=False,
            endpoint="fuyao.aicubes.cn",
            observations=(),
        )
        assert report.level == LEVEL_ALERT
        assert report.classes == ((CLASS_NOT_CONFIGURED, 1),)
        assert report.owner == CLASS_OWNERS[CLASS_NOT_CONFIGURED]


class TestGradingCombinesObservations:
    """One leg can fail several ways; the plane reports the worst and counts all."""

    def test_the_worst_class_sets_level_and_owner(self) -> None:
        report = _report_for(
            "ths",
            (
                CLASS_TRANSIENT_BLIP,
                CLASS_CREDENTIAL_REJECTED,
                CLASS_TRANSIENT_BLIP,
            ),
        )
        assert report.level == LEVEL_ALERT
        assert report.owner == CLASS_OWNERS[CLASS_CREDENTIAL_REJECTED]
        assert dict(report.classes)[CLASS_CREDENTIAL_REJECTED] == 1
        assert dict(report.classes)[CLASS_TRANSIENT_BLIP] == 2

    def test_attributions_are_deduplicated_and_bounded(self) -> None:
        observations = tuple(
            FailureObservation("ths", CLASS_UNCLASSIFIED, f"shape-{index}")
            if index % 2
            else FailureObservation("ths", CLASS_UNCLASSIFIED, "same-shape")
            for index in range(9)
        )
        report = build_report(
            "ths",
            required=True,
            configured=True,
            endpoint="fuyao.aicubes.cn",
            observations=observations,
        )
        assert "same-shape" in report.attributions
        assert len(report.attributions) == 3

    def test_another_source_s_failure_does_not_dirty_this_report(self) -> None:
        report = build_report(
            "fred",
            required=True,
            configured=True,
            endpoint="api.stlouisfed.org",
            observations=(FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "FUYAO_HTTP_401"),),
        )
        assert report.level == LEVEL_PRESENCE_ONLY


class TestPayloadCarriesNoCredential:
    """The hard constraint: metadata in, secrets out, on every rendered plane."""

    def test_a_leaky_message_really_does_carry_the_query(self) -> None:
        # The premise of the rest of this class: this is where a credential
        # would come from, so a plane that renders raw exception text would
        # print it. Asserting the danger exists keeps the guards from being
        # assertions about nothing.
        error = _httpx_status_error(429, "Too Many Requests")
        assert CANARY in str(error)
        assert CANARY not in redact(str(error))
        assert "api_key" not in redact(str(error))

    def test_redact_keeps_the_origin_it_strips_the_query(self) -> None:
        assert redact(f"GET {LEAKY_URL} -> 429") == f"GET {LEAKY_ORIGIN} -> 429"

    def test_a_key_quoted_into_prose_needs_the_value_pass_not_the_url_pass(self) -> None:
        # A header is not a URL, so the query rule cannot see it. This is the
        # shape :meth:`key_values` exists for: scrub against what we actually
        # hold instead of guessing which text shape carries a credential.
        text = f"Authorization: Bearer {CANARY}"
        assert CANARY in redact(text)
        assert redact(text, secrets=(CANARY,)) == f"Authorization: Bearer {REDACTED_MARKER}"

    def test_an_unconfigured_key_is_not_a_scrub_pattern(self) -> None:
        # An empty value would otherwise replace between every character of
        # every message, turning the report into noise that hides real detail.
        assert redact("boom", secrets=("", CANARY)) == "boom"

    def test_the_value_pass_runs_after_the_url_pass(self) -> None:
        # One message, two credentials in it: the query-borne one is dropped by
        # origin reduction, the header-borne one by the value pass. Neither
        # rule is allowed to leave the other's shape behind.
        text = f"GET {LEAKY_URL} with Authorization: Bearer {CANARY}"
        assert redact(text, secrets=(CANARY,)) == (
            f"GET {LEAKY_ORIGIN} with Authorization: Bearer {REDACTED_MARKER}"
        )
        assert CANARY not in redact(text, secrets=(CANARY,))

    def test_attribution_of_a_leaky_exception_hides_the_query(self) -> None:
        assert attribution_of(_httpx_status_error(429, "Too Many Requests")) == (
            f"status=429 {LEAKY_ORIGIN}"
        )

    def test_fred_attribution_keeps_code_and_status_but_not_the_key(self) -> None:
        assert attribution_of(_fred(429, LEAKY_URL)) == f"FRED_HTTP_ERROR status=429 {LEAKY_ORIGIN}"

    def test_attribution_of_a_bare_failure_names_only_its_type(self) -> None:
        assert attribution_of(TimeoutError()) == "TimeoutError"

    @pytest.mark.parametrize(
        "url",
        ["https://[::1", "http://"],
        ids=["unparseable-ipv6", "no-host"],
    )
    def test_an_unparseable_url_is_dropped_instead_of_passed_through(self, url: str) -> None:
        # Origin reduction is the only thing standing between an attribution
        # and whatever a URL carried; a value it cannot parse must yield
        # nothing rather than the raw tail it failed on.
        exc = EcbProviderError("ECB_HTTP_ERROR", status=503, url=url)
        assert attribution_of(exc) == "ECB_HTTP_ERROR status=503"

    def test_no_class_of_failure_renders_a_credential(self) -> None:
        # Every class, both Key-bearing sources, and a settings object whose Key
        # values are the canary: whichever way a value could be reached - the
        # configured flag, the endpoint table, a provider code, an HTTP status,
        # a URL in a message - it has to come back absent.
        errors = [
            FredProviderError("FRED_API_KEY_MISSING"),
            _fred(401, LEAKY_URL),
            _fred(429, LEAKY_URL),
            _fred(503, LEAKY_URL),
            _fred(404, LEAKY_URL),
            _fred(400, LEAKY_URL),
            _httpx_status_error(401, "Unauthorized"),
            _httpx_status_error(429, "Too Many Requests"),
            error_for_upstream_code(2001),
            error_for_transport("http", detail="403"),
            error_for_transport("rate_limited", detail="429"),
            ThsProviderError(f"THS_SYMBOL_INVALID: {CANARY}"),
            HTTPError(f"502 Server Error: Bad Gateway for url: {LEAKY_URL}"),
            TimeoutError(),
        ]
        legs = [
            _Leg(source, classify_failure(error), attribution_of(error))
            for error in errors
            for source in ("ths", "fred")
        ]
        rendered = json.dumps(_rendered(credential_health(settings=_Settings(), results=legs)))
        assert CANARY not in rendered
        assert "api_key" not in rendered
        assert len(legs) == 28

    def test_the_redaction_guard_is_in_attribution_not_in_the_payload(self) -> None:
        # Control for the test above: the plane renders what the patrol handed
        # it, so the canary reappears if the raw message is passed as an
        # attribution. The guard lives in ``attribution_of``/``redact`` - the one
        # place failure text is produced - not as a filter bolted on at the end.
        legs = [_Leg("ths", CLASS_CREDENTIAL_REJECTED, LEAKY_URL)]
        rendered = json.dumps(_rendered(credential_health(settings=_Settings(), results=legs)))
        assert CANARY in rendered
        assert CANARY not in attribution_of(_httpx_status_error(401, "Unauthorized"))

    def test_the_grades_reach_the_payload_without_the_key(self) -> None:
        payload = credential_health(
            settings=_Settings(),
            results=[_Leg("ths", CLASS_CREDENTIAL_REJECTED, "FUYAO_HTTP_401")],
        )
        assert payload["ths"].level == LEVEL_ALERT
        assert payload["ths"].owner == CLASS_OWNERS[CLASS_CREDENTIAL_REJECTED]
        assert payload["fred"].level == LEVEL_ALERT
        assert payload["fred"].classes[0][0] == CLASS_NOT_CONFIGURED
        assert payload["akshare"].level == LEVEL_NOT_APPLICABLE
        assert CANARY not in json.dumps(_rendered(payload), ensure_ascii=False)

    def test_presence_only_keeps_the_expiry_and_quota_boundary_in_the_note(self) -> None:
        # The AC-19 演进项 has to be readable from the payload itself, or a
        # green-ish line invites the reader to assume the Key was checked.
        note = credential_health(settings=_Settings())["ths"].note
        assert "到期" in note
        assert "429" in note

    def test_issuer_expiry_metadata_sets_severity_without_claiming_a_probe(self) -> None:
        base = {
            "required": True,
            "configured": True,
            "endpoint": "fuyao.aicubes.cn",
            "observations": (),
            "as_of": date(2026, 9, 30),
        }
        expired = build_report("ths", **base, expires_at=date(2026, 9, 29))
        soon = build_report("ths", **base, expires_at=date(2026, 10, 3))

        assert expired.level == LEVEL_ALERT
        assert expired.expiry_state == "expired"
        assert expired.owner == "凭据负责人"
        assert "2026-09-29" in expired.note
        assert "有效性" in expired.note
        assert soon.level == LEVEL_WARN
        assert soon.expiry_state == "expiring-soon"
        assert "未提供到期日" not in soon.note

    def test_expiry_does_not_elevate_unconfigured_or_optional_sources(self) -> None:
        expired = date(2026, 9, 29)
        unconfigured = build_report(
            "ths",
            required=True,
            configured=False,
            endpoint="fuyao.aicubes.cn",
            observations=(),
            expires_at=expired,
            as_of=date(2026, 9, 30),
        )
        optional = build_report(
            "akshare",
            required=False,
            configured=True,
            endpoint="n/a",
            observations=(),
            expires_at=expired,
            as_of=date(2026, 9, 30),
        )

        assert unconfigured.level == LEVEL_ALERT
        assert unconfigured.classes[0][0] == CLASS_NOT_CONFIGURED
        assert unconfigured.expiry_state == "unknown"
        assert optional.level == LEVEL_NOT_APPLICABLE
        assert optional.expiry_state == "unknown"
        assert optional.expires_at is None


def _rendered(reports: dict[str, KeyReport]) -> dict[str, dict[str, object]]:
    """Render reports the way the API endpoints do."""
    return {source: report.as_dict() for source, report in reports.items()}


def _report_for(source: str, classes: tuple[str, ...]) -> KeyReport:
    """Grade one configured source from the classes it was seen failing with."""
    return build_report(
        source,
        required=True,
        configured=True,
        endpoint="fuyao.aicubes.cn",
        observations=tuple(FailureObservation(source, name, name) for name in classes),
    )
