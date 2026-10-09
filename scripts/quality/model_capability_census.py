"""Turn the model-shape census into an anchored capability roadmap.

The three census files describe every remaining model slice in free text, which makes them
unreadable as a build plan: 96 distinct ``needs_engine_capability`` label strings for 150 rows, with
synonyms (``client_side_filter`` / ``client_side_row_filter`` / ``client_side_date_filter``) and
near-neighbours (``multi_request_join`` / ``cross_request_join`` / ``multi_endpoint_join``) counted
apart. Worse, the labels were never checked against the engine: ``csv_decoder`` blocks rows as a
missing capability while delimited bodies have been decodable since this round's
:mod:`opendata.data.providers._engine.decoders`. A roadmap over those labels is a roadmap over
wording, so this instrument replaces the wording with three measured faces.

* **the taxonomy** -- every label maps onto exactly one canonical capability, and loading fails
  closed naming any label with no mapping. That refusal is what stops the taxonomy rotting; the
  registry must cover exactly the set of canonical ids the labels produce, in both directions.
* **the anchored capability registry** -- for each canonical capability, whether the engine has it
  *today*, proved by a literal token in a repo file this run reads (plus a second cross-check file
  for the claims that have one). A claim whose token is gone is reported ``drifted``, never silently
  kept. A claim that cannot be anchored to a token is registered ``unanchored`` and printed as
  unproven: that is the honest state, not ``present``. One entry (``rows.filter``) has its presence
  read straight off the disk every run, so whoever ships the client-side row filter flips the
  roadmap without an edit here.
* **the built-but-unused face** -- a capability counts as exercised only when a *production provider
  module* declares it. The engine package defines these declaration classes, so its own bytes are
  not evidence that any model uses them; the scan covers ``opendata/data/providers/**/*.py`` minus
  ``_engine/`` and minus the vendored ``_vendor`` tree, and never counts ``tests/``. A capability
  that is present, anchored and declared zero times is flagged ``built_but_unused``.

The roadmap is built from need *sets*, not frequencies: a row is unlocked only when its whole
canonical need set is covered, so the greedy prefix proposes the capability that unlocks the most
remaining rows at each step, names the provider+model rows it unlocked (so a build order can be
checked against the ledger rather than argued with a number), and leaves the rows no engine work can
reach in two explicit lists. Nothing here executes the engine, replays a fixture, or opens a socket:
an entry is anchored to source text, which is the only presence claim this round can support without
a live response.

Fail-closed, in this order: a census directory with no files, a payload whose ``rows`` is not a
list, a row without a ``task_id``, a duplicated ``task_id``, an unmapped label, a registry whose
keys are not exactly the canonical set, a provider module that will not parse. The row-count guard
(150) and a drifted anchor are printed in every mode and additionally force a non-zero exit under
``--check``, where the message names the entry that moved instead of raising a bare traceback.

CLI::

    python scripts/quality/model_capability_census.py
    python scripts/quality/model_capability_census.py --json docs/.../capability-roadmap.json
    python scripts/quality/model_capability_census.py --check --round-id C75
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

_REPO_ROOT = Path(__file__).resolve().parents[2]

CENSUS_DIR_REL: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
CENSUS_GLOB: Final = "census-*.json"
#: The audit record shares the glob but carries no rows; only its exact name is set aside, so any
#: other ``census-*.json`` without a rows list still aborts the run instead of being skipped.
CENSUS_AUDIT_NAME: Final = "census-audit.json"
CENSUS_CARRIED_FILES: Final = (
    "census-imf-oecd-famafrench-misc.json",
    "census-sec-tmx-fed-gov-finra.json",
    "census-yfinance-cboe-finviz.json",
)
ROADMAP_REL: Final = f"{CENSUS_DIR_REL}/capability-roadmap.json"
EXPECTED_CENSUS_ROWS: Final = 150
DEFAULT_ROUND_ID: Final = "C74"
SCHEMA_VERSION: Final = 1

#: Where production declarations live, and the trees that are not production provider code.
PROVIDER_ROOT_REL: Final = "opendata/data/providers"
DECLARATION_EXCLUDED_PARTS: Final = ("_engine", "_vendor", "__pycache__")

DECODERS_REL: Final = f"{PROVIDER_ROOT_REL}/_engine/decoders.py"
HTTP_JSON_REL: Final = f"{PROVIDER_ROOT_REL}/_engine/http_json.py"
SPEC_REL: Final = f"{PROVIDER_ROOT_REL}/_engine/spec.py"
SEC_SPECS_REL: Final = f"{PROVIDER_ROOT_REL}/sec/specs.py"

Status = Literal["present", "absent", "drifted", "unanchored"]
_STATUSES: Final[tuple[Status, ...]] = ("present", "absent", "drifted", "unanchored")

# label -> canonical capability. Complete over the shipped census; :func:`load_rows` fails closed
# on anything absent here, so a census that grows a new kind of blocker has to say so in this map.
LABEL_TO_CAPABILITY: Final[dict[str, str]] = {
    # ---- response decoding -------------------------------------------------
    "csv_decoder": "decode.delimited",
    "csv_delimiter": "decode.delimited",
    "csv_header_offset": "decode.delimited",
    "delimited_rows_without_published_header": "decode.delimited",
    "xml_decoder": "decode.xml",
    "rss_xml_decoder": "decode.xml",
    "xlsx_decoder": "decode.tabular_file",
    "sheet_region_parse": "decode.tabular_file",
    "pdf_bytes_passthrough": "decode.opaque_bytes",
    "zip_member_decoder": "decode.archive_member",
    "html_table_decoder": "decode.html_table",
    "html_to_markdown": "decode.html_text",
    "regex_strip_html": "decode.html_text",
    "table_text_parser": "decode.text_table",
    "regex_row_parse": "decode.text_table",
    "geojson_attribute_rows": "decode.geojson",
    "sdmx_json_decoder": "shape.nested_data_message",
    # ---- locating the record list -----------------------------------------
    "sdmx_dotted_key_path": "path.dotted_pointer",
    "parameterized_rows_pointer": "path.dotted_pointer",
    "rows_from_object_keys": "path.object_keys",
    "response_dict_keying": "path.object_keys",
    "object_graph_flatten": "path.object_keys",
    "per_record_nested_flatten": "path.explode",
    "nested_array_explode": "path.explode",
    "series_pivot": "path.explode",
    "single_row_assembly": "path.assemble_single_row",
    "columnar_parallel_arrays": "shape.columnar",
    "columnar_to_rows": "shape.columnar",
    "columnar_output_shape": "shape.columnar",
    "columnar_list_rows": "shape.columnar",
    "array_rows_with_field_header": "shape.columnar",
    "groupby_first_column": "shape.columnar",
    # ---- the request itself -------------------------------------------------
    "post_body": "request.body",
    "post_request_body": "request.body",
    "structured_filter_body": "request.body",
    "body_offset_pagination": "request.body_pagination",
    "graphql_query_template": "request.graphql",
    "encoded_json_query_param": "request.serialised_param",
    "query_key_date_mapping": "request.serialised_param",
    # ---- more than one request ---------------------------------------------
    "multi_request_fanout": "flow.fan_out",
    "per_item_fanout": "flow.fan_out",
    "symbol_fan_out": "flow.fan_out",
    "multi_request_concat": "flow.fan_out",
    "multi_request_join": "flow.join",
    "cross_request_join": "flow.join",
    "multi_endpoint_join": "flow.join",
    "cross_key_join": "flow.join",
    "cross_request_lookup": "flow.join",
    "prior_period_lookup": "flow.join",
    # ---- knowing the address before asking ---------------------------------
    "lookup_preflight": "flow.preflight_lookup",
    "preflight_directory_lookup": "flow.preflight_lookup",
    "preflight_url_discovery": "flow.preflight_lookup",
    "dataset_path_lookup": "flow.preflight_lookup",
    "caller_url_list": "flow.preflight_lookup",
    "caller_url_template": "flow.url_template",
    "branching_base_url": "flow.url_template",
    "exchange_path_template": "flow.url_template",
    "conditional_endpoint_branch": "flow.branch",
    "interval_branch": "flow.branch",
    "metric_branch": "flow.branch",
    # ---- post-request row surgery ------------------------------------------
    "client_side_filter": "rows.filter",
    "client_side_row_filter": "rows.filter",
    "client_side_date_filter": "rows.filter",
    "date_window_filter": "rows.filter",
    "preset_filter_merge": "rows.filter",
    "sentinel_value_nulling": "rows.filter",
    "client_side_sort": "rows.order_limit",
    "client_side_sort_limit": "rows.order_limit",
    "derived_columns": "columns.derive",
    "derived_url_columns": "columns.derive",
    "regex_column_derivation": "columns.derive",
    "symbol_regex_parse": "columns.derive",
    "epoch_ms_to_date_conversion": "columns.cast",
    "date_format": "columns.cast",
    "percent_column_scaling": "columns.rescale",
    "published_value_rescale": "columns.rescale",
    "strike_scaling": "columns.rescale",
    "tag_column_selection": "columns.select",
    "xbrl_tag_assembly": "columns.select",
    "field_rename_map": "columns.rename",
    "param_value_mapping": "columns.rename",
    "symbol_normalization": "columns.rename",
    "hardcoded_symbol_roster": "columns.rename",
    "column_drop": "columns.select",
    "column_truncation": "columns.select",
    "client_side_column_drop": "columns.select",
    # ---- not an engine capability at all -----------------------------------
    "sdk_delegation": "outside.sdk",
    "html_scraping_sdk": "outside.sdk",
    "fetcher_delegation": "outside.bespoke",
    "fetcher_composition": "outside.bespoke",
    "alias_model": "outside.sdk",
    "static_no_http_source": "outside.no_request",
    "vendored_upstream_source_missing": "outside.source_absent",
    "local_cache_store": "outside.stateful",
    "session_cookie_warmup": "outside.stateful",
    "anti_bot_retry": "outside.anti_bot",
}


class RoadmapError(RuntimeError):
    """A census, taxonomy, or declaration-scan precondition did not hold."""


@dataclass(frozen=True)
class DeclarationProbe:
    """What a provider declaration would have to say for this capability to count as exercised.

    Attributes:
        call: Declaration class whose call sites are counted (``ModelSpec``, ``ColumnSpec``, ...).
        keyword: Keyword the call must set; empty means every call site of ``call`` counts.
        value_contains: Literal that must appear in that keyword's source text.
        non_empty: Whether an empty literal value (``""``, ``()``, ``0``, ``None``) fails to
            exercise the capability -- ``rows_pointer=""`` names a field without using it.
    """

    call: str
    keyword: str = ""
    value_contains: str = ""
    non_empty: bool = False


@dataclass(frozen=True)
class CapabilityEntry:
    """One canonical capability and the claim this instrument makes about the engine.

    Attributes:
        capability: Canonical id -- exactly one value of :data:`LABEL_TO_CAPABILITY`.
        claim: The asserted presence. Ignored when ``read_from_disk`` is set.
        engine_work: Whether building it is engine work at all; the ``outside.*`` entries are not.
        proof_file: Repo-relative file the claim is checked against; empty means unanchored.
        anchor: Literal token that must still be found in ``proof_file``.
        why: One line saying what that anchor actually proves.
        read_from_disk: Whether presence is *measured* from the anchor instead of asserted, so a
            feature someone else ships flips the face without an edit in this file.
        cross_file: Second file whose ``cross_anchor`` must also hold.
        cross_anchor: Token proving the claim from the declaration side as well as the code side.
        probe: Declaration shape a provider must emit for the capability to be exercised.
    """

    capability: str
    claim: bool
    engine_work: bool
    proof_file: str
    anchor: str
    why: str
    read_from_disk: bool = False
    cross_file: str = ""
    cross_anchor: str = ""
    probe: DeclarationProbe | None = None


@dataclass(frozen=True)
class EntryReading:
    """One registry entry as this run read it, with the anchor line that decided it."""

    capability: str
    status: Status
    present: bool
    engine_work: bool
    claim: bool
    read_from_disk: bool
    proof_file: str
    anchor: str
    anchor_found: bool
    anchor_line: str
    anchor_line_number: int
    cross_file: str
    cross_anchor: str
    cross_anchor_found: bool
    why: str
    production_declarations: int

    def to_json(self) -> dict[str, object]:
        """Render the claim and the evidence for it, in a fixed key order."""
        return {
            "anchor": self.anchor,
            "anchor_found": self.anchor_found,
            "anchor_line": self.anchor_line,
            "anchor_line_number": self.anchor_line_number,
            "claim": self.claim,
            "cross_anchor": self.cross_anchor,
            "cross_anchor_found": self.cross_anchor_found,
            "cross_file": self.cross_file,
            "engine_work": self.engine_work,
            "present": self.present,
            "production_declarations": self.production_declarations,
            "proof_file": self.proof_file,
            "read_from_disk": self.read_from_disk,
            "status": self.status,
            "why": self.why,
        }


@dataclass(frozen=True)
class CensusRow:
    """One model slice, its raw wording kept beside the canonical ids it maps to."""

    task_id: str
    provider: str
    upstream_model: str
    raw_labels: tuple[str, ...]
    needs: frozenset[str]
    expressible_today: bool


@dataclass(frozen=True)
class RowIdentity:
    """A census row as the roadmap names it: ledger id, provider+model, and its need set."""

    task_id: str
    provider: str
    upstream_model: str
    needs: tuple[str, ...]

    @property
    def name(self) -> str:
        """The label used by the printed face."""
        return f"{self.provider}/{self.upstream_model}"

    def to_json(self) -> dict[str, object]:
        """Render the identity in a fixed key order."""
        return {
            "needs": list(self.needs),
            "provider": self.provider,
            "task_id": self.task_id,
            "upstream_model": self.upstream_model,
        }


@dataclass(frozen=True)
class BuildStep:
    """One greedy step and the rows it is claimed to unlock."""

    capability: str
    rows_newly_unlocked: int
    cumulative_rows_unlocked: int
    newly_unlocked_rows: tuple[RowIdentity, ...]

    def to_json(self) -> dict[str, object]:
        """Render the step in a fixed key order."""
        return {
            "capability": self.capability,
            "cumulative_rows_unlocked": self.cumulative_rows_unlocked,
            "newly_unlocked_rows": [row.to_json() for row in self.newly_unlocked_rows],
            "rows_newly_unlocked": self.rows_newly_unlocked,
        }


@dataclass(frozen=True)
class Roadmap:
    """The set-cover face, including the two lists that stop anyone over-claiming."""

    rows_pending: int
    rows_expressible_today: int
    shipped_cover: tuple[RowIdentity, ...]
    build_order: tuple[BuildStep, ...]
    rows_unlocked_by_greedy_prefix: int
    rows_still_locked_after_engine_pool: tuple[RowIdentity, ...]
    blocking_non_engine: tuple[str, ...]
    rows_outside_engine: tuple[RowIdentity, ...]
    rows_with_no_engine_work_possible: int
    rows_with_empty_need_set: tuple[RowIdentity, ...]

    def to_json(self) -> dict[str, object]:
        """Render the roadmap in a fixed key order."""
        return {
            "blocking_non_engine_capabilities": list(self.blocking_non_engine),
            "greedy_build_order": [step.to_json() for step in self.build_order],
            "rows_expressible_today": self.rows_expressible_today,
            "rows_outside_engine": [row.to_json() for row in self.rows_outside_engine],
            "rows_pending": self.rows_pending,
            "rows_still_locked_after_engine_pool": [
                row.to_json() for row in self.rows_still_locked_after_engine_pool
            ],
            "rows_unlocked_by_greedy_prefix": self.rows_unlocked_by_greedy_prefix,
            "rows_with_empty_need_set": [row.to_json() for row in self.rows_with_empty_need_set],
            "rows_with_no_engine_work_possible": self.rows_with_no_engine_work_possible,
            "shipped_cover_rows": [row.to_json() for row in self.shipped_cover],
        }


@dataclass(frozen=True)
class Report:
    """Everything this run measured, kept typed so the JSON and the face cannot disagree."""

    round_id: str
    census_files: tuple[str, ...]
    row_count: int
    unique_task_ids: int
    raw_label_count: int
    raw_mention_count: int
    labels_supported: int
    canonical_count: int
    mentions: dict[str, int]
    rows_needing_only_this: dict[str, int]
    readings: dict[str, EntryReading]
    declarations: dict[str, int]
    declarations_by_file: dict[str, dict[str, int]]
    modules_scanned: int
    roadmap: Roadmap

    @property
    def statuses(self) -> dict[str, tuple[str, ...]]:
        """Capability ids grouped by how this run read them, each group sorted."""
        return {
            status: tuple(
                sorted(
                    capability
                    for capability, reading in self.readings.items()
                    if reading.status == status
                )
            )
            for status in _STATUSES
        }

    @property
    def built_but_unused(self) -> tuple[str, ...]:
        """Present, anchored capabilities that no production declaration exercises."""
        return tuple(
            sorted(
                capability
                for capability, reading in self.readings.items()
                if reading.status == "present" and reading.production_declarations == 0
            )
        )

    @property
    def row_count_guard_fired(self) -> bool:
        """Whether the census still has the audited 150 rows."""
        return self.row_count != EXPECTED_CENSUS_ROWS

    @property
    def failures(self) -> tuple[str, ...]:
        """What ``--check`` refuses, each naming the entry rather than raising a traceback."""
        drifted = ", ".join(self.statuses["drifted"])
        messages: list[str] = []
        if self.statuses["drifted"]:
            messages.append(f"registry anchors drifted, so these claims are unproven: {drifted}")
        if self.row_count_guard_fired:
            messages.append(
                f"census row count is {self.row_count}, expected {EXPECTED_CENSUS_ROWS}; "
                "either the census changed or a file is missing"
            )
        return tuple(messages)

    def to_json(self) -> dict[str, object]:
        """Render the roadmap document: sorted everywhere, and no timestamp in any field."""
        return {
            "built_but_unused": list(self.built_but_unused),
            "capability_registry": {
                capability: self.readings[capability].to_json()
                for capability in sorted(self.readings)
            },
            "census": {
                "carried_forward_files": list(CENSUS_CARRIED_FILES),
                "files": list(self.census_files),
                "row_count_expected": EXPECTED_CENSUS_ROWS,
                "row_count_guard_fired": self.row_count_guard_fired,
                "row_count_measured": self.row_count,
                "unique_task_ids": self.unique_task_ids,
            },
            "check": {
                "drifted_capabilities": list(self.statuses["drifted"]),
                "failures": list(self.failures),
                "unanchored_capabilities": list(self.statuses["unanchored"]),
            },
            "declaration_scan": {
                "exclude_parts": list(DECLARATION_EXCLUDED_PARTS),
                "modules_scanned": self.modules_scanned,
                "production_declarations_by_file": {
                    capability: dict(sorted(files.items()))
                    for capability, files in sorted(self.declarations_by_file.items())
                    if files
                },
                "production_declarations_per_capability": {
                    capability: self.declarations[capability]
                    for capability in sorted(self.declarations)
                },
                "provider_root": PROVIDER_ROOT_REL,
            },
            "method_note": (
                "Measured this round: the census rows were read from the census-*.json files, "
                "every needs_engine_capability label was mapped through LABEL_TO_CAPABILITY (an "
                "unmapped label aborts the run), each registry claim was checked by reading its "
                "proof_file for its anchor token, and production declaration counts came from an "
                f"AST scan of {PROVIDER_ROOT_REL} minus _engine/ and minus _vendor/. Carried "
                "forward from the census files: provider, upstream_model, expressible_today and "
                "the need lists themselves. Carried forward by construction: the label map and "
                "every anchor string. No engine code was executed, no recorded fixture was "
                "replayed, and no request was made; a capability this round cannot claim more "
                "than that its source text exists."
            ),
            "roadmap": self.roadmap.to_json(),
            "round_id": self.round_id,
            "schema_version": SCHEMA_VERSION,
            "taxonomy": {
                "canonical_capability_count": self.canonical_count,
                "labels_supported": self.labels_supported,
                "mentions_per_capability": {
                    capability: self.mentions[capability] for capability in sorted(self.mentions)
                },
                "raw_label_count": self.raw_label_count,
                "raw_label_mention_count": self.raw_mention_count,
                "rows_needing_only_this_capability": {
                    capability: self.rows_needing_only_this[capability]
                    for capability in sorted(self.rows_needing_only_this)
                },
                "statuses": {
                    status: list(capabilities)
                    for status, capabilities in sorted(self.statuses.items())
                },
            },
        }

    def render_face(self, check_mode: bool) -> str:
        """Render the human-readable face, including every drift and unproven claim."""
        lines: list[str] = [f"capability roadmap -- round {self.round_id}"]
        lines.append(
            f"census: {self.row_count} rows / {self.unique_task_ids} unique task_ids from "
            f"{len(self.census_files)} files (expected {EXPECTED_CENSUS_ROWS})"
        )
        lines.append(
            f"taxonomy: {self.raw_label_count} distinct raw labels, {self.raw_mention_count} "
            f"mentions -> {self.canonical_count} canonical capabilities "
            f"({self.labels_supported} label spellings accepted)"
        )
        roadmap = self.roadmap
        lines.append(
            f"rows: expressible today {roadmap.rows_expressible_today}, pending "
            f"{roadmap.rows_pending}; pending rows whose whole need set is already shipped: "
            f"{len(roadmap.shipped_cover)}"
        )
        lines.append("")
        lines.append("capability registry (presence read from the proof file this run)")
        for status in _STATUSES:
            members = self.statuses[status]
            lines.append(f"  {status} ({len(members)}):")
            for capability in members:
                reading = self.readings[capability]
                if status == "unanchored":
                    lines.append(f"    - {capability}: UNPROVEN - no anchor registered")
                    lines.append(f"        why: {reading.why}")
                    continue
                evidence = (
                    f"{reading.proof_file}:{reading.anchor_line_number} anchor {reading.anchor!r}"
                    if reading.anchor_found
                    else f"anchor {reading.anchor!r} NOT FOUND in {reading.proof_file}"
                )
                flag = "  [read from disk]" if reading.read_from_disk else ""
                lines.append(f"    - {capability}: {evidence}{flag}")
                if reading.cross_anchor:
                    lines.append(
                        f"        cross-check: {reading.cross_file} "
                        f"{reading.cross_anchor!r} -> "
                        f"{'found' if reading.cross_anchor_found else 'MISSING'}"
                    )
                lines.append(f"        anchor line: {reading.anchor_line or '(none)'}")
                lines.append(f"        why: {reading.why}")
        lines.append("")
        lines.append(
            "built_but_unused (present and anchored, zero production declarations exercise it)"
        )
        if self.built_but_unused:
            lines.extend(f"  - {capability}" for capability in self.built_but_unused)
        else:
            lines.append("  (none)")
        nonzero = {key: value for key, value in self.declarations.items() if value}
        lines.append(
            f"declaration scan: {self.modules_scanned} modules under {PROVIDER_ROOT_REL} minus "
            f"{'/'.join(DECLARATION_EXCLUDED_PARTS)}; non-zero counts: "
            f"{json.dumps(nonzero, sort_keys=True)}"
        )
        lines.append("")
        lines.append("roadmap (greedy subset cover over present=false engine capabilities)")
        lines.append(f"  step 0  shipped already, covers {len(roadmap.shipped_cover)} pending rows")
        lines.extend(
            f"      - {row.name} needs {', '.join(row.needs)}" for row in roadmap.shipped_cover
        )
        for index, step in enumerate(roadmap.build_order, start=1):
            lines.append(
                f"  step {index}  {step.capability} -> +{step.rows_newly_unlocked} newly "
                f"unlocked, cumulative {step.cumulative_rows_unlocked}"
            )
            lines.extend(
                f"      - {row.name} [{', '.join(row.needs)}]" for row in step.newly_unlocked_rows
            )
        lines.append(
            f"  rows unlocked once the pool is exhausted: "
            f"{roadmap.rows_unlocked_by_greedy_prefix} of {roadmap.rows_pending} pending"
        )
        lines.append("")
        lines.append(
            f"over-claim guard 1 -- pending rows whose needs include a non-engine capability "
            f"({len(roadmap.rows_outside_engine)}):"
        )
        lines.extend(
            f"  - {row.name} [{', '.join(row.needs)}] (task_id {row.task_id})"
            for row in roadmap.rows_outside_engine
        )
        lines.append(
            f"  of those, rows with no engine work possible at all: "
            f"{roadmap.rows_with_no_engine_work_possible}"
        )
        lines.append(
            f"over-claim guard 2 -- rows still locked after the whole engine pool "
            f"({len(roadmap.rows_still_locked_after_engine_pool)}):"
        )
        lines.extend(
            f"  - {row.name} [{', '.join(row.needs)}]"
            for row in roadmap.rows_still_locked_after_engine_pool
        )
        lines.append(f"  blocking non-engine capabilities: {list(roadmap.blocking_non_engine)}")
        drifted = self.statuses["drifted"]
        unanchored = self.statuses["unanchored"]
        lines.append("")
        if drifted:
            lines.append(f"DRIFT: {len(drifted)} entries whose anchor is gone: {list(drifted)}")
        if unanchored:
            lines.append(f"UNPROVEN: {len(unanchored)} entries carry no anchor: {list(unanchored)}")
        if not drifted and not unanchored:
            lines.append("no drifted and no unanchored registry entries")
        lines.extend(f"GUARD: {message}" for message in self.failures)
        if check_mode:
            lines.append(f"--check verdict: {'FAIL' if self.failures else 'PASS'}")
        return "\n".join(lines)


def canonical_capabilities() -> frozenset[str]:
    """Return every canonical id the label map can produce."""
    return frozenset(LABEL_TO_CAPABILITY.values())


def _display(path: Path) -> str:
    """Name an input by its repo-relative path so the archive stays machine-independent."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read_text(path: Path) -> str:
    """Read UTF-8 source text, naming the file when it is not readable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoadmapError(f"{path} could not be read as UTF-8 text: {exc}") from exc


def _check_registry(entries: tuple[CapabilityEntry, ...]) -> None:
    """Refuse a registry that is not exactly the taxonomy: no gap, no orphan, no duplicate.

    Raises:
        RoadmapError: A canonical capability has no entry, an entry maps to no label, or an id is
            registered twice.
    """
    ids = [entry.capability for entry in entries]
    duplicates = sorted({capability for capability in ids if ids.count(capability) > 1})
    if duplicates:
        raise RoadmapError(f"registry registers a capability twice: {duplicates}")
    canonical = canonical_capabilities()
    missing = sorted(canonical - set(ids))
    if missing:
        raise RoadmapError(f"registry has no entry for canonical capabilities: {missing}")
    extra = sorted(set(ids) - canonical)
    if extra:
        raise RoadmapError(f"registry names capabilities no census label maps to: {extra}")


def _raw_labels(raw: object, task_id: str) -> tuple[str, ...]:
    """Return one row's raw ``needs_engine_capability`` spellings in census order."""
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, dict):
        return tuple(str(item) for item in raw)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    raise RoadmapError(f"row {task_id} has no needs_engine_capability list")


