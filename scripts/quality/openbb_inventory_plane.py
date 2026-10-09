#!/usr/bin/env python3
"""Fail when the OpenBB provider inventory stops describing the running system.

C33 / AC-10 judgment plane. One module, imported by ``tests/test_openbb_inventory_guard.py``
(that is where it runs in ``make gate``, via ``test-cov``) and runnable by hand to produce
the archived census (``docs/evidence/C33/``).

Why this file exists: ``docs/proposals/openbb-migration/README.md`` §5 designates
``provider-inventory.yaml`` as the progress baseline — "批次进度以本文件与
``provider-inventory.yaml`` 的 ``status`` 字段为准" — and nothing in the tree ever read
that file back. What a write-only record became, measured at the start of this round:

  * it does not deserialize (34 lines of ``credentials: -`` / ``sdk_dependencies: -``, where
    a bare ``-`` is a sequence marker, not a value), so no guard could have checked it;
  * and of the 64 dependency/credential fields behind those dashes, **none was a list**:
    21 were bare scalars (``sdk_dependencies: xmltodict``) and 9 were one comma-joined
    string — those two shapes *do* parse, as strings, so a tool reading them counted 1
    dependency and tested membership by substring;
  * C33 snapshot: all 32 rows say ``待实现`` while 7 sources register 33 capabilities,
    20 of which are auto-routable (the other 13 are registered, importable, and skipped
    by ``auto``);
  * the two largest sources in the system — ``ths`` (11 capabilities) and ``akshare`` (10) —
    have no row at all, so 21 of 33 live capabilities are invisible to the baseline;
  * its own header claims "33 个 provider 目录" while the body holds 32 rows and the
  * trailing comment says 32, and nothing pins the "348 个 fetcher 模型" figure either.

Seven rules, each of which can be non-zero in both directions:

  * ``PARSE`` — the record must deserialize as the data type its filename promises.
  * ``FIELD SHAPE`` — a field that names a list (``models`` / ``sdk_dependencies`` /
    ``credentials`` / ``rights_rows``) must deserialize as a list, not as one string and
    not as ``null``. ``PARSE`` cannot see this class: the offending lines are valid YAML.
    Judged only while ``PARSE`` is green — a file that does not deserialize has no field
    types to judge, and double-reporting one broken line would inflate the count.
  * ``MISSING ROW`` — a source the registry serves must appear in exactly one section.
  * ``DUPLICATE ROW`` — appearing in both sections is not "exactly one".
  * ``STALE STATUS`` — ``status`` must equal what the registry says, by exact comparison:
    ``待实现`` iff no capability is registered, ``已对照转正`` iff every capability of the
    source takes part in ``auto`` routing, ``已实现未对照`` otherwise. "Otherwise" is the
    state this round made nameable: implemented, importable, routable by explicit source,
    and still excluded from ``auto``. The C33 snapshot placed ``fred`` and ``akshare`` in
    this state; C65 comparison promoted FRED's three registered capabilities, and the
    current registry count is 23 auto-routable / 10 registered but not auto-routable.
  * ``COUNT CLAIM`` — declared counts and the header's prose numbers must equal the body.
  * ``RIGHTS LINK`` — a source that serves data must name its row in
    ``docs/data-rights-registry.md``; AC-1 gate R3 says an unregistered source may not be
    routed, so the baseline that tracks routing must be able to point at the registration.

Model completeness is judged from the fixed upstream AST and model task ledger. An explicit
``...``, a mismatch between ``fetchers`` and listed keys, duplicate names, or a provider/model
set mismatch fails the plane. Registry capability counts and model-task status are separate
fields: a source with one verified capability does not imply that its full upstream model set
is implemented.

The registry side is read in-process (import + ``register_providers()``); no network, no
MySQL, no credential is touched. Callers that want to judge a copy of the record — the
"before" archive and the falsification runs — pass ``--inventory``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess  # nosec B404  # git_text(): GIT const + 3 fixed read-only argvs
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
GIT: Final = shutil.which("git") or "git"

INVENTORY: Final = REPO_ROOT / "docs/proposals/openbb-migration/provider-inventory.yaml"
RIGHTS_REGISTRY: Final = REPO_ROOT / "docs/data-rights-registry.md"

#: The three states the record distinguishes. One word was not enough: the tree has
#: sources that are implemented, importable and explicitly routable, but excluded from
#: ``auto`` because no upstream comparison has been recorded (README §6.2, R2/R5).
STATUS_TODO: Final = "待实现"
STATUS_UNVERIFIED: Final = "已实现未对照"
STATUS_VERIFIED: Final = "已对照转正"
KNOWN_STATUSES: Final = (STATUS_TODO, STATUS_UNVERIFIED, STATUS_VERIFIED)

ROW_HEAD: Final = re.compile(r"^  - provider: (?P<provider>\S+)\s*$")
ROW_FIELD: Final = re.compile(r"^    (?P<key>\w+): (?P<value>.*)$")
SECTION: Final = re.compile(r"^(?P<section>providers|local_providers):\s*$")
HEADER_PROSE: Final = re.compile(
    r"规模：(?P<providers>\d+)\s*个\s*provider 目录 / (?P<fetchers>\d+) 个"
)
DECLARED_COUNT: Final = re.compile(
    r"^(?P<key>provider_count|local_provider_count|upstream_model_count|"
    r"unique_upstream_model_count|registered_source_count|registered_capability_count|"
    r"auto_routable_capability_count):\s*(?P<value>\d+)\s*$",
    re.MULTILINE,
)
RIGHTS_TABLE_ROW: Final = re.compile(r"^\|\s*(?P<number>\d+)\s*\|(?P<cells>.*)$")

#: Fields whose name says "a list of names". Each must deserialize as a list: a bare
#: scalar or a comma string parses as *one* string, and a key with no value parses as
#: ``None`` — neither of which is a list, whatever the file looks like to a reader.
LIST_FIELDS: Final = ("models", "sdk_dependencies", "credentials", "rights_rows")
SECTIONS: Final = ("providers", "local_providers")

#: The judged rules, in the order the archive prints their counters. Adding a rule here
#: means adding it to ``findings()`` too — the counters and the header line are both
#: derived from this tuple, so a run's archive says which rule set judged it.
KINDS: Final = (
    "PARSE",
    "FIELD SHAPE",
    "MISSING ROW",
    "DUPLICATE ROW",
    "STALE STATUS",
    "COUNT CLAIM",
    "RIGHTS LINK",
    "MODEL ELISION",
    "MODEL DUPLICATE",
    "MODEL COUNT",
    "MODEL SET",
    "CREDENTIAL MISMATCH",
    "CAPABILITY COUNT",
    "MODEL TASK STATUS",
    "REQUESTER STATUS",
)


@dataclass(frozen=True)
class Finding:
    """One rule violation: the kind names the counter, the text names the drift."""

    kind: str
    text: str

    def __str__(self) -> str:
        """Render as ``KIND: detail``, the shape the archive prints.

        Returns:
            The finding on one line.
        """
        return f"{self.kind}: {self.text}"


@dataclass(frozen=True)
class Row:
    """One provider entry of the record, read structurally from its lines.

    Attributes:
        provider: The source label the row is about.
        section: ``providers`` (an OpenBB upstream internalization item) or
            ``local_providers`` (a source we run that upstream does not ship).
        fields: Every scalar/flow field written under the head, as raw strings.
    """

    provider: str
    section: str
    fields: dict[str, str]


def git_text(*args: str) -> str:
    """Run a read-only git command and return its trimmed stdout.

    Args:
        *args: Arguments after ``git``, taken from a fixed argv (no shell, no input).

    Returns:
        Trimmed stdout, or ``<unavailable>`` when git declines to answer.
    """
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603  # GIT const; 3 fixed read-only argvs
            [GIT, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "<unavailable>"
    return proc.stdout.strip() if proc.returncode == 0 else "<unavailable>"


def flow_list(raw: str) -> list[str]:
    """Split a YAML flow sequence written on one line.

    Args:
        raw: The field's text, e.g. ``[a, b, c]`` or a bare scalar.

    Returns:
        The trimmed members, or ``[]`` when the value is not a flow sequence.
        A ``...`` member is dropped: it marks truncation, it is not a name.
    """
    value = raw.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return []
    return [
        part.strip() for part in value[1:-1].split(",") if part.strip() and part.strip() != "..."
    ]


def declared_rights(raw: str) -> list[str]:
    """Read a row's ``rights_rows`` field however it happens to be written.

    Args:
        raw: The field's text — a flow sequence, a single name, or an empty/``-`` value.

    Returns:
        The named rights rows, with the "none needed" marks dropped.
    """
    value = raw.strip()
    if value.startswith("["):
        return flow_list(value)
    if not value or value == "-":
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_rows(text: str) -> list[Row]:
    """Read the record's rows line by line, without needing it to be valid YAML.

    The file did not parse when this plane was written, and the point of the ``PARSE``
    rule is exactly that a broken record must still be describable — so the rows come
    from here, and :func:`parse_error` separately reports what a real parser makes of
    the same bytes.

    Args:
        text: Whole file contents.

    Returns:
        One :class:`Row` per ``- provider:`` line, tagged with its section.
    """
    rows: list[Row] = []
    section = ""
    current: dict[str, str] | None = None
    provider = ""
    row_section = ""
    for line in text.splitlines():
        head = ROW_HEAD.match(line)
        if head:
            if current is not None:
                rows.append(Row(provider, row_section, current))
            provider = head.group("provider")
            # The section is captured here, not when the row is flushed: a row is closed
            # by the *next* head, and the last row of a section is closed after the
            # section line has already moved on (C33 read 31 providers + 3 locals until
            # this line pinned the section at the moment the row opened).
            row_section = section
            current = {}
            continue
        sect = SECTION.match(line)
        if sect:
            section = sect.group("section")
            continue
        field = ROW_FIELD.match(line)
        if field and current is not None:
            current[field.group("key")] = field.group("value").strip()
    if current is not None:
        rows.append(Row(provider, row_section, current))
    return rows


def parse_error(text: str) -> str | None:
    """Ask a real YAML parser what it makes of the record.

    Args:
        text: Whole file contents.

    Returns:
        ``None`` when the document deserializes into a mapping holding ``providers``,
        otherwise the parser's own first two lines (or why the shape is unusable).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # the finding IS the exception
        lines = [ln for ln in str(exc).splitlines() if ln.strip()]
        return " | ".join(lines[:2])
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return "parsed, but the document has no `providers:` list"
    return None


