"""Graded credential health for the key-bearing sources (AC-19 / NFR-5).

The patrol already *sees* every credential failure: ``key_status()`` reports
whether a Key is configured, and a probe that fails with a 401 lands in the
report. What it could not say is **whose problem it is**. A missing Key, a Key
that was revoked, an exhausted quota and a source that is simply down were all
one line of text and one exit code, so the only actionable reading of the
report was "something is wrong with ths".

This module turns that text into a graded, owned classification:

* every failure is classified from the structured fields the providers already
  raise (``category`` on :class:`opendata_fuyao.errors.FuyaoError`, ``code`` and
  ``status`` on the ``*ProviderError`` family), falling back to the message text
  only for shapes that carry nothing else - the ported tree's
  ``requests.HTTPError`` and bare timeouts;
* each class names an **owner** (deployment / credential holder / capacity /
  upstream / the porting layer itself) and a **level**, so an alert can be
  routed rather than re-read;
* the two confusions that make a credential plane useless are closed by
  construction: a blip is never reported as a ban (they are different classes
  with different owners), and a rejected credential is never reported as "no
  data" (a 401 and a 3001 do not share a class);
* nothing that leaves here can carry a secret. Attribution is built from
  status codes and URL **origins** - the query is dropped, because that is
  where ``FRED_API_KEY`` travels - and :func:`redact` is applied to the free
  text the patrol archives, with the values this process holds replaced rather
  than trusted to the URL rule alone.

The boundary is stated as plainly as the capability: **no source in this repo
publishes a Key's expiry, remaining quota or revocation status** (the AC-19
演进项). A report therefore never says "healthy" on the strength of a Key being
configured; it says ``presence-only`` and lists what stays unverified. Only a
classified failure can raise a level, and only an observation can raise it as
high as ``alert`` - which is what keeps "we could not look" from reading as
"we looked and it was fine".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The Key is required and absent: a deployment gap, not a source problem.
CLASS_NOT_CONFIGURED = "not-configured"
#: The credential was presented and refused (401/403, fuyao auth/permission).
CLASS_CREDENTIAL_REJECTED = "credential-rejected"
#: The credential is valid but the request budget is not (429, fuyao 4001).
CLASS_QUOTA_EXHAUSTED = "quota-exhausted"
#: The source itself failed (5xx, fuyao 5xxx): no credential is implicated.
CLASS_SOURCE_DEGRADED = "source-degraded"
#: A connection that never completed (reset, DNS, timeout in flight).
CLASS_TRANSIENT_BLIP = "transient-blip"
#: The source answered and said there is nothing here (3001/3002, 404).
CLASS_NO_DATA = "no-data"
#: Our request could not be served by this endpoint (4xx, envelope, bad params).
CLASS_LOCAL_REQUEST = "local-request"
#: The patrol had no probe for the leg, so nothing about the source is known.
CLASS_PATROL_GAP = "patrol-gap"
#: A shape this module cannot read. Never treated as a pass.
CLASS_UNCLASSIFIED = "unclassified"

#: Levels, worst first, so a report can be sorted and thresholded.
LEVEL_ALERT = "alert"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"
#: A Key is configured and no failure was observed: presence, nothing more.
LEVEL_PRESENCE_ONLY = "presence-only"
#: Source needs no credential, so the credential plane says nothing about it.
LEVEL_NOT_APPLICABLE = "not-applicable"

_LEVEL_ORDER = (
    LEVEL_ALERT,
    LEVEL_WARN,
    LEVEL_INFO,
    LEVEL_PRESENCE_ONLY,
    LEVEL_NOT_APPLICABLE,
)

#: Who has to act, per class. Naming the owner is the point of the classification.
CLASS_OWNERS: dict[str, str] = {
    CLASS_NOT_CONFIGURED: "部署",
    CLASS_CREDENTIAL_REJECTED: "凭据负责人",
    CLASS_QUOTA_EXHAUSTED: "容量/采购",
    CLASS_SOURCE_DEGRADED: "上游源侧",
    CLASS_TRANSIENT_BLIP: "无需处置（抖动）",
    CLASS_NO_DATA: "业务口径",
    CLASS_LOCAL_REQUEST: "本地搬运层",
    CLASS_PATROL_GAP: "巡检自身",
    CLASS_UNCLASSIFIED: "未定（需补分类）",
}

#: Level per class. A blip is ``info`` and a ban is ``alert``: the distinction
#: the report exists to make.
CLASS_LEVELS: dict[str, str] = {
    CLASS_NOT_CONFIGURED: LEVEL_ALERT,
    CLASS_CREDENTIAL_REJECTED: LEVEL_ALERT,
    CLASS_QUOTA_EXHAUSTED: LEVEL_WARN,
    CLASS_UNCLASSIFIED: LEVEL_WARN,
    CLASS_SOURCE_DEGRADED: LEVEL_INFO,
    CLASS_TRANSIENT_BLIP: LEVEL_INFO,
    CLASS_NO_DATA: LEVEL_INFO,
    CLASS_LOCAL_REQUEST: LEVEL_INFO,
    CLASS_PATROL_GAP: LEVEL_INFO,
}

#: Classes about the credential rather than about the source or the data.
CREDENTIAL_CLASSES = frozenset(
    {CLASS_NOT_CONFIGURED, CLASS_CREDENTIAL_REJECTED, CLASS_QUOTA_EXHAUSTED}
)

#: What no probe in this repo can prove, whatever the level says. AC-19's 演进项
#: recorded as data instead of prose, so ``presence-only`` cannot be misread.
UNVERIFIED_WITHOUT_ACTIVE_CHECK = ("key-validity", "key-expiry", "revocation-or-ban", "quota-left")

#: fuyao's stable transport categories (``opendata_fuyao/errors.py``) mapped onto
#: the health classes. The transport taxonomy is the single source of truth for
#: ``auth``/``rate_limited``/... ; this table only re-homes it, and "http" is
#: deliberately absent because an HTTP status is the whole question there.
_FUYAO_CATEGORY_CLASSES: dict[str, str] = {
    "auth": CLASS_CREDENTIAL_REJECTED,
    "permission": CLASS_CREDENTIAL_REJECTED,
    "rate_limited": CLASS_QUOTA_EXHAUSTED,
    "transient": CLASS_SOURCE_DEGRADED,
    "network": CLASS_TRANSIENT_BLIP,
    "timeout": CLASS_TRANSIENT_BLIP,
    "empty": CLASS_NO_DATA,
    "not_ready": CLASS_NO_DATA,
    "request": CLASS_LOCAL_REQUEST,
    "unsupported": CLASS_LOCAL_REQUEST,
    "envelope_invalid": CLASS_LOCAL_REQUEST,
    "response_too_large": CLASS_LOCAL_REQUEST,
    # `ok` on a raised error is a contradiction; `unknown` is an unmapped
    # upstream code. Neither may be read as a pass.
    "ok": CLASS_UNCLASSIFIED,
    "unknown": CLASS_UNCLASSIFIED,
}

#: Statuses that outrank any provider code naming a different cause.
_CREDENTIAL_STATUSES = frozenset({401, 403, 429})

#: Provider codes meaning "this client has no credential to use".
_CONFIG_CODE_SUFFIXES = ("_API_KEY_MISSING", "_NOT_CONFIGURED")
#: Provider codes meaning "the source answered and has nothing for this query".
_NO_DATA_CODE_SUFFIXES = ("_EMPTY_RESPONSE",)
#: Provider codes meaning "the request, the symbol or the parsing - not the
#: credential and not the source's health". Enumerated from the codes the
#: bundled providers actually raise, so a new code is a decision made here
#: rather than a silent reclassification into ``unclassified``.
_LOCAL_REQUEST_CODE_SUFFIXES = (
    "_SYMBOL_UNRESOLVED",
    "_SYMBOL_INVALID",
    "_SYMBOL_MISMATCH",
    "_DUPLICATE_SYMBOL",
    "_QUALIFIED_REQUIRED",
    "_BAD_RESPONSE",
    "_BAD_OBSERVATION",
    "_UNSUPPORTED",
    "_SNAPSHOT_ONLY",
    "_SNAPSHOT_DATE_MISSING",
    "_WINDOW_INCOMPLETE",
)

#: Transport exception names that fail before any status can be read.
_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "NetworkError",
        "TransportError",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "RemoteDisconnected",
        "SSLError",
    }
)

_STATUS_IN_CODE_RE = re.compile(r"_(\d{3})(?:_\w+)*$")
_STATUS_IN_TEXT_RE = re.compile(r"\bstatus=(\d{3})\b")
_STATUS_IN_HTTP_ERROR_RE = re.compile(r"\b([45]\d{2}) (?:Client Error|Server Error)")
#: httpx writes ``Client error '429 Too Many Requests' for url '<full url>'`` -
#: the query included, which is why its text is read for a status and then
#: dropped by :func:`redact`.
_STATUS_IN_HTTPX_RE = re.compile(r"(?:Client|Server) error '([45]\d{2})\b")
_URL_RE = re.compile(r"https?://[^\s'\"]+")

#: Attribution lines are made of these, so nothing else can slip into a payload.
_MAX_ATTRIBUTION_FIELDS = 3
_MAX_OBSERVATIONS_PER_CLASS = 3

#: What a known credential value is replaced with inside report text.
REDACTED_MARKER = "<redacted-key>"


@dataclass(frozen=True)
class FailureObservation:
    """One classified failure of one source, as the patrol saw it.

    Attributes:
        source: Source identifier the probe was running against.
        failure_class: One of the ``CLASS_*`` values.
        attribution: Metadata-only origin (code, status, host+path), or None
            when the failure named nothing that can be shown.
    """

    source: str
    failure_class: str
    attribution: str | None = None


@dataclass(frozen=True)
class KeyReport:
    """The graded credential health of one source.

    Attributes:
        source: Source identifier.
        required: Whether the source cannot work without a Key.
        configured: Whether a Key is present in this process's settings.
        endpoint: Host the credential is sent to (never a query, never a Key).
        level: One of the ``LEVEL_*`` values, worst observation wins.
        owner: Who has to act; None when nothing asks anyone to act.
        classes: Observed classes with their counts, worst first.
        attributions: Distinct metadata-only attributions behind the classes.
        unverified: What active checking this repo does not have, and so what
            no level here has ever ruled out.
        note: One line a reader can act on.
    """

    source: str
    required: bool
    configured: bool
    endpoint: str
    level: str
    owner: str | None
    classes: tuple[tuple[str, int], ...]
    attributions: tuple[str, ...]
    unverified: tuple[str, ...]
    note: str

    def as_dict(self) -> dict[str, Any]:
        """Return the credential payload for an API response or a report line.

        Returns:
            Every field of the report, with the class counts flattened. The
            payload is metadata only: no Key value, no request headers, no URL
            query - which is what the credential-leak guard asserts.
        """
        return {
            "source": self.source,
            "required": self.required,
            "configured": self.configured,
            "endpoint": self.endpoint,
            "level": self.level,
            "owner": self.owner,
            "classes": [{"class": name, "count": count} for name, count in self.classes],
            "attributions": list(self.attributions),
            "unverified": list(self.unverified),
            "note": self.note,
        }


def classify_failure(exc: BaseException) -> str:
    """Classify one provider failure into a health class.

    The fields are read in order of trustworthiness: the transport's own stable
    ``category``, then a 401/403/429 on the structured status (which outranks a
    provider code naming another cause), then the provider ``code``'s suffix,
    then any status the failure names, then the exception type for a connection
    that never completed.

    Args:
        exc: The exception the probe raised, from any provider or transport.

    Returns:
        One of the ``CLASS_*`` values. An exception this module cannot read is
        ``CLASS_UNCLASSIFIED``, which is a ``warn`` - never a silent pass.
    """
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category:
        if category == "http":
            # fuyao collapses every non-2xx/429 into one transport key and puts
            # the status in the stable code, so the status is the classification.
            status = _status_of(exc)
            return _class_for_status(status) if status is not None else CLASS_UNCLASSIFIED
        mapped = _FUYAO_CATEGORY_CLASSES.get(category)
        if mapped is not None:
            return mapped

    code = getattr(exc, "code", None)
    refused = getattr(exc, "status", None)
    if isinstance(refused, int) and refused in _CREDENTIAL_STATUSES:
        # A refused credential or a spent budget outranks every other reading of
        # the same response - including a code that names a parsing problem, which
        # is what a client would report after giving up on an empty 401 body.
        return _class_for_status(refused)
    if isinstance(code, str) and code:
        if code.endswith(_CONFIG_CODE_SUFFIXES):
            return CLASS_NOT_CONFIGURED
        if code.endswith(_NO_DATA_CODE_SUFFIXES):
            return CLASS_NO_DATA
        if code.endswith(_LOCAL_REQUEST_CODE_SUFFIXES):
            return CLASS_LOCAL_REQUEST

    status = _status_of(exc)
    if status is not None:
        return _class_for_status(status)

    if type(exc).__name__ in _TRANSIENT_EXCEPTION_NAMES:
        return CLASS_TRANSIENT_BLIP
    return CLASS_UNCLASSIFIED


def attribution_of(exc: BaseException) -> str:
    """Describe one failure with fields that can be published.

    Args:
        exc: The exception to describe.

    Returns:
        Stable code, status and the URL **origin** joined by spaces - the query
        is dropped, so the ``api_key`` a Key-in-query provider puts on the wire
        cannot reach a report. Falls back to the exception type name, which
        names a transport, not a credential.
    """
    fields: list[str] = []
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        fields.append(code)
    status = _status_of(exc)
    if status is not None:
        fields.append(f"status={status}")
    origin = _origin_of(getattr(exc, "url", None)) or _first_origin(str(exc))
    if origin:
        fields.append(origin)
    if not fields:
        return type(exc).__name__
    return " ".join(fields[:_MAX_ATTRIBUTION_FIELDS])


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Strip credentials from free text destined for a report.

    Args:
        text: The message a probe failure produced.
        secrets: The Key values this process actually holds (see
            :func:`opendata.pipeline.patrol.key_values`); empty entries are
            ignored.

    Returns:
        The same text with every URL reduced to its origin and every listed Key
        value replaced by :data:`REDACTED_MARKER`. A provider that builds a
        message from ``raise_for_status()`` embeds the full URL, query included
        - for a Key-in-query source that is the credential, so no query is
        allowed to survive here regardless of which source is behaving. The
        value pass covers the shape the URL rule cannot: a client that quotes
        its own ``Authorization`` header into a failure message.
    """
    scrubbed = _URL_RE.sub(lambda match: _origin_of(match.group(0)) or match.group(0), text)
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, REDACTED_MARKER)
    return scrubbed


