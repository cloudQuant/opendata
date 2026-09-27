"""Cross-check alert governance and dual-channel delivery (AC-9, design §8.2).

``alerts.AlertPolicy`` could already decide *whether* to alert, and
``subscription.diff_alert_message`` could already render the WS payload -
what did not exist was the production side of either. Measured in
``docs/evidence/C48``: the only production ``CrossCheckService`` is built in
``templates.build_stock_daily_pipeline`` without ``notifier=`` or ``policy=``,
so every decision was computed and dropped, and a policy constructed per
comparison starts with an empty ``alerted`` set, which makes
"相同差异不重复告警" true only in the sense that nothing ever repeats within a
single call.

Three pieces, one per clause of the criterion:

* :func:`shared_policy` - one process-scoped policy plus the whitelist read
  from ``diff_governance.json``, handed to every service, so the dedupe set
  and the rate-spike baseline survive from one scheduled run to the next.
  A process restart resets them; persisting across processes needs a control
  database table, which is deliberately not invented here.
* :class:`DiffAlertDispatcher` - the delivery half. It takes the pair
  ``(summary, decision)``: the decision gates it (a suppressed difference
  reaches neither channel, which is what makes governance load-bearing) and
  the summary carries the payload. Each channel's outcome is recorded,
  including failures, so "no subscriber was listening" and "the channel
  raised" stay different readings.
* :func:`smtp_mail_sender` - the second channel, best-effort by nature and
  explicit about it: no SMTP configured is a reported ``mail_sent=0`` with a
  reason, never a silent pass.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from opendata.pipeline.alerts import AlertPolicy
from opendata.pipeline.subscription import diff_alert_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from opendata.pipeline.alerts import AlertDecision
    from opendata.pipeline.cross_check import DiffSummary

    #: App-wide WS sink (``ws_manager.broadcast``).
    Broadcast = Callable[[dict[str, Any]], Awaitable[object]]
    #: ``(to, subject, body)`` mail sink; returns nothing meaningful.
    MailSend = Callable[[str, str, str], Awaitable[object]]

#: Governance state file (lives beside the code that reads it).
GOVERNANCE_PATH = Path(__file__).resolve().parent / "diff_governance.json"


@dataclass(frozen=True)
class Governance:
    """The configured alert-governance state.

    Attributes:
        whitelist: ``(domain, field)`` pairs treated as known differences.
        recipients: Mail addresses for diff alerts.
        reasons: Why each whitelisted pair is tolerated (for the audit).
    """

    whitelist: frozenset[tuple[str, str]] = frozenset()
    recipients: tuple[str, ...] = ()
    reasons: tuple[tuple[tuple[str, str], str], ...] = ()

    def reason_for(self, pair: tuple[str, str]) -> str:
        """Return the recorded basis for one tolerated pair."""
        return dict(self.reasons).get(pair, "")


def load_governance(path: Path | None = None) -> Governance:
    """Read ``diff_governance.json``.

    Args:
        path: Override for tests; None uses :data:`GOVERNANCE_PATH`.

    Returns:
        The configured whitelist and recipients.

    Raises:
        RuntimeError: If the file exists but is unreadable or malformed. An
            absent file is *not* an error: no tolerated difference and no
            mail address is a legitimate state, silently ignoring a broken
            file is not (a whitelist that fails to load would re-alert
            everything, which is noise, not a hole).
    """
    target = GOVERNANCE_PATH if path is None else path
    if not target.is_file():
        return Governance()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"{target} must hold a JSON object")
    entries = raw.get("known_differences", [])
    if not isinstance(entries, list):
        raise RuntimeError(f"{target}: `known_differences` must be a list")
    whitelist: list[tuple[str, str]] = []
    reasons: list[tuple[tuple[str, str], str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not {"domain", "field", "reason"} <= set(entry):
            raise RuntimeError(
                f"{target}: every known difference needs domain/field/reason; {entry!r} has less. "
                "An entry without a reason is how a tolerance outlives the defect it excused."
            )
        pair = (str(entry["domain"]), str(entry["field"]))
        whitelist.append(pair)
        reasons.append((pair, str(entry["reason"])))
    recipients = raw.get("email_recipients", [])
    if not isinstance(recipients, list) or not all(isinstance(r, str) for r in recipients):
        raise RuntimeError(f"{target}: `email_recipients` must be a list of strings")
    return Governance(
        whitelist=frozenset(whitelist), recipients=tuple(recipients), reasons=tuple(reasons)
    )


@lru_cache(maxsize=1)
def _cached_governance() -> Governance:
    return load_governance()


def governance(*, refresh: bool = False) -> Governance:
    """Return the governance state (cached; ``refresh`` re-reads the file)."""
    if refresh:
        _cached_governance.cache_clear()
    return _cached_governance()


_POLICY: AlertPolicy | None = None


def shared_policy(*, refresh: bool = False) -> AlertPolicy:
    """Return the process-scoped alert policy used by production wiring.

    The point is the lifetime: dedupe and the diff-rate baseline are only
    meaningful across comparisons, and a per-construction policy makes
    "相同差异不重复告警" and "差异率突增才升级" unmeasurable.

    Args:
        refresh: Re-read the whitelist from disk (tests, or after editing
            the governance file).

    Returns:
        The same ``AlertPolicy`` instance on every call within the process.
    """
    global _POLICY
    if _POLICY is None:
        _POLICY = AlertPolicy(whitelist=set(governance().whitelist))
    elif refresh:
        _POLICY.whitelist = set(governance(refresh=True).whitelist)
    return _POLICY


def reset_policy() -> None:
    """Forget the shared policy (tests only)."""
    global _POLICY
    _POLICY = None
    _cached_governance.cache_clear()


@dataclass(frozen=True)
class Delivery:
    """What one decision did to each channel.

    Attributes:
        batch_id: Cross-check batch the decision belongs to.
        alerted: Whether the governance decision let the alert through.
        level: Alert level of the decision.
        reason: The policy's stated reason, verbatim.
        ws_sent: The app-wide WS broadcast accepted the message.
        ws_error: Failure the WS broadcast raised, if any.
        hub_delivered: Subscribers the domain-filtered hub reached.
        hub_error: Failure the hub raised, if any.
        mail_sent: Addresses the mail channel accepted.
        mail_errors: Per-address failures.
        mail_skipped: Why no mail went out although the alert fired.
    """

    batch_id: str
    alerted: bool
    level: str
    reason: str
    ws_sent: bool = False
    ws_error: str | None = None
    hub_delivered: int = 0
    hub_error: str | None = None
    mail_sent: int = 0
    mail_errors: tuple[str, ...] = ()
    mail_skipped: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the delivery as JSON-friendly job output."""
        return {
            "batch_id": self.batch_id,
            "alerted": self.alerted,
            "level": self.level,
            "reason": self.reason,
            "ws_sent": self.ws_sent,
            "ws_error": self.ws_error,
            "hub_delivered": self.hub_delivered,
            "hub_error": self.hub_error,
            "mail_sent": self.mail_sent,
            "mail_errors": list(self.mail_errors),
            "mail_skipped": self.mail_skipped,
        }