def field_shapes(text: str) -> list[Finding]:
    """Report list-shaped fields that a real parser does not hand back as lists.

    ``PARSE`` judges whether the file deserializes at all; this judges the second failure
    mode, which PARSE is blind to because the offending lines *are* valid YAML. Written as
    ``sdk_dependencies: xmltodict`` the value arrives as the string ``"xmltodict"``, so
    ``len()`` reported 1 dependency and ``"async-lru" in value`` was a substring test;
    written as ``credentials:`` with nothing after it the value arrives as ``None``, which
    is not an empty list either. Called only when PARSE is green.

    Args:
        text: Whole file contents of a record that deserializes.

    Returns:
        One finding per (row, field) whose parsed value is not a list.
    """
    body = yaml.safe_load(text)
    if not isinstance(body, dict):
        return []
    out: list[Finding] = []
    for section in SECTIONS:
        entries = body.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider", "(unnamed row)"))
            for field in LIST_FIELDS:
                if field not in entry:
                    continue
                value = entry[field]
                if isinstance(value, list):
                    continue
                shown = "null（键写了却没值）" if value is None else f"`{value}`"
                out.append(
                    Finding(
                        "FIELD SHAPE",
                        f"`{provider}`（{section}）把 `{field}` 写成 {shown}，反序列化得到 "
                        f"{type(value).__name__} 而不是 list："
                        f"名单字段要写成 `[a, b]` 或块状列表",
                    )
                )
    return out


