"""Unit tests for cross-check alert governance and delivery (AC-9, C48).

``AlertPolicy`` itself is covered in :mod:`tests.test_alert_matrix`; what
lives here is the half that did not exist before this round: the governance
file the whitelist comes from, the *process-scoped* policy that makes dedupe
and the rate-spike baseline survive from one scheduled run to the next, and
the dispatcher that has to report what each channel did - including the times
it did nothing, and say why.

The shipped ``diff_governance.json`` is read for real (it is this
deployment's state); a test that needs a whitelist entry points
:data:`diff_alerts.GOVERNANCE_PATH` at a temporary file instead.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from opendata.core.config import settings
from opendata.pipeline import diff_alerts, subscription
from opendata.pipeline.alerts import AlertPolicy
from opendata.pipeline.cross_check import DiffSummary, FieldDiff, Verdict
from opendata.pipeline.diff_alerts import (
    Delivery,
    DiffAlertDispatcher,
    Governance,
    default_notifier,
    governance,
    load_governance,
    render_diff_email,
    reset_policy,
    shared_policy,
    smtp_mail_sender,
)
from opendata.pipeline.runner import Window

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Only used for the batch id here; the window maths is tested in templates.
BATCH_WINDOW = Window(start=date(2026, 9, 24), end=date(2026, 9, 24))


def make_summary(
    *,
    deviations: int = 1,
    missing: int = 0,
    keys: int = 100,
    per_field: dict[str, int] | None = None,
    samples: list[FieldDiff] | None = None,
) -> DiffSummary:
    """Build a comparison summary carrying everything the delivery reads."""
    return DiffSummary(
        domain="stock_daily",
        source_a="ths",
        source_b="akshare",
        checked_at=datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc),
        batch_id=f"xcheck:stock_daily:{BATCH_WINDOW.label()}",
        compared_keys=keys,
        deviation_count=deviations,
        missing_count=missing,
        per_field=per_field if per_field is not None else {"close": deviations},
        samples=samples or [],
    )


class Channels:
    """Recording stand-ins for the three delivery sinks."""

    def __init__(
        self,
        *,
        hub_delivered: int = 3,
        ws_error: Exception | None = None,
        hub_error: Exception | None = None,
        mail_error: Exception | None = None,
    ) -> None:
        self.ws_messages: list[dict[str, Any]] = []
        self.hub_messages: list[dict[str, Any]] = []
        self.mail: list[tuple[str, str, str]] = []
        self._hub_delivered = hub_delivered
        self._ws_error = ws_error
        self._hub_error = hub_error
        self._mail_error = mail_error

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.ws_messages.append(message)
        if self._ws_error is not None:
            raise self._ws_error

    async def publish(self, message: dict[str, Any]) -> int:
        self.hub_messages.append(message)
        if self._hub_error is not None:
            raise self._hub_error
        return self._hub_delivered

    async def send_mail(self, to_addr: str, subject: str, body: str) -> None:
        self.mail.append((to_addr, subject, body))
        if self._mail_error is not None:
            raise self._mail_error

    @property
    def hub(self) -> object:
        """Something shaped like the subscription hub."""
        return SimpleNamespace(publish_diff_alert=self.publish)

    @property
    def touched(self) -> bool:
        """Whether any sink saw a call."""
        return bool(self.ws_messages or self.hub_messages or self.mail)


def dispatcher_for(channels: Channels, *, recipients: tuple[str, ...] = ()) -> DiffAlertDispatcher:
    """A dispatcher wired to the recording channels."""
    return DiffAlertDispatcher(
        broadcast=channels.broadcast,
        hub=channels.hub,
        mail_send=channels.send_mail,
        recipients=recipients,
    )


@pytest.fixture
def fresh_policy():
    """Keep the process-scoped policy from leaking between tests."""
    reset_policy()
    yield
    reset_policy()


@pytest.fixture
def governance_file(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    """Write a governance document to the test's own directory and point at it."""

    def write(raw: dict[str, Any]) -> Path:
        path = tmp_path / "diff_governance.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    return write