@dataclass
class DiffAlertDispatcher:
    """Deliver cross-check decisions on both channels and record the outcome.

    Attributes:
        broadcast: App-wide WS sink; None disables that channel (recorded,
            not silently skipped).
        hub: The ``SubscriptionHub`` whose ``publish_diff_alert`` filters by
            the subscriber's domains; None disables it.
        mail_send: ``(to, subject, body)`` sender; None disables mail.
        recipients: Mail addresses.
        deliveries: One entry per ``notify`` call (the job reports it).
    """

    broadcast: Broadcast | None = None
    hub: object | None = None
    mail_send: MailSend | None = None
    recipients: Sequence[str] = ()
    deliveries: list[Delivery] = field(default_factory=list)

    async def notify(self, summary: DiffSummary, decision: AlertDecision) -> Delivery:
        """Hand one comparison's decision to the channels it should reach.

        Args:
            summary: The comparison the decision was made about.
            decision: The governance verdict (level, reason, dedupe state).

        Returns:
            What each channel did.
        """
        message = diff_alert_message(
            domain=summary.domain,
            source_a=summary.source_a,
            source_b=summary.source_b,
            batch_id=summary.batch_id,
            mismatch_ratio=summary.diff_rate,
            mismatches=summary.deviation_count + summary.missing_count,
            compared=summary.compared_keys,
            level=decision.level,
            reason=decision.reason,
        )
        if not decision.alert:
            delivery = Delivery(
                batch_id=summary.batch_id,
                alerted=False,
                level=decision.level,
                reason=decision.reason,
            )
            self.deliveries.append(delivery)
            return delivery
        ws_sent, ws_error = await self._publish_ws(message)
        hub_delivered, hub_error = await self._publish_hub(message)
        mail_sent, mail_errors, mail_skipped = await self._send_mail(summary, decision)
        delivery = Delivery(
            batch_id=summary.batch_id,
            alerted=True,
            level=decision.level,
            reason=decision.reason,
            ws_sent=ws_sent,
            ws_error=ws_error,
            hub_delivered=hub_delivered,
            hub_error=hub_error,
            mail_sent=mail_sent,
            mail_errors=mail_errors,
            mail_skipped=mail_skipped,
        )
        self.deliveries.append(delivery)
        return delivery

    async def _publish_ws(self, message: dict[str, Any]) -> tuple[bool, str | None]:
        """Broadcast on the app-wide WS channel."""
        if self.broadcast is None:
            return False, "no WS broadcast channel wired"
        try:
            await self.broadcast(message)
        except Exception as exc:
            logger.warning(f"diff alert WS broadcast failed: {type(exc).__name__}: {exc}")
            return False, f"{type(exc).__name__}: {exc}"
        return True, None

    async def _publish_hub(self, message: dict[str, Any]) -> tuple[int, str | None]:
        """Publish to the domain-filtered subscription hub."""
        publish = getattr(self.hub, "publish_diff_alert", None)
        if publish is None:
            return 0, None if self.hub is None else "hub exposes no publish_diff_alert"
        try:
            return int(await publish(message)), None
        except Exception as exc:
            logger.warning(f"diff alert hub publish failed: {type(exc).__name__}: {exc}")
            return 0, f"{type(exc).__name__}: {exc}"

    async def _send_mail(
        self, summary: DiffSummary, decision: AlertDecision
    ) -> tuple[int, tuple[str, ...], str | None]:
        """Mail every configured recipient; report why none was reached."""
        if self.mail_send is None:
            return 0, (), "SMTP not configured (no sender)"
        if not self.recipients:
            return 0, (), "no diff-alert recipients configured"
        subject, body = render_diff_email(summary, decision)
        errors: list[str] = []
        sent = 0
        for address in self.recipients:
            try:
                await self.mail_send(address, subject, body)
            except Exception as exc:
                logger.warning(f"diff alert mail to {address} failed: {type(exc).__name__}")
                errors.append(f"{address}: {type(exc).__name__}")
                continue
            sent += 1
        return sent, tuple(errors), None