def declared_counts(text: str) -> dict[str, int]:
    """Read the record's own machine-declared counts.

    Args:
        text: Whole file contents.

    Returns:
        ``provider_count`` / ``local_provider_count`` values that are present, as ints.
    """
    return {m.group("key"): int(m.group("value")) for m in DECLARED_COUNT.finditer(text)}


def header_prose(text: str) -> tuple[int, int] | None:
    """Read the numbers the header comment claims about the body.

    Args:
        text: Whole file contents.

    Returns:
        ``(provider_directories, fetcher_models)`` from the ``规模：…`` line, or ``None``.
    """
    match = HEADER_PROSE.search(text)
    if not match:
        return None
    return int(match.group("providers")), int(match.group("fetchers"))


def rights_rows(text: str) -> list[str]:
    """Read the data-source column of the rights registry's registration table.

    Only §1 counts: the file holds a second table whose first column is also numbered,
    and matching a 待办 sentence as if it were a data source is how a rights rule quietly
    becomes unfalsifiable.

    Args:
        text: Whole ``docs/data-rights-registry.md`` contents.

    Returns:
        The 数据源 cell of every §1 row, in file order.
    """
    names: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## 1")
            continue
        if not in_section:
            continue
        match = RIGHTS_TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if cells:
            names.append(cells[0])
    return names