def build_report(
    source: str,
    *,
    required: bool,
    configured: bool,
    endpoint: str,
    observations: Sequence[FailureObservation],
) -> KeyReport:
    """Grade one source's credential health from presence and observations.

    Args:
        source: Source identifier.
        required: Whether the source cannot work without a Key.
        configured: Whether a Key is configured in this process.
        endpoint: Host the credential is sent to.
        observations: Classified probe failures for this source. A source that
            was never probed contributes none.

    Returns:
        The report. An absent required Key is an ``alert`` with no observation
        to show; a configured Key with no failure is ``presence-only``, because
        nothing looked at whether the Key still works.
    """
    own = tuple(obs for obs in observations if obs.source == source)
    if own:
        classes = _ranked_classes(own)
        level = CLASS_LEVELS[classes[0][0]]
        owner = CLASS_OWNERS[classes[0][0]]
        return KeyReport(
            source=source,
            required=required,
            configured=configured,
            endpoint=endpoint,
            level=level,
            owner=owner,
            classes=classes,
            attributions=_attributions(own),
            unverified=UNVERIFIED_WITHOUT_ACTIVE_CHECK,
            note=_observed_note(source, classes, required, configured),
        )
    if not required:
        return KeyReport(
            source=source,
            required=required,
            configured=configured,
            endpoint=endpoint,
            level=LEVEL_NOT_APPLICABLE,
            owner=None,
            classes=(),
            attributions=(),
            unverified=(),
            note="该源不需要 Key，凭证面不对它作判断",
        )
    if not configured:
        return KeyReport(
            source=source,
            required=required,
            configured=configured,
            endpoint=endpoint,
            level=LEVEL_ALERT,
            owner=CLASS_OWNERS[CLASS_NOT_CONFIGURED],
            classes=((CLASS_NOT_CONFIGURED, 1),),
            attributions=(),
            unverified=UNVERIFIED_WITHOUT_ACTIVE_CHECK,
            note=f"{source} 必须 Key 未配置：部署缺口，与源侧健康无关",
        )
    return KeyReport(
        source=source,
        required=required,
        configured=configured,
        endpoint=endpoint,
        level=LEVEL_PRESENCE_ONLY,
        owner=None,
        classes=(),
        attributions=(),
        unverified=UNVERIFIED_WITHOUT_ACTIVE_CHECK,
        note=(
            f"{source} 已配置 Key 且本轮巡检未见失败；本轮没有到期/封禁主动探测，也未见任何 429，"
            "所以本行不等于 Key 有效"
        ),
    )