class TestLoadGovernance:
    def test_an_absent_file_is_an_empty_state_not_a_failure(self, tmp_path: Path) -> None:
        assert load_governance(tmp_path / "nope.json") == Governance()

    def test_the_shipped_file_is_a_valid_state(self) -> None:
        state = load_governance()

        assert isinstance(state.whitelist, frozenset)
        assert isinstance(state.recipients, tuple)
        # A tolerance registered without a basis is how a whitelist outlives
        # the defect it excused; the shipped file holds nothing like that.
        for pair in state.whitelist:
            assert state.reason_for(pair)

    def test_a_written_state_round_trips(self, governance_file: Callable[..., Path]) -> None:
        path = governance_file(
            {
                "known_differences": [
                    {"domain": "stock_daily", "field": "volume", "reason": "单位不同"}
                ],
                "email_recipients": ["ops@example.com", "dq@example.com"],
            }
        )

        state = load_governance(path)

        assert state.whitelist == frozenset({("stock_daily", "volume")})
        assert state.recipients == ("ops@example.com", "dq@example.com")
        assert state.reason_for(("stock_daily", "volume")) == "单位不同"
        assert state.reason_for(("stock_daily", "amount")) == ""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("{not json", "not valid JSON"),
            ("[]", "must hold a JSON object"),
            (json.dumps({"known_differences": {"stock_daily": 1}}), "must be a list"),
            (
                json.dumps({"known_differences": [{"domain": "d", "field": "f"}]}),
                "domain/field/reason",
            ),
            (json.dumps({"email_recipients": "ops@example.com"}), "list of strings"),
            (json.dumps({"email_recipients": [1]}), "list of strings"),
        ],
        ids=["malformed", "root-not-object", "list-not-list", "no-reason", "str", "non-str"],
    )
    def test_a_broken_file_fails_closed(self, tmp_path: Path, payload: str, expected: str) -> None:
        """Ignoring the file silently would re-alert everything and hide why."""
        path = tmp_path / "diff_governance.json"
        path.write_text(payload, encoding="utf-8")

        with pytest.raises(RuntimeError, match=expected):
            load_governance(path)


class TestSharedPolicy:
    """The clause C48 earns: the state behind dedupe and leveling is shared."""

    def test_every_caller_gets_the_same_instance(self, fresh_policy: None) -> None:
        assert shared_policy() is shared_policy()

    def test_its_whitelist_comes_from_the_governance_file(
        self,
        fresh_policy: None,
        monkeypatch: pytest.MonkeyPatch,
        governance_file: Callable[..., Path],
    ) -> None:
        monkeypatch.setattr(
            diff_alerts,
            "GOVERNANCE_PATH",
            governance_file(
                {
                    "known_differences": [
                        {"domain": "stock_daily", "field": "volume", "reason": "单位不同"}
                    ]
                }
            ),
        )

        assert shared_policy().whitelist == {("stock_daily", "volume")}

    def test_dedupe_survives_a_second_scheduled_run(self, fresh_policy: None) -> None:
        """The second batch's identical difference is quiet *because* of the shared set."""
        summary = make_summary(deviations=3)

        first = shared_policy().decide(summary)
        second = shared_policy().decide(summary)

        assert (first.alert, second.alert) == (True, False)
        assert second.reason == "this difference pattern was already alerted"

    def test_refresh_picks_up_an_edited_whitelist(
        self,
        fresh_policy: None,
        monkeypatch: pytest.MonkeyPatch,
        governance_file: Callable[..., Path],
    ) -> None:
        path = governance_file({"known_differences": []})
        monkeypatch.setattr(diff_alerts, "GOVERNANCE_PATH", path)
        policy = shared_policy()
        assert policy.whitelist == set()

        path.write_text(
            json.dumps(
                {
                    "known_differences": [
                        {"domain": "stock_daily", "field": "close", "reason": "复权口径"}
                    ]
                }
            ),
            encoding="utf-8",
        )

        assert shared_policy(refresh=True) is policy
        assert policy.whitelist == {("stock_daily", "close")}
        # Only the whitelisted field differs and no row is missing: tolerated.
        assert not policy.decide(make_summary()).alert

    def test_the_rate_baseline_carries_across_comparisons(self, fresh_policy: None) -> None:
        policy = shared_policy()

        steady = policy.decide(make_summary(deviations=1))
        spike = policy.decide(make_summary(deviations=4, per_field={"volume": 4}))

        assert (steady.level, steady.alert) == ("warning", True)
        assert (spike.level, spike.alert) == ("critical", True)

    def test_reset_gives_the_next_process_a_clean_dedupe_set(self, fresh_policy: None) -> None:
        summary = make_summary()

        assert shared_policy().decide(summary).alert
        assert not shared_policy().decide(summary).alert

        reset_policy()

        assert shared_policy().decide(summary).alert