def map_needs(labels: tuple[str, ...], task_id: str) -> frozenset[str]:
    """Map one census row's free-text needs onto canonical ids, failing closed on wording.

    Raises:
        RoadmapError: Any label is absent from :data:`LABEL_TO_CAPABILITY`.
    """
    unmapped = sorted({label for label in labels if label not in LABEL_TO_CAPABILITY})
    if unmapped:
        raise RoadmapError(f"unmapped census labels on {task_id}: {unmapped}")
    return frozenset(LABEL_TO_CAPABILITY[label] for label in labels)


def census_row_files(census_dir: Path) -> list[Path]:
    """Return the census files that carry model rows, in path order.

    Only :data:`CENSUS_AUDIT_NAME` is set aside by exact name; a payload of its own shape belongs to
    the audit plane, not to the roadmap. Every other match has to provide a rows list or loading
    fails closed, so a census file added beside these three cannot go unread.
    """
    return [path for path in sorted(census_dir.glob(CENSUS_GLOB)) if path.name != CENSUS_AUDIT_NAME]


def load_rows(census_dir: Path) -> list[CensusRow]:
    """Read every census file in path order and return the mapped rows.

    Raises:
        RoadmapError: No census file, a payload whose ``rows`` is not a list, a row that is not an
            object or has no ``task_id``, a duplicated ``task_id``, or an unmapped label.
    """
    files = census_row_files(census_dir)
    if not files:
        raise RoadmapError(f"no {CENSUS_GLOB} files under {census_dir}")
    rows: list[CensusRow] = []
    for path in files:
        try:
            payload: object = json.loads(_read_text(path))
        except json.JSONDecodeError as exc:
            raise RoadmapError(f"{_display(path)} is not JSON: {exc.msg}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise RoadmapError(f"{_display(path)} has no rows list")
        for item in payload["rows"]:
            if not isinstance(item, dict):
                raise RoadmapError(f"{_display(path)} has a row that is not an object")
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise RoadmapError(f"{_display(path)} has a row without a task_id")
            labels = _raw_labels(item.get("needs_engine_capability"), task_id)
            rows.append(
                CensusRow(
                    task_id=task_id,
                    provider=str(item.get("provider", "")),
                    upstream_model=str(item.get("upstream_model", "")),
                    raw_labels=labels,
                    needs=map_needs(labels, task_id),
                    expressible_today=item.get("expressible_today") is True,
                )
            )
    counts = Counter(row.task_id for row in rows)
    duplicated = sorted(task_id for task_id, total in counts.items() if total > 1)
    if duplicated:
        raise RoadmapError(f"census task_ids are not unique: {duplicated[:5]}")
    return rows


def _anchor_line(root: Path, relative: str, anchor: str) -> tuple[int, str]:
    """Return the 1-based line number and stripped text of ``anchor``, or ``(0, "")`` if absent."""
    if not relative or not anchor:
        return 0, ""
    path = root / PurePosixPath(relative)
    if not path.is_file():
        return 0, ""
    for number, line in enumerate(_read_text(path).splitlines(), start=1):
        if anchor in line:
            return number, line.strip()
    return 0, ""


def provider_modules(root: Path) -> list[Path]:
    """Return the production provider modules a declaration may live in.

    ``tests/`` is out by construction (the walk starts at the provider root); ``_engine`` is pruned
    because it *defines* these declaration classes, and the vendored ``_vendor`` tree because it is
    upstream code, not a declaration of this repo's models.
    """
    base = root / PROVIDER_ROOT_REL
    if not base.is_dir():
        raise RoadmapError(f"provider root does not exist: {base}")
    modules: list[Path] = []
    for path in sorted(base.rglob("*.py")):
        relative = path.relative_to(base)
        if any(part in DECLARATION_EXCLUDED_PARTS for part in relative.parts):
            continue
        modules.append(path)
    return modules


def _call_name(node: ast.expr) -> str:
    """Return the trailing name of a call target, so ``spec.ModelSpec(...)`` matches too."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_empty_literal(node: ast.expr) -> bool:
    """Whether a literal value says nothing: an empty string, container, zero or None."""
    if isinstance(node, ast.Constant):
        return node.value in ("", 0, False, None)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return False


def _probe_matches(probe: DeclarationProbe, node: ast.Call, source: str) -> bool:
    """Whether one call site exercises the capability ``probe`` describes."""
    if _call_name(node.func) != probe.call:
        return False
    if not probe.keyword:
        return True
    for keyword in node.keywords:
        if keyword.arg != probe.keyword:
            continue
        value = keyword.value
        if probe.non_empty and _is_empty_literal(value):
            return False
        segment = ast.get_source_segment(source, value) or ""
        return not probe.value_contains or probe.value_contains in segment
    return False


def count_declarations(
    entries: tuple[CapabilityEntry, ...], root: Path
) -> tuple[dict[str, int], dict[str, dict[str, int]], int]:
    """Count, per capability, the production declarations that match its probe.

    Returns:
        The totals for every capability (0 where no probe exists, because the capability has no
        declaration surface yet), the per-file breakdown for the probes that matched, and the number
        of modules scanned.

    Raises:
        RoadmapError: A provider module does not parse -- a declaration count read from a broken
            tree would be a count of nothing.
    """
    scoped = [entry for entry in entries if entry.probe is not None]
    totals: dict[str, int] = {entry.capability: 0 for entry in entries}
    per_file: dict[str, dict[str, int]] = {entry.capability: {} for entry in scoped}
    modules = provider_modules(root)
    for path in modules:
        source = _read_text(path)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise RoadmapError(f"{path} does not parse: {exc.msg}") from exc
        relative = PurePosixPath(path.relative_to(root)).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for entry in scoped:
                probe = entry.probe
                if probe is not None and _probe_matches(probe, node, source):
                    totals[entry.capability] += 1
                    bucket = per_file[entry.capability]
                    bucket[relative] = bucket.get(relative, 0) + 1
    return totals, per_file, len(modules)


def verify_registry(
    entries: tuple[CapabilityEntry, ...],
    root: Path,
    declarations: dict[str, int],
) -> dict[str, EntryReading]:
    """Read every proof file and decide each claim from the bytes on disk right now.

    An entry with no proof file is ``unanchored`` and never claims presence. An entry flagged
    ``read_from_disk`` is present exactly when its anchor is found. Anything else keeps the claim
    only while its anchor (and cross-anchor) is still there; if the token is gone the entry is
    ``drifted`` -- the claim is dropped rather than trusted.
    """
    readings: dict[str, EntryReading] = {}
    for entry in entries:
        cross_number, _ = _anchor_line(root, entry.cross_file, entry.cross_anchor)
        cross_found = cross_number > 0
        status: Status
        present: bool
        if not entry.proof_file or not entry.anchor:
            status, present = "unanchored", False
            number, line, found = 0, "", False
        else:
            number, line = _anchor_line(root, entry.proof_file, entry.anchor)
            found = number > 0 and (not entry.cross_anchor or cross_found)
            if entry.read_from_disk:
                status, present = ("present", True) if found else ("absent", False)
            elif found:
                status, present = ("present", True) if entry.claim else ("absent", False)
            else:
                status, present = "drifted", False
        readings[entry.capability] = EntryReading(
            capability=entry.capability,
            status=status,
            present=present,
            engine_work=entry.engine_work,
            claim=entry.claim,
            read_from_disk=entry.read_from_disk,
            proof_file=entry.proof_file,
            anchor=entry.anchor,
            anchor_found=found,
            anchor_line=line,
            anchor_line_number=number,
            cross_file=entry.cross_file,
            cross_anchor=entry.cross_anchor,
            cross_anchor_found=cross_found or not bool(entry.cross_anchor),
            why=entry.why,
            production_declarations=declarations.get(entry.capability, 0),
        )
    return readings


def _identity(row: CensusRow) -> RowIdentity:
    """Name one census row for the roadmap face."""
    return RowIdentity(
        task_id=row.task_id,
        provider=row.provider,
        upstream_model=row.upstream_model,
        needs=tuple(sorted(row.needs)),
    )


def greedy_cover(rows: list[CensusRow], readings: dict[str, EntryReading]) -> Roadmap:
    """Pick, step by step, the capability that unlocks the most remaining rows.

    A row is only unlocked when its entire canonical need set is covered, so the step gains are not
    additive and a frequency table cannot produce this order. The pool is every engine-work
    capability this run did *not* read as present: shipped work is not a roadmap step, and a drifted
    entry is excluded as well because its claim can no longer be checked -- those rows surface under
    the still-locked guard instead.
    """
    pending = [row for row in rows if not row.expressible_today]
    covered = frozenset(capability for capability, reading in readings.items() if reading.present)
    non_engine = frozenset(
        capability for capability, reading in readings.items() if not reading.engine_work
    )
    pool = sorted(
        capability
        for capability, reading in readings.items()
        if not reading.present and reading.engine_work and reading.status != "drifted"
    )
    shipped = [row for row in pending if row.needs and row.needs <= covered]
    empty = [row for row in pending if not row.needs]
    reached = {row.task_id for row in shipped} | {row.task_id for row in empty}
    remaining = [row for row in pending if row.task_id not in reached]
    built = set(covered)
    cumulative = len(shipped)
    steps: list[BuildStep] = []
    while remaining and pool:
        best_capability: str | None = None
        best_rows: list[CensusRow] = []
        for capability in pool:
            trial = frozenset(built | {capability})
            hit = [row for row in remaining if row.needs <= trial]
            if len(hit) > len(best_rows):
                best_capability, best_rows = capability, hit
        if best_capability is None or not best_rows:
            break
        built.add(best_capability)
        pool.remove(best_capability)
        unlocked = {row.task_id for row in best_rows}
        remaining = [row for row in remaining if row.task_id not in unlocked]
        cumulative += len(best_rows)
        steps.append(
            BuildStep(
                capability=best_capability,
                rows_newly_unlocked=len(best_rows),
                cumulative_rows_unlocked=cumulative,
                newly_unlocked_rows=tuple(_identity(row) for row in best_rows),
            )
        )
    blocking = sorted(
        {
            capability
            for row in remaining
            for capability in row.needs
            if capability in non_engine or readings[capability].status == "drifted"
        }
    )
    return Roadmap(
        rows_pending=len(pending),
        rows_expressible_today=len(rows) - len(pending),
        shipped_cover=tuple(_identity(row) for row in shipped),
        build_order=tuple(steps),
        rows_unlocked_by_greedy_prefix=cumulative,
        rows_still_locked_after_engine_pool=tuple(_identity(row) for row in remaining),
        blocking_non_engine=tuple(blocking),
        rows_outside_engine=tuple(_identity(row) for row in pending if row.needs & non_engine),
        rows_with_no_engine_work_possible=sum(
            1 for row in pending if row.needs and row.needs <= non_engine
        ),
        rows_with_empty_need_set=tuple(_identity(row) for row in empty),
    )


def build_report(
    *,
    round_id: str,
    rows: list[CensusRow],
    readings: dict[str, EntryReading],
    declarations: dict[str, int],
    declarations_by_file: dict[str, dict[str, int]],
    modules_scanned: int,
    census_files: tuple[str, ...],
) -> Report:
    """Assemble this run's faces from the rows and the readings, nothing else."""
    pending = [row for row in rows if not row.expressible_today]
    mentions: Counter[str] = Counter(capability for row in pending for capability in row.needs)
    solo: Counter[str] = Counter(
        next(iter(row.needs))
        for row in pending
        if len(row.needs) == 1 and readings[next(iter(row.needs))].engine_work
    )
    raw_labels = {label for row in rows for label in row.raw_labels}
    return Report(
        round_id=round_id,
        census_files=census_files,
        row_count=len(rows),
        unique_task_ids=len({row.task_id for row in rows}),
        raw_label_count=len(raw_labels),
        raw_mention_count=sum(len(row.raw_labels) for row in rows),
        labels_supported=len(LABEL_TO_CAPABILITY),
        canonical_count=len(readings),
        mentions=dict(mentions),
        rows_needing_only_this=dict(solo),
        readings=readings,
        declarations=declarations,
        declarations_by_file=declarations_by_file,
        modules_scanned=modules_scanned,
        roadmap=greedy_cover(rows, readings),
    )


def run(
    *,
    round_id: str = DEFAULT_ROUND_ID,
    root: Path | None = None,
    census_dir: Path | None = None,
    entries: tuple[CapabilityEntry, ...] | None = None,
) -> Report:
    """Load the census, anchor the registry, scan declarations and cover the sets.

    Args:
        round_id: The round label recorded in the JSON instead of any timestamp.
        root: Repository root whose provider tree and proof files are read.
        census_dir: Directory holding the ``census-*.json`` files; defaults inside ``root``.
        entries: Registry to verify; defaults to :data:`REGISTRY`, which tests replace wholesale.
    """
    base = root if root is not None else _REPO_ROOT
    census = census_dir if census_dir is not None else base / CENSUS_DIR_REL
    scope = REGISTRY if entries is None else entries
    _check_registry(scope)
    rows = load_rows(census)
    declarations, by_file, modules = count_declarations(scope, base)
    readings = verify_registry(scope, base, declarations)
    files = tuple(path.name for path in census_row_files(census))
    return build_report(
        round_id=round_id,
        rows=rows,
        readings=readings,
        declarations=declarations,
        declarations_by_file=by_file,
        modules_scanned=modules,
        census_files=files,
    )


def serialize(report: Report) -> str:
    """Serialize the roadmap deterministically: sorted keys, and no timestamp in any field."""
    return json.dumps(report.to_json(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


REGISTRY: Final[tuple[CapabilityEntry, ...]] = (
    CapabilityEntry(
        capability="decode.delimited",
        claim=True,
        engine_work=True,
        proof_file=DECODERS_REL,
        anchor="csv.reader",
        cross_file=SPEC_REL,
        cross_anchor="zip_csv",
        why="decode_body reads csv/tsv/zip_csv bytes into header-keyed rows with a declarable "
        "delimiter, member and encoding.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind"),
    ),
    CapabilityEntry(
        capability="decode.archive_member",
        claim=True,
        engine_work=True,
        proof_file=DECODERS_REL,
        anchor="zipfile.ZipFile",
        why="One named member is selected from the archive (exact, then basename, then glob) and "
        "its bytes handed to the delimited reader -- only for a zip_csv body; an archive of XML "
        "or XLSX has no decoder kind.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="zip_csv"),
    ),
    CapabilityEntry(
        capability="decode.xml",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="The decoder vocabulary is a closed Literal and DecoderSpec.__post_init__ rejects "
        "anything outside it, so no declaration can ask for an XML or RSS body.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="xml"),
    ),
    CapabilityEntry(
        capability="decode.tabular_file",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="No workbook kind exists in the decoder vocabulary and nothing reads a sheet region.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="xlsx"),
    ),
    CapabilityEntry(
        capability="decode.opaque_bytes",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="A body becomes a document or it becomes rows; no kind publishes the bytes "
        "themselves as the result, which is what a PDF download needs.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="bytes"),
    ),
    CapabilityEntry(
        capability="decode.html_table",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="No html kind exists, so an HTML table cannot be declared as a body shape.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="html_table"),
    ),
    CapabilityEntry(
        capability="decode.html_text",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="No html kind exists, and the engine never strips markup or rewrites a body as "
        "markdown.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="html_markdown"),
    ),
    CapabilityEntry(
        capability="decode.text_table",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="Only a delimited reader splits a body; a colon-delimited file or a regex-parsed line "
        "has no decoder kind.",
        probe=DeclarationProbe(call="ModelSpec", keyword="row_pattern"),
    ),
    CapabilityEntry(
        capability="decode.geojson",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="A feature collection publishes its attributes under geometry members; no kind walks "
        "them into rows.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="geojson"),
    ),
    CapabilityEntry(
        capability="shape.nested_data_message",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]',
        why="SDMX-JSON needs message shaping (dimension series, observation keys, sectioned "
        "structure); the engine reads a JSON document or a delimited table and nothing else.",
        probe=DeclarationProbe(call="DecoderSpec", keyword="kind", value_contains="sdmx"),
    ),
    CapabilityEntry(
        capability="path.dotted_pointer",
        claim=True,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor='pointer.split(".")',
        cross_file=SPEC_REL,
        cross_anchor='rows_pointer: str = ""',
        why="resolve_rows walks dotted parts through dicts and digit list indices, and an absent "
        "path is *_SHAPE_INVALID rather than an empty answer.",
        probe=DeclarationProbe(call="ModelSpec", keyword="rows_pointer", non_empty=True),
    ),
    CapabilityEntry(
        capability="path.object_keys",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="if not isinstance(current, list):",
        why="A document that is not a JSON array is refused, so records keyed by object member or "
        "flattened out of a graph cannot be located.",
        probe=DeclarationProbe(call="ModelSpec", keyword="rows_from_keys"),
    ),
    CapabilityEntry(
        capability="path.explode",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="def resolve_rows",
        why="One pointer returns one record list; nothing re-reads the document per record, "
        "expands a nested array into rows, or pivots a series.",
        probe=DeclarationProbe(call="ModelSpec", keyword="per_record_path"),
    ),
    CapabilityEntry(
        capability="path.assemble_single_row",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="def normalize_record",
        why="normalize_record builds exactly one row out of exactly one record, so a model whose "
        "single row is assembled from many values has nowhere to say so.",
        probe=DeclarationProbe(call="ModelSpec", keyword="row_assembly"),
    ),
    CapabilityEntry(
        capability="shape.columnar",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="if not isinstance(current, list):",
        why="Parallel arrays under named keys are a dict, and resolve_rows only accepts a list.",
        probe=DeclarationProbe(call="ModelSpec", keyword="field_header"),
    ),
    CapabilityEntry(
        capability="request.body",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="get_shared_http_client",
        why="Names the engine's only send seam: _http_get_json calls the governed client's GET. "
        "The GET-only call is the proof that no request body can be declared.",
        probe=DeclarationProbe(call="ModelSpec", keyword="body"),
    ),
    CapabilityEntry(
        capability="request.body_pagination",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="params[paging.offset_key] = str(collected)",
        why="An offset is written into the query keys and never into a body, so body-driven offset "
        "paging is not expressible.",
        probe=DeclarationProbe(call="ModelSpec", keyword="body_pagination"),
    ),
    CapabilityEntry(
        capability="request.graphql",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="get_shared_http_client",
        why="The same GET-only send seam is the proof there is no query-template request shape.",
        probe=DeclarationProbe(call="ModelSpec", keyword="graphql_query"),
    ),
    CapabilityEntry(
        capability="request.serialised_param",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor='",".join(str(item) for item in value)',
        why="_encode_value renders a list parameter by comma-joining it; nothing serialises a JSON "
        "object or a key/date mapping into one query value.",
        probe=DeclarationProbe(call="ModelSpec", keyword="param_encoder"),
    ),
    CapabilityEntry(
        capability="flow.fan_out",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="url = f\"{spec.base_url.rstrip('/')}{render_path(spec, query)}\"",
        why="fetch_pages builds one URL per declaration and pages it; there is no per-symbol or "
        "per-item request set to enumerate and concatenate.",
        probe=DeclarationProbe(call="ModelSpec", keyword="fan_out"),
    ),
    CapabilityEntry(
        capability="flow.join",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="url = f\"{spec.base_url.rstrip('/')}{render_path(spec, query)}\"",
        why="The same single-URL loop is the proof no second response is fetched and joined onto "
        "the first model's rows.",
        probe=DeclarationProbe(call="ModelSpec", keyword="join_on"),
    ),
    CapabilityEntry(
        capability="flow.preflight_lookup",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="url = f\"{spec.base_url.rstrip('/')}{render_path(spec, query)}\"",
        why="The URL is known from the declaration before any request, so a directory lookup that "
        "decides the URL afterwards cannot be declared.",
        probe=DeclarationProbe(call="ModelSpec", keyword="preflight"),
    ),
    CapabilityEntry(
        capability="flow.url_template",
        claim=True,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor='path.replace(f"{{{name}}}", text)',
        cross_file=SPEC_REL,
        cross_anchor="path_placeholders",
        why="render_path substitutes each declared {placeholder} from the validated query, after "
        "_PATH_SEGMENT proves the value is exactly one URL segment.",
        probe=DeclarationProbe(call="ModelSpec", keyword="path", value_contains="{"),
    ),
    CapabilityEntry(
        capability="flow.branch",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor="base_url: str",
        why="One base_url and one path string per declaration: an interval/metric/region branch "
        "that addresses different endpoints has no declaration surface.",
        probe=DeclarationProbe(call="ModelSpec", keyword="path_branches"),
    ),
    CapabilityEntry(
        capability="rows.filter",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor="row_filters",
        why="Presence is read off the disk rather than asserted: row_filters is the field only a "
        "finished client-side row filter can add to ModelSpec, so this entry reports absent "
        "until it lands and present from the first run after it does.",
        read_from_disk=True,
        probe=DeclarationProbe(call="ModelSpec", keyword="row_filters"),
    ),
    CapabilityEntry(
        capability="rows.order_limit",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="def _page_request",
        why="_page_request adds paging keys and nothing else: rows stay in upstream order, with no "
        "sort and no top-n applied.",
        probe=DeclarationProbe(call="ModelSpec", keyword="order_by"),
    ),
    CapabilityEntry(
        capability="columns.derive",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="values[column.name] = value",
        why="A column's value is copied out of the record verbatim; no column is computed from "
        "other columns, concatenated, or parsed out of a symbol.",
        probe=DeclarationProbe(call="ModelSpec", keyword="derived_columns"),
    ),
    CapabilityEntry(
        capability="columns.cast",
        claim=False,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="def _column_type",
        why="_column_type maps a declared kind onto one pydantic domain, so the engine enforces a "
        "value's type but never converts epoch milliseconds or reformats a published date.",
        probe=DeclarationProbe(call="ColumnSpec", keyword="format"),
    ),
    CapabilityEntry(
        capability="columns.rescale",
        claim=False,
        engine_work=True,
        proof_file=SPEC_REL,
        anchor='units: str = ""',
        why="ColumnSpec keeps units verbatim for the review record and applies nothing, so a "
        "published percent or a 1/1000 strike scale cannot be declared.",
        probe=DeclarationProbe(call="ColumnSpec", keyword="scale"),
    ),
    CapabilityEntry(
        capability="columns.select",
        claim=True,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="for column in spec.columns:",
        cross_file=HTTP_JSON_REL,
        cross_anchor='extra="forbid"',
        why="normalize_record walks only the declared columns and the generated row model forbids "
        "extras, so an undeclared raw key cannot reach a row.",
        probe=DeclarationProbe(call="ModelSpec", keyword="columns", non_empty=True),
    ),
    CapabilityEntry(
        capability="columns.rename",
        claim=True,
        engine_work=True,
        proof_file=HTTP_JSON_REL,
        anchor="key = column.source_key or column.name",
        why="Every column declares the raw key it reads and publishes it under its own name.",
        probe=DeclarationProbe(call="ColumnSpec", keyword="source_key", non_empty=True),
    ),
    CapabilityEntry(
        capability="outside.sdk",
        claim=False,
        engine_work=False,
        proof_file=SPEC_REL,
        anchor="Nothing in this package performs I/O or imports an upstream SDK.",
        why="A model whose answer is an SDK call cannot be expressed as a declaration at all; the "
        "engine forbids SDK imports by design, so this is delegation rather than engine work.",
    ),
    CapabilityEntry(
        capability="outside.bespoke",
        claim=False,
        engine_work=False,
        proof_file=SEC_SPECS_REL,
        anchor="NOT_DECLARABLE: dict[str, str] = {",
        why="Delegation to or composition of a hand-written fetcher is recorded in each provider's "
        "NOT_DECLARABLE ledger instead of being built into the engine.",
    ),
    CapabilityEntry(
        capability="outside.no_request",
        claim=False,
        engine_work=False,
        proof_file=SPEC_REL,
        anchor='if not self.base_url.startswith("https://"):',
        why="A declaration must name an https origin, so a model that only transposes a bundled "
        "constant has no static-source field to use.",
    ),
    CapabilityEntry(
        capability="outside.source_absent",
        claim=False,
        engine_work=False,
        proof_file="",
        anchor="",
        why="A vendored upstream tree that is not in the repo is a filesystem fact this instrument "
        "does not read; recorded unanchored rather than anchored to a token whose absence would be "
        "indistinguishable from a typo.",
    ),
    CapabilityEntry(
        capability="outside.stateful",
        claim=False,
        engine_work=False,
        proof_file=SPEC_REL,
        anchor="static_headers: tuple[tuple[str, str], ...] = ()",
        why="Headers a declaration sends are static pairs replayed on every page, so a cookie "
        "warmed by a prior response or a local cache store has nowhere to live.",
    ),
    CapabilityEntry(
        capability="outside.anti_bot",
        claim=False,
        engine_work=False,
        proof_file=HTTP_JSON_REL,
        anchor='429: "RATE_LIMITED"',
        why="_classify_status maps a throttled status onto a stable failure code and raises it; a "
        "retry-and-warm-up loop is not the engine's semantics.",
    ),
)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; ``--check`` is the only mode that exits non-zero on drift."""
    parser = argparse.ArgumentParser(description="Model-capability census roadmap instrument.")
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help=f"write the roadmap JSON to this path (suggested: {ROADMAP_REL})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when a registry anchor drifted or the census is not "
        f"{EXPECTED_CENSUS_ROWS} rows (an unmapped label already aborts every mode)",
    )
    parser.add_argument(
        "--round-id", default=DEFAULT_ROUND_ID, help="round label recorded in the JSON"
    )
    args = parser.parse_args(argv)
    try:
        report = run(round_id=str(args.round_id))
    except (RoadmapError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(report.render_face(bool(args.check)))
    output: Path | None = args.json
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialize(report), encoding="utf-8")
        except OSError as error:
            print(f"FAIL: {output} could not be written: {error}", file=sys.stderr)
            return 1
        print(f"OK: wrote {_display(output)}")
    if args.check and report.failures:
        for failure in report.failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
