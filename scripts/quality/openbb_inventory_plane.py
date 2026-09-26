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
  * all 32 rows say ``待实现`` while 7 sources register 33 capabilities, 20 of which are
    auto-routable (the other 13 are registered, importable, and skipped by ``auto``);
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
    and still excluded from ``auto`` — which is where ``fred`` and ``akshare`` sit today.
  * ``COUNT CLAIM`` — declared counts and the header's prose numbers must equal the body.
  * ``RIGHTS LINK`` — a source that serves data must name its row in
    ``docs/data-rights-registry.md``; AC-1 gate R3 says an unregistered source may not be
    routed, so the baseline that tracks routing must be able to point at the registration.

``MODEL ELISION`` is reported and deliberately not judged: the upstream ``models: [...]``
lists are truncated with ``...`` in 15 rows, and an elided list that says so is honest. The
names were never written down, and this plane will not invent them.

The registry side is read in-process (import + ``register_providers()``); no network, no
MySQL, no credential is touched. Callers that want to judge a copy of the record — the
"before" archive and the falsification runs — pass ``--inventory``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import yaml

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
    r"^(?P<key>provider_count|local_provider_count):\s*(?P<value>\d+)\s*$", re.MULTILINE
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
        proc = subprocess.run(  # noqa: S603  # nosec B603
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
    register_providers()
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
    """Report the rows whose upstream model list is shorter than the count it states.

    Args:
        rows: The parsed record rows.

    Returns:
        One line per truncated row: ``provider: 6 listed of 36 stated``.
    """
    out: list[str] = []
    for row in rows:
        stated = row.fields.get("fetchers", "")
        if not stated.isdigit():
            continue
        listed = len(flow_list(row.fields.get("models", "")))
        if int(stated) > listed:
            out.append(f"{row.provider}: {listed} listed of {stated} stated")
    return out


def findings(
    inventory_text: str,
    rights_text: str,
    legs: dict[str, list[tuple[str, bool]]],
) -> list[Finding]:
    """Judge the record against the registry and the rights registry.

    Args:
        inventory_text: Whole ``provider-inventory.yaml`` contents.
        rights_text: Whole ``docs/data-rights-registry.md`` contents.
        legs: Registry-side legs, from :func:`live_legs` or a test fixture.

    Returns:
        Every violated rule, in report order. Empty means the record and the tree agree.
    """
    rows = parse_rows(inventory_text)
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
    return out


def report(inventory_text: str, rights_text: str, legs: dict[str, list[tuple[str, bool]]]) -> int:
    """Print the tallies, the counters and every finding of one run.

    Args:
        inventory_text: Whole record contents.
        rights_text: Whole rights registry contents.
        legs: The registry-side legs.

    Returns:
        The process exit code: ``0`` with no judged finding, ``1`` with any.
    """
    rows = parse_rows(inventory_text)
    upstream = [row for row in rows if row.section == "providers"]
    local = [row for row in rows if row.section == "local_providers"]
    elided = model_elisions(rows)
    found = findings(inventory_text, rights_text, legs)

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
    print(f"rows with elided model lists : {len(elided)}  (reported, not judged)")
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
    print("== reported, not judged ==")
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
    print("           MODEL ELISION is reported but not judged")
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
    return report(
        args.inventory.read_text(encoding="utf-8"), args.rights.read_text(encoding="utf-8"), legs
    )


if __name__ == "__main__":
    raise SystemExit(main())