class TestSuppression:
    """Governance is load-bearing only if a suppressed difference delivers nothing."""

    async def test_a_whitelisted_difference_reaches_no_channel(self) -> None:
        channels = Channels()
        dispatcher = dispatcher_for(channels, recipients=("ops@example.com",))
        summary = make_summary()
        decision = AlertPolicy(whitelist={("stock_daily", "close")}).decide(summary)

        delivery = await dispatcher.notify(summary, decision)

        assert decision.alert is False
        assert channels.touched is False
        assert delivery.alerted is False
        assert (delivery.ws_sent, delivery.hub_delivered, delivery.mail_sent) == (False, 0, 0)
        # Nothing was skipped: nothing was supposed to go out.
        assert (delivery.ws_error, delivery.hub_error, delivery.mail_skipped) == (None, None, None)
        assert delivery.reason == "differences are whitelisted for ['close']"

    async def test_a_consistent_comparison_is_recorded_without_alerting(self) -> None:
        channels = Channels()
        dispatcher = dispatcher_for(channels)
        summary = make_summary(deviations=0, per_field={})

        delivery = await dispatcher.notify(summary, AlertPolicy().decide(summary))

        assert channels.touched is False
        assert (delivery.alerted, delivery.reason) == (False, "sources are consistent")
        assert dispatcher.deliveries == [delivery]

    async def test_every_notify_is_recorded_even_the_quiet_ones(self) -> None:
        dispatcher = DiffAlertDispatcher()
        policy = AlertPolicy()
        summary = make_summary()

        first = await dispatcher.notify(summary, policy.decide(summary))
        second = await dispatcher.notify(summary, policy.decide(summary))

        assert dispatcher.deliveries == [first, second]
        assert [entry.alerted for entry in dispatcher.deliveries] == [True, False]