def _class_for_status(status: int) -> str:
    """Map an HTTP status onto the class that owns it.

    Args:
        status: The status code a provider reported.

    Returns:
        401/403 are a refused credential, 429 a spent budget, 5xx the source's
        own failure, 404 an answered "nothing here", other 4xx a request this
        endpoint cannot serve. Any other status is unclassified: guessing here
        would let a new failure mode look like a known one.
    """
    if status in (401, 403):
        return CLASS_CREDENTIAL_REJECTED
    if status == 429:
        return CLASS_QUOTA_EXHAUSTED
    if status == 404:
        return CLASS_NO_DATA
    if status >= 500:
        return CLASS_SOURCE_DEGRADED
    if 400 <= status < 500:
        return CLASS_LOCAL_REQUEST
    return CLASS_UNCLASSIFIED


def _status_of(exc: BaseException) -> int | None:
    """Read the HTTP status a failure carries, from structured fields first.

    Args:
        exc: The exception to inspect.

    Returns:
        The status, or None when the failure names none. ``status`` (the macro
        providers), ``response.status_code`` (httpx and requests, whose
        ``raise_for_status()`` is what the ported tree fails with) and the
        status baked into a fuyao code are all structured; only the message text
        is read last.
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 100 <= status < 600:
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and 100 <= status < 600:
        return status
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        match = _STATUS_IN_CODE_RE.search(code)
        if match:
            return int(match.group(1))
    return _status_from_text(str(exc))


def _status_from_text(text: str) -> int | None:
    """Pick a status out of message text written in one of the known shapes.

    Args:
        text: The exception message.

    Returns:
        The status, or None. Only the shapes the transports actually write are
        read - ``status=NNN`` from the macro providers, ``NNN Server Error``
        from requests and ``Client error 'NNV`` from httpx. An unanchored
        three-digit match would turn a row count or a window length into a
        status code.
    """
    for pattern in (_STATUS_IN_TEXT_RE, _STATUS_IN_HTTPX_RE, _STATUS_IN_HTTP_ERROR_RE):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _origin_of(url: object) -> str | None:
    """Reduce a URL to scheme, host and path.

    Args:
        url: A URL (or anything; non-strings yield None).

    Returns:
        ``scheme://netloc/path``, or None when the value is not an absolute URL.
    """
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _first_origin(text: str) -> str | None:
    """Return the origin of the first URL mentioned in ``text``.

    Args:
        text: The exception message.

    Returns:
        The origin, or None when no absolute URL appears.
    """
    match = _URL_RE.search(text)
    return _origin_of(match.group(0)) if match else None


def _ranked_classes(observations: Sequence[FailureObservation]) -> tuple[tuple[str, int], ...]:
    """Count observed classes, worst first.

    Args:
        observations: The failures of one source.

    Returns:
        ``(class, count)`` pairs ordered by level then class name, so the first
        entry is the one that sets the report's level and owner.
    """
    counts: dict[str, int] = {}
    for obs in observations:
        counts[obs.failure_class] = counts.get(obs.failure_class, 0) + 1
    return tuple(
        sorted(
            counts.items(),
            key=lambda item: (_level_rank(CLASS_LEVELS.get(item[0], LEVEL_WARN)), item[0]),
        )
    )


def _level_rank(level: str) -> int:
    """Rank a level so the worst of several can be selected.

    Args:
        level: One of the ``LEVEL_*`` values.

    Returns:
        Its index in :data:`_LEVEL_ORDER`; anything unknown ranks as ``warn``,
        because an unranked level must not read as the quietest thing on a page.
    """
    try:
        return _LEVEL_ORDER.index(level)
    except ValueError:
        return _LEVEL_ORDER.index(LEVEL_WARN)


def _attributions(observations: Sequence[FailureObservation]) -> tuple[str, ...]:
    """Collect the distinct metadata-only attributions of one source.

    Args:
        observations: The failures of one source.

    Returns:
        Up to :data:`_MAX_OBSERVATIONS_PER_CLASS` attributions, ordered by the
        worst class they belong to, empty when every failure was too vague to
        show.
    """
    ordered: list[str] = []
    for name, _ in _ranked_classes(observations):
        for obs in observations:
            if obs.failure_class == name and obs.attribution and obs.attribution not in ordered:
                ordered.append(obs.attribution)
    return tuple(ordered[:_MAX_OBSERVATIONS_PER_CLASS])


def _observed_note(
    source: str,
    classes: tuple[tuple[str, int], ...],
    required: bool,
    configured: bool,
) -> str:
    """Write the one-line note for a source that produced observations.

    Args:
        source: Source identifier.
        classes: Ranked observed classes (non-empty).
        required: Whether the source needs a Key at all.
        configured: Whether a Key is configured.

    Returns:
        A note naming the worst class, its count and whose problem it is. A
        quota count is spelled out as the only quota evidence this repo has:
        no source publishes a remaining budget, so a 429 seen on the wire is
        what the quota plane reports, and nothing else is claimed.
    """
    worst, count = classes[0]
    rest = "" if len(classes) == 1 else f"；另有 {'/'.join(name for name, _ in classes[1:])}"
    key_state = "无 Key 源" if not required else "已配置 Key" if configured else "未配置 Key"
    quota = ""
    if worst == CLASS_QUOTA_EXHAUSTED:
        quota = f"；配额证据只有 {count} 次 429 本身，无余量主动源"
    owner = CLASS_OWNERS[worst]
    return f"{source} 本轮 {count} 次 {worst}（{key_state}），处置方={owner}{rest}{quota}"