def live_legs() -> dict[str, list[tuple[str, bool]]]:
    """Ask the running registry which capabilities each source actually serves.

    Returns:
        Source label to ``(domain, participates_in_auto)`` pairs, sorted by domain.

    Raises:
        RuntimeError: The provider tree could not be imported, so the tree side of the
            comparison is unknown rather than empty.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry
    except ImportError as exc:
        raise RuntimeError(f"cannot import the provider tree: {exc}") from exc
    register = cast("Any", register_providers)
    register()
    legs: dict[str, list[tuple[str, bool]]] = {}
    for cap in get_registry().capabilities():
        legs.setdefault(cap.source, []).append((cap.domain, cap.participates_in_auto()))
    return {source: sorted(pairs) for source, pairs in legs.items()}


def expected_status(legs: list[tuple[str, bool]]) -> str:
    """Derive the status the registry implies for one source.

    Args:
        legs: The source's ``(domain, participates_in_auto)`` pairs.

    Returns:
        :data:`STATUS_TODO` with no leg, :data:`STATUS_VERIFIED` when every leg is
        auto-routable, and :data:`STATUS_UNVERIFIED` for the mixed/unverified case.
    """
    if not legs:
        return STATUS_TODO
    return STATUS_VERIFIED if all(auto for _, auto in legs) else STATUS_UNVERIFIED


def model_elisions(rows: list[Row]) -> list[str]:
    """Return rows whose model list is explicitly elided or shorter than its fetcher count.

    Args:
        rows: The parsed record rows.

    Returns:
        One line per truncated row: ``provider: 6 listed of 36 stated``.
    """
    out: list[str] = []
    for row in rows:
        stated = row.fields.get("fetchers", "")
        model_text = row.fields.get("models", "")
        listed = len(flow_list(model_text))
        has_ellipsis = "..." in model_text
        if has_ellipsis or (stated.isdigit() and int(stated) > listed):
            out.append(f"{row.provider}: {listed} listed of {stated} stated")
    return out


def _yaml_provider_rows(text: str) -> dict[tuple[str, str], dict[str, object]]:
    """Return parsed provider rows keyed by section/name when the document is valid YAML."""
    try:
        body = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(body, dict):
        return {}
    parsed: dict[tuple[str, str], dict[str, object]] = {}
    for section in SECTIONS:
        entries = body.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("provider"), str):
                parsed[(section, entry["provider"])] = entry
    return parsed


def _summary_status(values: Sequence[str]) -> str:
    """Return a stable status summary for model tasks from the ledger."""
    distinct = sorted(set(values))
    return distinct[0] if len(distinct) == 1 else "MIXED"


def findings(
    inventory_text: str,
    rights_text: str,
    legs: dict[str, list[tuple[str, bool]]],
    expected_models: Mapping[str, Sequence[str]] | None = None,
    expected_credentials: Mapping[str, Sequence[str]] | None = None,
    ledger_rows: Sequence[Mapping[str, object]] | None = None,
) -> list[Finding]:
    """Judge the record against the registry and the rights registry.

    Args:
        inventory_text: Whole ``provider-inventory.yaml`` contents.
        rights_text: Whole ``docs/data-rights-registry.md`` contents.
        legs: Registry-side legs, from :func:`live_legs` or a test fixture.
        expected_models: Fixed AST identities, keyed by upstream provider, when available.
        expected_credentials: Fixed AST credential names, keyed by provider, when available.
        ledger_rows: Model-level task states, separate from registry source ``status``.

    Returns:
        Every violated rule, in report order. Empty means the record and the tree agree.
    """
    rows = parse_rows(inventory_text)
    parsed_rows = _yaml_provider_rows(inventory_text)
    by_provider: dict[str, list[Row]] = {}
    for row in rows:
        by_provider.setdefault(row.provider, []).append(row)
    registry = rights_rows(rights_text)
    out: list[Finding] = []

    error = parse_error(inventory_text)
    if error:
        out.append(Finding("PARSE", f"provider-inventory.yaml does not deserialize ({error})"))
    else:
        out.extend(field_shapes(inventory_text))

    out.extend(
        Finding(
            "MISSING ROW",
            f"`{source}` registers {len(legs[source])} capabilities and has no row "
            f"in either section, so the progress baseline cannot see it",
        )
        for source in sorted(set(legs) - set(by_provider))
    )
    for provider, group in sorted(by_provider.items()):
        if len(group) > 1:
            sections = sorted(str(row.section) for row in group)
            out.append(
                Finding(
                    "DUPLICATE ROW",
                    f"`{provider}` appears in {sections}; a source belongs to exactly one section",
                )
            )

    for row in rows:
        source = row.provider
        served = legs.get(source, [])
        declared = row.fields.get("status", "")
        want = expected_status(served)
        if declared not in KNOWN_STATUSES:
            out.append(
                Finding(
                    "STALE STATUS",
                    f"`{source}` status `{declared or '(absent)'}` is outside {KNOWN_STATUSES}",
                )
            )
        elif declared != want:
            detail = (
                f"{len(served)} capabilities ({sum(1 for _, auto in served if auto)} auto-routable)"
                if served
                else "no capability registered"
            )
            out.append(
                Finding(
                    "STALE STATUS",
                    f"`{source}` says {declared} but the registry says {want}: {detail}",
                )
            )
        if served:
            links = declared_rights(row.fields.get("rights_rows", ""))
            if not links:
                out.append(
                    Finding(
                        "RIGHTS LINK",
                        f"`{source}` serves {len(served)} capabilities with no `rights_rows:`, "
                        f"so the AC-1 gate cannot be traced from the baseline",
                    )
                )
            out.extend(
                Finding(
                    "RIGHTS LINK",
                    f"`{source}` names rights row `{link}`, which is not a 数据源 "
                    f"of the registration table",
                )
                for link in links
                if not any(link in name for name in registry)
            )

    upstream_rows = [row for row in rows if row.section == "providers"]
    actual_models: dict[str, list[str]] = {}
    for row in upstream_rows:
        parsed = parsed_rows.get((row.section, row.provider), {})
        value = parsed.get("models")
        models = (
            [item for item in value if isinstance(item, str)]
            if isinstance(value, list)
            else flow_list(row.fields.get("models", ""))
        )
        actual_models[row.provider] = models
        raw_models = row.fields.get("models", "")
        if "..." in raw_models or "..." in models:
            out.append(
                Finding(
                    "MODEL ELISION",
                    f"`{row.provider}` model list contains `...` and is not a complete "
                    "identity list",
                )
            )
        duplicate_models = sorted(model for model, count in Counter(models).items() if count > 1)
        if duplicate_models:
            out.append(
                Finding(
                    "MODEL DUPLICATE",
                    f"`{row.provider}` repeats upstream model names: {duplicate_models}",
                )
            )
        fetchers = row.fields.get("fetchers", "")
        if fetchers.isdigit() and int(fetchers) != len(models):
            out.append(
                Finding(
                    "MODEL COUNT",
                    f"`{row.provider}` fetchers is {fetchers}, but models lists "
                    f"{len(models)} names",
                )
            )
        model_count = row.fields.get("model_count")
        if model_count is not None and (
            not model_count.isdigit() or int(model_count) != len(models)
        ):
            out.append(
                Finding(
                    "MODEL COUNT",
                    f"`{row.provider}` model_count is {model_count}, but models lists "
                    f"{len(models)} names",
                )
            )

    if expected_models is not None:
        expected_providers = set(expected_models)
        actual_providers = set(actual_models)
        out.extend(
            Finding(
                "MODEL SET",
                f"provider `{provider}` is absent from one side of the fixed AST/model manifest",
            )
            for provider in sorted(expected_providers ^ actual_providers)
        )
        for provider in sorted(expected_providers & actual_providers):
            expected_names = list(expected_models[provider])
            actual_names = actual_models[provider]
            if set(actual_names) != set(expected_names):
                out.append(
                    Finding(
                        "MODEL SET",
                        f"`{provider}` model identities differ: "
                        f"missing={sorted(set(expected_names) - set(actual_names))}, "
                        f"extra={sorted(set(actual_names) - set(expected_names))}",
                    )
                )
        expected_count = sum(len(models) for models in expected_models.values())
        expected_unique_count = len(
            {model for models in expected_models.values() for model in models}
        )
        fixed_counts = declared_counts(inventory_text)
        if fixed_counts.get("upstream_model_count") != expected_count:
            out.append(
                Finding(
                    "MODEL COUNT",
                    f"upstream_model_count is {fixed_counts.get('upstream_model_count')}, "
                    f"fixed AST has {expected_count}",
                )
            )
        if fixed_counts.get("unique_upstream_model_count") != expected_unique_count:
            out.append(
                Finding(
                    "MODEL COUNT",
                    "unique_upstream_model_count is "
                    f"{fixed_counts.get('unique_upstream_model_count')}, "
                    f"fixed AST has {expected_unique_count}",
                )
            )

    if expected_credentials is not None:
        for row in upstream_rows:
            expected_credential_names = set(expected_credentials.get(row.provider, ()))
            parsed = parsed_rows.get((row.section, row.provider), {})
            value = parsed.get("credentials")
            actual_credential_names = (
                {item for item in value if isinstance(item, str)}
                if isinstance(value, list)
                else set(flow_list(row.fields.get("credentials", "")))
            )
            if actual_credential_names != expected_credential_names:
                out.append(
                    Finding(
                        "CREDENTIAL MISMATCH",
                        f"`{row.provider}` credentials differ from fixed AST: "
                        f"expected={sorted(expected_credential_names)}, "
                        f"actual={sorted(actual_credential_names)}",
                    )
                )

    counts = declared_counts(inventory_text)
    prose = header_prose(inventory_text)
    upstream = [row for row in rows if row.section == "providers"]
    local = [row for row in rows if row.section == "local_providers"]
    if prose and prose[0] != len(upstream):
        out.append(
            Finding(
                "COUNT CLAIM",
                f"header prose claims {prose[0]} provider 目录, the body has {len(upstream)}",
            )
        )
    if "provider_count" in counts and counts["provider_count"] != len(upstream):
        out.append(
            Finding(
                "COUNT CLAIM",
                f"provider_count: {counts['provider_count']}, the body has {len(upstream)}",
            )
        )
    if "local_provider_count" in counts and counts["local_provider_count"] != len(local):
        out.append(
            Finding(
                "COUNT CLAIM",
                f"local_provider_count: {counts['local_provider_count']}, "
                f"the body has {len(local)}",
            )
        )
    if prose:
        stated = sum(
            int(row.fields["fetchers"])
            for row in upstream
            if row.fields.get("fetchers", "").isdigit()
        )
        if prose[1] != stated:
            out.append(
                Finding(
                    "COUNT CLAIM",
                    f"header prose claims {prose[1]} fetcher 模型, the rows sum to {stated}",
                )
            )

    actual_model_count = sum(len(models) for models in actual_models.values())
    actual_unique_model_count = len(
        {model for models in actual_models.values() for model in models}
    )
    for field, expected_numeric in (
        ("upstream_model_count", actual_model_count),
        ("unique_upstream_model_count", actual_unique_model_count),
    ):
        declared_model_count = counts.get(field)
        if declared_model_count is not None and declared_model_count != expected_numeric:
            out.append(
                Finding(
                    "MODEL COUNT",
                    f"{field} is {declared_model_count}, "
                    f"but listed upstream models yield {expected_numeric}",
                )
            )

    actual_capability_count = sum(len(source_legs) for source_legs in legs.values())
    actual_auto_count = sum(1 for source_legs in legs.values() for _, auto in source_legs if auto)
    required_runtime_counts = {
        "registered_source_count": len(legs),
        "registered_capability_count": actual_capability_count,
        "auto_routable_capability_count": actual_auto_count,
    }
    for field, registry_count in required_runtime_counts.items():
        declared_runtime_count = counts.get(field)
        if declared_runtime_count is None and expected_models is not None:
            out.append(
                Finding("CAPABILITY COUNT", f"manifest is missing `{field}` runtime metadata")
            )
        elif declared_runtime_count is not None and declared_runtime_count != registry_count:
            out.append(
                Finding(
                    "CAPABILITY COUNT",
                    f"{field} is {declared_runtime_count}, "
                    f"but ProviderRegistry reports {registry_count}",
                )
            )
    for row in rows:
        source_legs = legs.get(row.provider, [])
        per_source_counts = {
            "registered_capability_count": len(source_legs),
            "auto_routable_capability_count": sum(1 for _, auto in source_legs if auto),
        }
        for field, registry_count in per_source_counts.items():
            declared_source_count = row.fields.get(field)
            if (
                declared_source_count is None
                and expected_models is not None
                and row.section == "providers"
            ):
                out.append(
                    Finding(
                        "CAPABILITY COUNT",
                        f"`{row.provider}` is missing `{field}` runtime metadata",
                    )
                )
            elif declared_source_count is not None and (
                not declared_source_count.isdigit() or int(declared_source_count) != registry_count
            ):
                out.append(
                    Finding(
                        "CAPABILITY COUNT",
                        f"`{row.provider}` {field} is {declared_source_count}, "
                        f"but ProviderRegistry reports {registry_count}",
                    )
                )

    if ledger_rows is not None:
        implementation_counts = Counter(
            str(row.get("implementation_task_status", "")) for row in ledger_rows
        )
        live_counts = Counter(str(row.get("live_verification_status", "")) for row in ledger_rows)
        scenario_counts = Counter(str(row.get("scenario_status", "")) for row in ledger_rows)
        expected_status_fields = {
            "model_implementation_task_status": _summary_status(
                [str(row.get("implementation_task_status", "")) for row in ledger_rows]
            ),
            "model_live_verification_status": _summary_status(
                [str(row.get("live_verification_status", "")) for row in ledger_rows]
            ),
        }
        try:
            document = yaml.safe_load(inventory_text)
        except yaml.YAMLError:
            document = {}
        if isinstance(document, dict):
            for field, expected_task_status in expected_status_fields.items():
                if document.get(field) != expected_task_status:
                    out.append(
                        Finding(
                            "MODEL TASK STATUS",
                            f"{field} is {document.get(field)}, "
                            f"CSV model tasks summarize to {expected_task_status}",
                        )
                    )
            expected_count_maps = {
                "model_implementation_task_status_counts": dict(implementation_counts),
                "model_live_verification_task_status_counts": dict(live_counts),
                "model_scenario_status_counts": dict(scenario_counts),
            }
            for field, expected_status_counts in expected_count_maps.items():
                if document.get(field) != expected_status_counts:
                    out.append(
                        Finding(
                            "MODEL TASK STATUS",
                            f"{field} is {document.get(field)}, "
                            f"CSV task counts are {expected_status_counts}",
                        )
                    )
        impl_by_provider: dict[str, list[str]] = {}
        live_by_provider: dict[str, list[str]] = {}
        scenario_by_provider: dict[str, list[str]] = {}
        for ledger_row in ledger_rows:
            provider = str(ledger_row.get("provider", ""))
            impl_by_provider.setdefault(provider, []).append(
                str(ledger_row.get("implementation_task_status", ""))
            )
            live_by_provider.setdefault(provider, []).append(
                str(ledger_row.get("live_verification_status", ""))
            )
            scenario_by_provider.setdefault(provider, []).append(
                str(ledger_row.get("scenario_status", ""))
            )
        for provider in sorted(impl_by_provider):
            parsed = parsed_rows.get(("providers", provider), {})
            expected_provider_status = {
                "implementation_task_status": _summary_status(impl_by_provider[provider]),
                "live_verification_status": _summary_status(live_by_provider.get(provider, [])),
                "model_scenario_status": _summary_status(scenario_by_provider.get(provider, [])),
            }
            for field, expected_provider_task_status in expected_provider_status.items():
                if parsed.get(field) != expected_provider_task_status:
                    out.append(
                        Finding(
                            "MODEL TASK STATUS",
                            f"`{provider}` {field} is {parsed.get(field)}, "
                            f"CSV tasks summarize to {expected_provider_task_status}",
                        )
                    )
        requester_counts: Counter[str] = Counter()
        for provider, models in actual_models.items():
            parsed = parsed_rows.get(("providers", provider), {})
            confirmation = parsed.get("requester_confirmation_status")
            if confirmation in {"CONFIRMED", "NOT_ASSESSED"}:
                requester_counts[str(confirmation)] += len(models)
            elif expected_models is not None:
                out.append(
                    Finding(
                        "REQUESTER STATUS",
                        f"`{provider}` is missing requester_confirmation_status metadata",
                    )
                )
        if isinstance(document, dict) and document.get(
            "model_requester_confirmation_status_counts"
        ) != dict(requester_counts):
            out.append(
                Finding(
                    "REQUESTER STATUS",
                    "model_requester_confirmation_status_counts does not match "
                    "per-source confirmation states",
                )
            )
    for row in rows:
        parsed = parsed_rows.get((row.section, row.provider), {})
        confirmation = parsed.get("requester_confirmation_status")
        explicit_confirmation = bool(
            parsed.get("requester")
            and parsed.get("requester_confirmed_date")
            and parsed.get("requester_evidence")
        )
        if confirmation not in {"CONFIRMED", "NOT_ASSESSED"}:
            if expected_models is not None:
                out.append(
                    Finding(
                        "REQUESTER STATUS",
                        f"`{row.provider}` has no valid requester confirmation state",
                    )
                )
        elif confirmation == "CONFIRMED" and not explicit_confirmation:
            out.append(
                Finding(
                    "REQUESTER STATUS",
                    f"`{row.provider}` is marked CONFIRMED without requester/date/evidence fields",
                )
            )
        elif confirmation == "NOT_ASSESSED" and (
            parsed.get("requester_confirmed_date") or parsed.get("requester_evidence")
        ):
            out.append(
                Finding(
                    "REQUESTER STATUS",
                    f"`{row.provider}` has confirmation evidence but remains NOT_ASSESSED",
                )
            )
    return out


def report(
    inventory_text: str,
    rights_text: str,
    legs: dict[str, list[tuple[str, bool]]],
    expected_models: Mapping[str, Sequence[str]] | None = None,
    expected_credentials: Mapping[str, Sequence[str]] | None = None,
    ledger_rows: Sequence[Mapping[str, object]] | None = None,
) -> int:
    """Print the tallies, the counters and every finding of one run.

    Args:
        inventory_text: Whole record contents.
        rights_text: Whole rights registry contents.
        legs: The registry-side legs.
        expected_models: Fixed provider/model identities, if loaded.
        expected_credentials: Fixed AST credential names, if loaded.
        ledger_rows: Model task rows, separate from runtime capability status.

    Returns:
        The process exit code: ``0`` with no judged finding, ``1`` with any.
    """
    rows = parse_rows(inventory_text)
    upstream = [row for row in rows if row.section == "providers"]
    local = [row for row in rows if row.section == "local_providers"]
    elided = model_elisions(rows)
    found = findings(
        inventory_text,
        rights_text,
        legs,
        expected_models,
        expected_credentials,
        ledger_rows,
    )

    sections = f"providers {len(upstream)} + local {len(local)}"
    print(f"record rows                  : {len(rows)} = {sections}")
    print(f"record statuses              : {sorted({r.fields.get('status', '') for r in rows})}")
    print(f"sources serving capabilities : {len(legs)} -> {', '.join(sorted(legs))}")
    print(
        f"capabilities registered      : {sum(len(v) for v in legs.values())} "
        f"({sum(1 for v in legs.values() for _, auto in v if auto)} auto-routable)"
    )
    print(f"rights registry §1 rows      : {len(rights_rows(rights_text))}")
    listed = sum(len(flow_list(row.fields.get("models", ""))) for row in rows)
    print(f"model names listed           : {listed}")
    print(f"rows with elided model lists : {len(elided)}  (judged)")
    print()
    print("== counters ==")
    for kind in KINDS:
        print(f"  {kind:14s} {sum(1 for f in found if f.kind == kind)}")
    print()
    print("== findings (judged) ==")
    for finding in found:
        print(f"  {finding}")
    if not found:
        print("  (none)")
    print()
    print("== model elision details (judged) ==")
    for line in elided:
        print(f"  MODEL ELISION: {line}")
    if not elided:
        print("  (none)")
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    """Print the provenance header, judge one record, and choose the exit code.

    Args:
        argv: Command-line overrides for the two documents being reconciled.

    Returns:
        ``0`` when the record agrees with the tree, ``1`` when any judged rule fires,
        ``2`` when the provider tree cannot be imported — the tree side is then unknown
        rather than empty, which is not a verdict about the record.
    """
    parser = argparse.ArgumentParser(
        prog="openbb_inventory_plane.py",
        description="Reconcile the OpenBB provider inventory with the live registry.",
    )
    parser.add_argument("--inventory", type=Path, default=INVENTORY)
    parser.add_argument("--rights", type=Path, default=RIGHTS_REGISTRY)
    parser.add_argument(
        "--upstream-path",
        type=Path,
        default=Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform"),
    )
    parser.add_argument(
        "--task-ledger",
        type=Path,
        default=REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv",
    )
    args = parser.parse_args(argv)

    def display(path: Path) -> str:
        """Render a path relative to the repo so the archive stays readable.

        Args:
            path: Any path involved in this run.

        Returns:
            The path as seen from the repository root, or the absolute path if it is not
            inside it.
        """
        try:
            return str(path.resolve().relative_to(REPO_ROOT))
        except ValueError:
            return str(path)

    print("C33 OpenBB provider inventory reconciliation — provider-inventory.yaml vs")
    print("opendata/data/providers + the live registry + docs/data-rights-registry.md")
    print(f"generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}")
    print(f"branch={git_text('branch', '--show-current')}")
    print(f"HEAD={git_text('rev-parse', '--short', 'HEAD')}")
    print(f"python={sys.version.split()[0]}")
    print("command: python scripts/quality/openbb_inventory_plane.py")
    print(f"           --inventory {display(args.inventory)} --rights {display(args.rights)}")
    print("           (static read + in-process registry; no network, no MySQL, no credential)")
    print("exit code: 0 = agrees with the tree; 1 = any judged finding; 2 = registry unavailable")
    print(f"rules judged ({len(KINDS)}): {', '.join(KINDS)}")
    print("           model identity is judged against the fixed upstream AST and task ledger")
    print()
    print("--- git status --porcelain captured before the run ---")
    print(git_text("status", "--porcelain") or "(clean)")
    print("--- end git status ---")
    print()
    try:
        legs = live_legs()
    except RuntimeError as exc:
        print(f"RUNTIME UNAVAILABLE: {exc}")
        return 2
    try:
        from scripts.quality.provider_model_inventory import (
            UPSTREAM_COMMIT,
            UpstreamUnavailableError,
            build_inventory_report,
            load_task_ledger,
        )

        static_report = build_inventory_report(args.upstream_path, args.task_ledger)
        if static_report["status"] != "PASS":
            print(f"FIXED AST INVENTORY {static_report['status']} at {UPSTREAM_COMMIT}")
            for issue in static_report["issues"]:
                print(f"  {issue}")
            return 2 if static_report["status"] == "NOT_AVAILABLE" else 1
        models_by_provider: dict[str, list[str]] = {}
        for model in static_report["reconstructed_models"]:
            models_by_provider.setdefault(model["provider"], []).append(model["model"])
        ledger_rows = load_task_ledger(args.task_ledger)
    except UpstreamUnavailableError as exc:
        print(f"FIXED AST INVENTORY NOT_AVAILABLE: {exc}")
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"FIXED AST INVENTORY FAIL: {exc}")
        return 1
    return report(
        args.inventory.read_text(encoding="utf-8"),
        args.rights.read_text(encoding="utf-8"),
        legs,
        models_by_provider,
        static_report["provider_credentials"],
        ledger_rows,
    )


if __name__ == "__main__":
    raise SystemExit(main())