class TestChannels:
    async def test_an_alerting_decision_reaches_all_three_sinks(self) -> None:
        channels = Channels(hub_delivered=2)
        dispatcher = dispatcher_for(channels, recipients=("ops@example.com", "dq@example.com"))
        summary = make_summary(deviations=2, missing=1)

        delivery = await dispatcher.notify(summary, AlertPolicy().decide(summary))

        assert delivery.as_dict() == {
            "batch_id": summary.batch_id,
            "alerted": True,
            "level": "warning",
            "reason": "2 deviation(s) and 1 missing row(s) over 100 keys (rate 0.0300)",
            "ws_sent": True,
            "ws_error": None,
            "hub_delivered": 2,
            "hub_error": None,
            "mail_sent": 2,
            "mail_errors": [],
            "mail_skipped": None,
        }
        assert [to for to, _, _ in channels.mail] == ["ops@example.com", "dq@example.com"]

    async def test_the_ws_payload_carries_the_decision_not_just_the_numbers(self) -> None:
        channels = Channels()
        summary = make_summary(deviations=50)
        decision = AlertPolicy(whitelist={("stock_daily", "amount")}).decide(summary)

        await dispatcher_for(channels).notify(summary, decision)

        message = channels.ws_messages[0]
        assert message["type"] == "data.diff_alert"
        assert message["batch_id"] == summary.batch_id
        assert (message["level"], message["reason"]) == (decision.level, decision.reason)
        assert (message["mismatches"], message["compared"]) == (50, 100)
        assert channels.hub_messages == [message]

    async def test_an_unwired_channel_is_a_reported_reason_not_a_silent_pass(self) -> None:
        summary = make_summary()

        delivery = await DiffAlertDispatcher().notify(summary, AlertPolicy().decide(summary))

        assert delivery.alerted is True
        assert (delivery.ws_sent, delivery.ws_error) == (False, "no WS broadcast channel wired")
        assert (delivery.hub_delivered, delivery.hub_error) == (0, None)
        assert (delivery.mail_sent, delivery.mail_skipped) == (0, "SMTP not configured (no sender)")

    async def test_a_configured_sender_with_no_recipients_says_so(self) -> None:
        channels = Channels()
        summary = make_summary()

        delivery = await dispatcher_for(channels).notify(summary, AlertPolicy().decide(summary))

        assert (delivery.mail_sent, delivery.mail_skipped) == (
            0,
            "no diff-alert recipients configured",
        )
        assert channels.mail == []

    async def test_a_channel_that_raises_is_recorded_and_does_not_stop_the_others(self) -> None:
        channels = Channels(ws_error=RuntimeError("client gone"), mail_error=OSError("smtp down"))
        summary = make_summary()

        delivery = await dispatcher_for(channels, recipients=("ops@example.com",)).notify(
            summary, AlertPolicy().decide(summary)
        )

        assert (delivery.ws_sent, delivery.ws_error) == (False, "RuntimeError: client gone")
        assert (delivery.hub_delivered, delivery.hub_error) == (3, None)
        assert (delivery.mail_sent, delivery.mail_errors) == (0, ("ops@example.com: OSError",))
        # The alert still counts: one dead channel is not a lost alert.
        assert delivery.alerted is True

    async def test_partial_mail_failure_reports_both_addresses(self) -> None:
        attempts: list[str] = []

        async def flaky(to_addr: str, subject: str, body: str) -> None:
            attempts.append(to_addr)
            if to_addr == "bad@example.com":
                raise ConnectionError("refused")

        dispatcher = DiffAlertDispatcher(
            mail_send=flaky, recipients=("bad@example.com", "ok@example.com")
        )
        summary = make_summary()

        delivery = await dispatcher.notify(summary, AlertPolicy().decide(summary))

        assert attempts == ["bad@example.com", "ok@example.com"]
        assert (delivery.mail_sent, delivery.mail_errors, delivery.mail_skipped) == (
            1,
            ("bad@example.com: ConnectionError",),
            None,
        )

    async def test_a_hub_without_the_diff_method_is_reported(self) -> None:
        channels = Channels()
        dispatcher = DiffAlertDispatcher(broadcast=channels.broadcast, hub=SimpleNamespace())
        summary = make_summary()

        delivery = await dispatcher.notify(summary, AlertPolicy().decide(summary))

        assert (delivery.hub_delivered, delivery.hub_error) == (
            0,
            "hub exposes no publish_diff_alert",
        )
        assert delivery.ws_sent is True