def render_diff_email(summary: DiffSummary, decision: AlertDecision) -> tuple[str, str]:
    """Render the SMTP payload for one alerting decision.

    Args:
        summary: The comparison that differed.
        decision: The governance verdict carrying level and reason.

    Returns:
        ``(subject, body)``.
    """
    subject = f"[opendata/{decision.level}] {summary.domain} 双源差异 {summary.diff_rate:.2%}"
    lines = [
        f"域: {summary.domain}",
        f"源对: {summary.source_a} vs {summary.source_b}",
        f"批次: {summary.batch_id}",
        f"对比键数: {summary.compared_keys}",
        f"值差异行: {summary.deviation_count}  缺行: {summary.missing_count}"
        f"  差异率: {summary.diff_rate:.4f}",
        f"级别: {decision.level}  原因: {decision.reason}",
        f"字段分布: {dict(sorted(summary.per_field.items()))}",
    ]
    if summary.samples:
        lines.append("样本（最多 5 条）:")
        lines.extend(
            f"  {'|'.join(str(part) for part in diff.biz_key)} {diff.field}: "
            f"{diff.value_a!r} != {diff.value_b!r}"
            for diff in summary.samples[:5]
        )
    lines.append(f"校对时间: {summary.checked_at.isoformat()}")
    return subject, "\n".join(lines)