class TestRenderDiffEmail:
    def test_the_body_answers_what_differed_and_why_we_are_telling(self) -> None:
        samples = [
            FieldDiff(("600519", date(2026, 9, 24)), field, 1.0, 2.0, 0.5, Verdict.DEVIATION)
            for field in ("close", "open")
        ]
        summary = make_summary(deviations=2, per_field={"close": 1, "open": 1}, samples=samples)
        decision = AlertPolicy().decide(summary)

        subject, body = render_diff_email(summary, decision)

        assert subject == "[opendata/warning] stock_daily 双源差异 2.00%"
        assert "域: stock_daily" in body
        assert "源对: ths vs akshare" in body
        assert f"批次: {summary.batch_id}" in body
        assert "对比键数: 100" in body
        assert "值差异行: 2  缺行: 0  差异率: 0.0200" in body
        assert f"级别: {decision.level}  原因: {decision.reason}" in body
        assert "字段分布: {'close': 1, 'open': 1}" in body
        assert "600519|2026-09-24 close: 1.0 != 2.0" in body
        assert "校对时间: 2026-09-24T02:00:00+00:00" in body

    def test_the_sample_block_is_capped_at_five_rows(self) -> None:
        samples = [
            FieldDiff(("600519", date(2026, 9, 24)), f"f{index}", 1.0, 2.0, None, Verdict.DEVIATION)
            for index in range(9)
        ]
        summary = make_summary(deviations=9, per_field={"f0": 9}, samples=samples)

        _, body = render_diff_email(summary, AlertPolicy().decide(summary))

        assert body.count(" != ") == 5
        assert "样本（最多 5 条）" in body

    def test_a_missing_row_sample_renders_the_absent_side(self) -> None:
        samples = [FieldDiff(("000001", date(2026, 9, 24)), "*", None, None, None, Verdict.MISSING)]
        summary = make_summary(deviations=0, missing=1, per_field={"*": 1}, samples=samples)

        _, body = render_diff_email(summary, AlertPolicy().decide(summary))

        assert "000001|2026-09-24 *: None != None" in body
        assert "源对: ths vs akshare" in body

    def test_a_consistent_summary_renders_no_sample_block(self) -> None:
        summary = make_summary(deviations=0, per_field={})

        subject, body = render_diff_email(summary, AlertPolicy().decide(summary))

        assert subject.startswith("[opendata/warning] stock_daily 双源差异 0.00%")
        assert "样本" not in body


class TestMailSenderWiring:
    def test_no_smtp_settings_means_no_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "smtp_host", None)

        assert smtp_mail_sender() is None

    def test_a_host_without_a_user_is_still_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_user", None)

        assert smtp_mail_sender() is None

    def test_both_settings_wire_a_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_user", "bot@example.com")

        assert smtp_mail_sender() is not None

    async def test_the_sender_hands_the_message_to_the_shared_smtp_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One mailbox path: the A1 notification service owns SMTP, this only feeds it."""
        sent: list[dict[str, Any]] = []

        def fake_smtp_send(**kwargs: Any) -> None:
            sent.append(kwargs)

        import opendata.services.notification_service as notification_service

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_user", "bot@example.com")
        monkeypatch.setattr(notification_service, "_smtp_send", fake_smtp_send)

        await smtp_mail_sender()("ops@example.com", "subject", "body")

        assert len(sent) == 1
        assert sent[0]["host"] == "smtp.example.com"
        assert sent[0]["user"] == "bot@example.com"
        assert sent[0]["msg"]["To"] == "ops@example.com"
        assert sent[0]["msg"]["Subject"] == "subject"
        assert sent[0]["msg"].get_payload()[-1].get_payload(decode=True).decode() == "body"


class TestProductionWiring:
    async def test_channels_are_resolved_on_first_alert_and_reused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Building a pipeline must not import the API layer; alerting may."""
        built: list[int] = []
        channels = Channels()
        dispatcher = DiffAlertDispatcher(broadcast=channels.broadcast)

        def fake_production_dispatcher(*, refresh: bool = False) -> DiffAlertDispatcher:
            built.append(1)
            return dispatcher

        monkeypatch.setattr(diff_alerts, "production_dispatcher", fake_production_dispatcher)
        notify = default_notifier()

        assert built == []

        summary = make_summary()
        first = await notify(summary, AlertPolicy().decide(summary))
        second = await notify(summary, AlertPolicy().decide(summary))

        assert built == [1]
        assert isinstance(first, Delivery)
        assert (first.alerted, second.alerted) == (True, True)
        assert channels.ws_messages and len(channels.ws_messages) == 2

    async def test_the_real_dispatcher_reaches_this_deployments_channels(self) -> None:
        dispatcher = diff_alerts.production_dispatcher()

        assert dispatcher.broadcast is not None
        assert dispatcher.hub is subscription.hub
        assert dispatcher.recipients == governance().recipients

    def test_a_deployment_without_a_hub_still_reports_its_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hub is optional; its absence is reported, not swallowed."""
        monkeypatch.setattr(subscription, "hub", None)

        assert diff_alerts.production_dispatcher().hub is None