def smtp_mail_sender() -> MailSend | None:
    """Build the SMTP sender from settings, or None when mail is not configured.

    The A1 ``NotificationService`` already owns the SMTP transport (and the
    ``smtp_host``/``smtp_user`` gates), so this reuses it rather than opening
    a second mailbox path with its own idea of "configured".
    """
    from opendata.core.config import settings

    if not settings.smtp_host or not settings.smtp_user:
        return None

    async def send(to_addr: str, subject: str, body: str) -> None:
        """Send one diff-alert mail off the event loop."""
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        from opendata.services.notification_service import _smtp_send

        message = MIMEMultipart()
        message["From"] = settings.emails_from_email or settings.smtp_user or "opendata"
        message["To"] = to_addr
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _smtp_send(
                host=str(settings.smtp_host),
                port=settings.smtp_port,
                user=str(settings.smtp_user),
                password=settings.smtp_password,
                msg=message,
            ),
        )

    return send


def production_dispatcher(*, refresh: bool = False) -> DiffAlertDispatcher:
    """Wire the dispatcher to this deployment's channels.

    ``ws_manager`` and the subscription hub are imported lazily so an offline
    caller can still build a dispatcher with injected sinks.

    Args:
        refresh: Re-read the governance file before wiring recipients.

    Returns:
        A dispatcher whose ``notify`` matches the ``Notifier`` contract.
    """
    from opendata.api.websocket import ws_manager

    return DiffAlertDispatcher(
        broadcast=ws_manager.broadcast,
        hub=_subscription_hub(),
        mail_send=smtp_mail_sender(),
        recipients=governance(refresh=refresh).recipients,
    )


def default_notifier() -> Callable[[DiffSummary, AlertDecision], Awaitable[Delivery]]:
    """The pipeline's step-3 notifier: production channels, resolved on first use.

    Lazily built because the incremental pipeline is constructed in unit
    tests that never reach an alert: resolving ``ws_manager`` and the SMTP
    settings at build time would make every pipeline test import the API
    layer, so the channels are only wired once a decision actually needs
    delivering. Each delivery is logged, which is how a step-3 alert shows
    up in the run log without a job result to carry it.
    """
    dispatcher: DiffAlertDispatcher | None = None

    async def notify(summary: DiffSummary, decision: AlertDecision) -> Delivery:
        """Deliver one decision and report what the channels did."""
        nonlocal dispatcher
        if dispatcher is None:
            dispatcher = production_dispatcher()
        delivery = await dispatcher.notify(summary, decision)
        logger.info(f"diff alert {summary.batch_id}: {delivery.as_dict()}")
        return delivery

    return notify


def _subscription_hub() -> object | None:
    """The module-level data subscription hub, or None when it is absent."""
    from opendata.pipeline import subscription

    hub = getattr(subscription, "hub", None)
    if hub is None:  # pragma: no cover - the module builds it at import
        logger.warning("diff alert hub unavailable: subscription module exposes no hub")
    return hub
