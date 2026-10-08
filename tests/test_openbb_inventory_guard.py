"""The provider inventory has to be able to fail (C33, AC-10).

``docs/proposals/openbb-migration/README.md`` §5 makes
``provider-inventory.yaml`` the baseline for batch progress, and until this round
nothing in the tree ever read the file back — so it had become a write-only record:
unparseable, every row still ``待实现`` while 7 sources serve 33 capabilities, and
silent about the two largest sources in the system. Those are the shapes a
"we'll keep it updated" document decays into when no judge reads it (the same
family as C23's authority table and C31's type check).

The rules live in ``scripts/quality/openbb_inventory_plane.py`` (one judgment
plane, C27) and are asserted here on synthetic records, in both directions:

  * a record that claims more than the tree — ``已对照转正`` with nothing registered,
    or with a leg still unverified — must redden;
  * a record that claims less than the tree — a source with legs still called
    ``待实现``, or missing a row entirely — must redden;
  * and a record that says nothing false but declares the wrong counts, or names a
    rights row that the registration table does not contain, must redden too;
  * and a field whose name promises a list but deserializes as one string (or as
    ``null``) must redden — ``PARSE`` cannot see that class, because those lines are
    valid YAML, and the pre-fix record wrote 30 of its 64 dependency fields that way.

Both real documents are checked as shipped inputs, so this file also fails if the
plan's baseline and the rights registry drift apart again.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path

from scripts.quality import openbb_inventory_plane as guard
from scripts.quality.provider_model_inventory import (
    DEFAULT_UPSTREAM_PATH,
    LEDGER_RELATIVE_PATH,
    load_pinned_provider_metadata,
    load_task_ledger,
    validate_credentials,
    validate_inventory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = REPO_ROOT / "docs/proposals/openbb-migration/provider-inventory.yaml"
RIGHTS = REPO_ROOT / "docs/data-rights-registry.md"
BEFORE = REPO_ROOT / "docs/evidence/C33/provider-inventory.before.yaml"

#: A copy of the pre-fix record, judged against today's tree. Used to prove the plane
#: is not green-by-construction: the same rules that pass on the shipped file produce
#: fourteen findings on the file this round replaced.
BEFORE_TEXT = BEFORE.read_text(encoding="utf-8")
RIGHTS_TEXT = RIGHTS.read_text(encoding="utf-8")


def fixed_authority() -> tuple[dict[str, list[str]], dict[str, list[str]], list[dict[str, str]]]:
    """Load and validate the fixed AST/CSV model and credential authority."""
    source_files, records, credentials, source_issues = load_pinned_provider_metadata(
        DEFAULT_UPSTREAM_PATH
    )
    ledger = load_task_ledger(REPO_ROOT / LEDGER_RELATIVE_PATH)
    model_issues, _ = validate_inventory(records, ledger, source_files)
    credential_issues = validate_credentials(credentials, ledger)
    assert source_issues == []
    assert model_issues == []
    assert credential_issues == []
    models: dict[str, list[str]] = {}
    for record in records:
        models.setdefault(record.provider, []).append(record.model)
    return models, credentials, ledger


MINI_RIGHTS = """# 数据源权利登记表

## 1. 登记表

| # | 数据源 | 接入方式 | 状态 |
|---|--------|---------|------|
| 1 | 同花顺扶摇 API | 直连 | 待复核 |
| 2 | 东方财富（网页/接口） | 搬运 | 待复核 |

## 2. 待办与责任

| # | 事项 | 责任 | 状态 |
|---|------|------|------|
| 1 | 向同花顺索取书面条款副本，明确四项边界 | 产品 | 待办 |
"""


def row(
    provider: str,
    status: str,
    *,
    rights: list[str] | None = None,
    fetchers: int = 1,
    models: str = "[SomethingElse]",
    credentials: str = "[]",
    sdk_dependencies: str | None = None,
) -> str:
    """Render one synthetic inventory row.

    Args:
        provider: The source label the row describes.
        status: The value written into ``status``.
        rights: Names to write into ``rights_rows``, or omit the field when ``None``.
        fetchers: The upstream fetcher count the row states.
        models: The literal text of the ``models:`` field.
        credentials: The literal text after ``credentials:`` — a test that wants a
            string-shaped or valueless list field writes it here.
        sdk_dependencies: The literal text after ``sdk_dependencies:``, or omit the
            field when ``None``.

    Returns:
        The row's YAML lines, indented as the real record indents them.
    """
    lines = [
        f"  - provider: {provider}",
        "    batch: P0",
        f"    fetchers: {fetchers}",
        f"    models: {models}",
        f"    credentials: {credentials}".rstrip(),
    ]
    if sdk_dependencies is not None:
        lines.append(f"    sdk_dependencies: {sdk_dependencies}")
    lines += [
        "    scenario: 守卫用例的合成场景",
        "    estimate_person_days: 1.0",
        f"    status: {status}",
    ]
    if rights is not None:
        lines.append(f"    rights_rows: [{', '.join(rights)}]")
    return "\n".join(lines) + "\n"


def record(
    providers: str = "",
    *,
    local: str = "",
    prose: tuple[int, int] | None = None,
    provider_count: int | None = None,
    local_provider_count: int | None = None,
) -> str:
    """Wrap synthetic rows in the header the real record carries.

    Args:
        providers: Pre-rendered rows for the ``providers:`` section.
        local: Pre-rendered rows for the ``local_providers:`` section.
        prose: The two numbers of the ``规模：…`` header comment, or omit the line.
        provider_count: Value of ``provider_count:``, or omit the field.
        local_provider_count: Value of ``local_provider_count:``, or omit the field.

    Returns:
        A whole document, valid YAML unless a test says otherwise.
    """
    head = "# OpenBB provider inventory (synthetic)\n"
    if prose:
        head += f"# 规模：{prose[0]} 个 provider 目录 / {prose[1]} 个 fetcher 模型\n"
    head += "version: 1\n"
    if provider_count is not None:
        head += f"provider_count: {provider_count}\n"
    if local_provider_count is not None:
        head += f"local_provider_count: {local_provider_count}\n"
    body = f"providers:\n{providers}"
    if local or local_provider_count is not None:
        body += f"local_providers:\n{local}"
    return head + body


def kinds(
    text: str,
    legs: dict[str, list[tuple[str, bool]]],
    rights: str = MINI_RIGHTS,
) -> list[str]:
    """Run the plane on a synthetic record and return the violated rule names.

    Args:
        text: The record under test.
        legs: The stand-in registry side.
        rights: The rights document side.

    Returns:
        One entry per finding, in report order.
    """
    return [finding.kind for finding in guard.findings(text, rights, legs)]


# --- the shipped documents ------------------------------------------------------


def test_shipped_record_deserializes() -> None:
    assert guard.parse_error(INVENTORY.read_text(encoding="utf-8")) is None


def test_shipped_record_agrees_with_the_tree() -> None:
    """The gate judgment: the baseline and the live registry say the same thing."""
    inventory_text = INVENTORY.read_text(encoding="utf-8")
    rows_by_provider = {row.provider: row.fields for row in guard.parse_rows(inventory_text)}
    assert guard.declared_rights(rows_by_provider["bls"]["rights_rows"]) == [
        "BLS（美国劳工统计局）"
    ]
    assert guard.declared_rights(rows_by_provider["fmp"]["rights_rows"]) == [
        "FMP（EquityHistorical / EquityQuote）"
    ]
    models, credentials, ledger = fixed_authority()
    found = guard.findings(
        inventory_text,
        RIGHTS_TEXT,
        guard.live_legs(),
        models,
        credentials,
        ledger,
    )
    assert [str(finding) for finding in found] == []


def test_pre_fix_record_reddens_the_same_rules() -> None:
    """The rules that pass on the shipped file must fail on the file it replaced.

    Without this, "0 findings" says nothing: a plane that never reports would satisfy
    the test above just as well.
    """
    legs = guard.live_legs()
    models, credentials, ledger = fixed_authority()
    found = guard.findings(BEFORE_TEXT, RIGHTS_TEXT, legs, models, credentials, ledger)
    assert len(found) >= 14, [str(finding) for finding in found]
    assert {finding.kind for finding in found} == {
        "PARSE",
        "MISSING ROW",
        "STALE STATUS",
        "RIGHTS LINK",
        "COUNT CLAIM",
        "MODEL ELISION",
        "MODEL COUNT",
        "MODEL SET",
        "CREDENTIAL MISMATCH",
        "CAPABILITY COUNT",
        "MODEL TASK STATUS",
        "REQUESTER STATUS",
    }
    text = "\n".join(str(finding) for finding in found)
    assert "`akshare` registers 10 capabilities" in text
    assert "`ths` registers 11 capabilities" in text
    assert "does not deserialize" in text


def test_report_exit_code_differs_between_the_two_records() -> None:
    """The archive's exit code is derived from the findings, not hard-coded green."""
    legs = guard.live_legs()
    with redirect_stdout(io.StringIO()) as before_buf:
        before_exit = guard.report(BEFORE_TEXT, RIGHTS_TEXT, legs)
    with redirect_stdout(io.StringIO()) as after_buf:
        after_exit = guard.report(INVENTORY.read_text(encoding="utf-8"), RIGHTS_TEXT, legs)
    assert (before_exit, after_exit) == (1, 0)
    assert "MODEL ELISION" in before_buf.getvalue()
    assert "== findings (judged) ==\n  (none)" in after_buf.getvalue()


# --- STALE STATUS, both directions ---------------------------------------------

VERIFIED_LEGS = {"demo": [("stock_daily", True)]}
MIXED_LEGS = {"demo": [("stock_daily", True), ("index_daily", False)]}
UNVERIFIED_LEGS = {"demo": [("stock_daily", False)]}
NO_LEGS: dict[str, list[tuple[str, bool]]] = {}


def test_record_claiming_less_than_the_tree_reddens() -> None:
    """A live source still called 待实现 is the drift this round was opened for."""
    text = record(row("demo", guard.STATUS_TODO, rights=["同花顺扶摇 API"]))
    assert kinds(text, VERIFIED_LEGS) == ["STALE STATUS"]


def test_record_claiming_more_than_the_tree_reddens() -> None:
    """转正 with nothing registered is the mirror image, and equally red."""
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]))
    assert kinds(text, NO_LEGS) == ["STALE STATUS"]


def test_verified_status_with_an_unverified_leg_reddens() -> None:
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]))
    assert kinds(text, MIXED_LEGS) == ["STALE STATUS"]


def test_unverified_status_while_every_leg_is_routable_reddens() -> None:
    text = record(row("demo", guard.STATUS_UNVERIFIED, rights=["同花顺扶摇 API"]))
    assert kinds(text, VERIFIED_LEGS) == ["STALE STATUS"]


def test_the_middle_state_is_nameable_and_exact() -> None:
    """已实现未对照 remains the exact state for a registered unverified leg."""
    for status in (guard.STATUS_TODO, guard.STATUS_VERIFIED):
        text = record(row("demo", status, rights=["同花顺扶摇 API"]))
        assert kinds(text, UNVERIFIED_LEGS) == ["STALE STATUS"]
    middle = record(row("demo", guard.STATUS_UNVERIFIED, rights=["同花顺扶摇 API"]))
    assert kinds(middle, UNVERIFIED_LEGS) == []


def test_unimplemented_rows_are_not_noise() -> None:
    """27 of the shipped rows have no leg; they must pass, not be reported as gaps."""
    text = record(row("demo", guard.STATUS_TODO))
    assert kinds(text, NO_LEGS) == []


def test_unknown_status_word_reddens() -> None:
    text = record(row("demo", "已完成", rights=["同花顺扶摇 API"]))
    assert kinds(text, VERIFIED_LEGS) == ["STALE STATUS"]


# --- MISSING ROW / DUPLICATE ROW ------------------------------------------------


def test_source_without_a_row_reddens_and_deleting_a_row_reddens() -> None:
    """Same rule, two ways to reach it: never written, or written and then removed."""
    legs = {"demo": [("stock_daily", True)], "other": [("index_daily", True)]}
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]))
    assert kinds(text, legs) == ["MISSING ROW"]


def test_row_in_both_sections_reddens() -> None:
    text = record(
        row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]),
        local=row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]),
    )
    assert kinds(text, VERIFIED_LEGS) == ["DUPLICATE ROW"]


# --- COUNT CLAIM ----------------------------------------------------------------


def test_provider_count_must_equal_the_body() -> None:
    providers = row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"])
    assert kinds(record(providers, provider_count=1), VERIFIED_LEGS) == []
    assert kinds(record(providers, provider_count=2), VERIFIED_LEGS) == ["COUNT CLAIM"]


def test_local_provider_count_must_equal_the_body() -> None:
    local = row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"])
    upstream = row("not_yet", guard.STATUS_TODO)
    text = record(upstream, local=local, local_provider_count=1)
    assert kinds(text, VERIFIED_LEGS) == []
    wrong = record(upstream, local=local, local_provider_count=7)
    assert kinds(wrong, VERIFIED_LEGS) == ["COUNT CLAIM"]


def test_header_prose_numbers_are_claims_and_are_checked() -> None:
    """Both figures of the 规模 line: directories and fetcher models."""
    providers = row(
        "demo",
        guard.STATUS_VERIFIED,
        rights=["同花顺扶摇 API"],
        fetchers=3,
        models="[A, B, C]",
    )
    assert kinds(record(providers, prose=(1, 3)), VERIFIED_LEGS) == []
    assert kinds(record(providers, prose=(33, 3)), VERIFIED_LEGS) == ["COUNT CLAIM"]
    assert kinds(record(providers, prose=(1, 348)), VERIFIED_LEGS) == ["COUNT CLAIM"]


def test_section_tagging_survives_a_second_section() -> None:
    """The last row of ``providers:`` belongs to ``providers:``, not to the next section.

    This is a regression guard for the plane's own bug: the counters above first read
    the fixed record as 31 providers + 3 locals and 345 fetchers, because a row was
    closed with the section that was current when the *next* row opened.
    """
    text = record(
        row("first", guard.STATUS_TODO) + row("last", guard.STATUS_TODO),
        local=row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]),
    )
    rows = guard.parse_rows(text)
    assert [(r.provider, r.section) for r in rows] == [
        ("first", "providers"),
        ("last", "providers"),
        ("demo", "local_providers"),
    ]


# --- RIGHTS LINK ----------------------------------------------------------------


def test_rights_table_scoping_ignores_the_todo_table() -> None:
    """A 待办 sentence is not a data source; matching one would unfalsify the rule."""
    assert guard.rights_rows(MINI_RIGHTS) == ["同花顺扶摇 API", "东方财富（网页/接口）"]
    todo_sentence = ["向同花顺索取书面条款副本，明确四项边界"]
    text = record(row("demo", guard.STATUS_VERIFIED, rights=todo_sentence))
    assert kinds(text, VERIFIED_LEGS) == ["RIGHTS LINK"]


def test_serving_source_without_a_rights_link_reddens() -> None:
    text = record(row("demo", guard.STATUS_VERIFIED))
    assert kinds(text, VERIFIED_LEGS) == ["RIGHTS LINK"]


def test_every_named_rights_row_must_exist() -> None:
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API", "不存在的源"]))
    assert kinds(text, VERIFIED_LEGS) == ["RIGHTS LINK"]


def test_new_provider_specific_rights_rows_must_exist_in_registration_table() -> None:
    """The BLS/FMP links stay red if their source-specific registry rows disappear."""
    rights_without_new_sources = "\n".join(
        line
        for line in RIGHTS_TEXT.splitlines()
        if "| BLS（美国劳工统计局） |" not in line
        and "| FMP（EquityHistorical / EquityQuote） |" not in line
    )
    models, credentials, ledger = fixed_authority()
    found = guard.findings(
        INVENTORY.read_text(encoding="utf-8"),
        rights_without_new_sources,
        guard.live_legs(),
        models,
        credentials,
        ledger,
    )
    assert [(finding.kind, finding.text.split("`")[1]) for finding in found] == [
        ("RIGHTS LINK", "bls"),
        ("RIGHTS LINK", "fmp"),
    ]
    assert all("not a 数据源" in finding.text for finding in found)


def registration_rows(text: str) -> list[tuple[int, str]]:
    """Index the §1 registration table's rows the way the plane's own reader does.

    Args:
        text: Whole ``docs/data-rights-registry.md`` contents.

    Returns:
        ``(line index, 数据源 cell)`` for every §1 table row, in file order.
    """
    rows: list[tuple[int, str]] = []
    in_section = False
    for index, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            in_section = line.startswith("## 1")
            continue
        if not in_section:
            continue
        match = guard.RIGHTS_TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if cells:
            rows.append((index, cells[0]))
    return rows


def test_every_declared_rights_row_is_load_bearing() -> None:
    """Delete a registration row and the sources that name it are the only ones reddened.

    The shipped file is green, so a ``rights_rows:`` name nothing really reads would stay green
    with its §1 line deleted. This asks the runtime which sources serve, takes every row name each
    one declares, and requires each deletion to produce exactly one RIGHTS LINK per declaring
    source — no more (the rest of the plane stays green) and no fewer (a name matching no row is
    reported as an instrument bug rather than passed over).
    """
    inventory_text = INVENTORY.read_text(encoding="utf-8")
    models, credentials, ledger = fixed_authority()
    rows_by_provider = {row.provider: row.fields for row in guard.parse_rows(inventory_text)}
    served = {source: legs for source, legs in guard.live_legs().items() if legs}

    declared: dict[str, list[str]] = {}
    for source in served:
        for link in guard.declared_rights(rows_by_provider[source]["rights_rows"]):
            declared.setdefault(link, []).append(source)
    assert {source for sources in declared.values() for source in sources} == set(served)

    lines = RIGHTS_TEXT.splitlines()
    with_nothing_removed = guard.findings(
        inventory_text, RIGHTS_TEXT, served, models, credentials, ledger
    )
    assert [str(finding) for finding in with_nothing_removed] == []

    for link, sources in sorted(declared.items()):
        targets = [index for index, name in registration_rows(RIGHTS_TEXT) if link in name]
        assert targets, f"`{link}` names no §1 row, so this face measured nothing"
        stripped = "\n".join(line for index, line in enumerate(lines) if index not in targets)
        found = guard.findings(inventory_text, stripped, served, models, credentials, ledger)
        assert [(finding.kind, finding.text.split("`")[1]) for finding in found] == [
            ("RIGHTS LINK", source) for source in sorted(sources)
        ], link
        assert all("not a 数据源" in finding.text for finding in found)


def test_local_rows_may_name_several_rights_rows() -> None:
    text = record(
        row("not_yet", guard.STATUS_TODO),
        local=row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API", "东方财富"]),
    )
    assert kinds(text, VERIFIED_LEGS) == []


# --- FIELD SHAPE -----------------------------------------------------------------


def test_a_list_field_written_as_a_bare_string_reddens() -> None:
    """``credentials: none`` is valid YAML, and parses as the word — not as a list."""
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"], credentials="none"))
    found = guard.findings(text, MINI_RIGHTS, VERIFIED_LEGS)
    assert [f.kind for f in found] == ["FIELD SHAPE"]
    assert "str 而不是 list" in str(found[0])


def test_a_comma_joined_string_field_reddens_as_one_dependency() -> None:
    """The shape the pre-fix record used 9 times: four names, one string, ``len()`` == 1."""
    text = record(
        row(
            "demo",
            guard.STATUS_VERIFIED,
            rights=["同花顺扶摇 API"],
            sdk_dependencies="openbb-platform-api, openbb-economy, openbb-charting, async-lru",
        )
    )
    found = guard.findings(text, MINI_RIGHTS, VERIFIED_LEGS)
    assert [f.kind for f in found] == ["FIELD SHAPE"]
    assert "`sdk_dependencies`" in str(found[0])


def test_a_list_field_written_with_no_value_reddens_as_null() -> None:
    """A key with nothing after it is ``None``, which is not an empty list either."""
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"], credentials=""))
    found = guard.findings(text, MINI_RIGHTS, VERIFIED_LEGS)
    assert [f.kind for f in found] == ["FIELD SHAPE"]
    assert "null" in str(found[0])


def test_block_style_lists_pass_the_shape_rule() -> None:
    """Flow style is not the only legal way to write a list; the rule must not demand it."""
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"], credentials=""))
    block = text.replace("    credentials:\n", "    credentials:\n      - fred_api_key\n")
    assert block != text
    assert kinds(block, VERIFIED_LEGS) == []


def test_the_shape_rule_stays_quiet_while_parse_reddens() -> None:
    """A record that does not deserialize has no field types to judge.

    The pre-fix file held 34 dashes *and* 30 string-shaped fields; reporting both would
    have counted one broken line twice. ``PARSE`` owns the line until the file parses.
    """
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"], credentials="none"))
    broken = text.replace("    credentials: none", "    credentials: -")
    assert kinds(broken, VERIFIED_LEGS) == ["PARSE"]


# --- PARSE ----------------------------------------------------------------------


def test_bare_dash_scalars_redden_parse_and_still_describe_the_body() -> None:
    """The bug that hid everything else: ``credentials: -`` is a sequence marker.

    34 such lines made the record unparseable, and a record no parser can read cannot
    be checked — which is why the rows are read structurally and the parse failure is
    its own counter rather than a crash.
    """
    text = record(row("demo", guard.STATUS_VERIFIED, rights=["同花顺扶摇 API"]))
    broken = text.replace("    credentials: []", "    credentials: -")
    assert kinds(broken, VERIFIED_LEGS) == ["PARSE"]
    assert len(guard.parse_rows(broken)) == 1


# --- MODEL completeness ---------------------------------------------------------


def test_truncated_model_lists_are_judged() -> None:
    """The fixed AST, not a hand-written ellipsis, owns the complete model list."""
    elided = record(
        row(
            "demo",
            guard.STATUS_VERIFIED,
            rights=["同花顺扶摇 API"],
            fetchers=3,
            models="[A, B, ...]",
        )
    )
    full_models = "[A, B, C]"
    full = record(
        row(
            "demo",
            guard.STATUS_VERIFIED,
            rights=["同花顺扶摇 API"],
            fetchers=3,
            models=full_models,
        )
    )
    assert "MODEL ELISION" in kinds(elided, VERIFIED_LEGS)
    assert kinds(full, VERIFIED_LEGS) == []
    assert guard.model_elisions(guard.parse_rows(elided)) == ["demo: 2 listed of 3 stated"]


def test_missing_eia_model_fails_against_fixed_model_authority() -> None:
    eia_models = ["PetroleumStatusReport", "ShortTermEnergyOutlook"]
    text = record(row("eia", guard.STATUS_TODO, fetchers=1, models="[PetroleumStatusReport]"))
    found = guard.findings(
        text,
        MINI_RIGHTS,
        NO_LEGS,
        {"eia": eia_models},
    )

    assert any(f.kind == "MODEL SET" and "ShortTermEnergyOutlook" in f.text for f in found)


def test_duplicate_model_identity_fails() -> None:
    text = record(row("demo", guard.STATUS_TODO, fetchers=2, models="[SameModel, SameModel]"))

    found = guard.findings(text, MINI_RIGHTS, NO_LEGS, {"demo": ["SameModel", "OtherModel"]})

    assert any(f.kind == "MODEL DUPLICATE" for f in found)
    assert any(f.kind == "MODEL SET" for f in found)


def test_declared_model_count_must_match_the_list_and_fixed_denominator() -> None:
    text = "upstream_model_count: 3\nunique_upstream_model_count: 3\n" + record(
        row("demo", guard.STATUS_TODO, fetchers=2, models="[ModelOne, ModelTwo]")
    )
    text = text.replace(
        "    models: [ModelOne, ModelTwo]\n",
        "    models: [ModelOne, ModelTwo]\n    model_count: 3\n",
    )

    found = guard.findings(
        text,
        MINI_RIGHTS,
        NO_LEGS,
        {"demo": ["ModelOne", "ModelTwo"]},
    )

    assert any(f.kind == "MODEL COUNT" for f in found)


def test_fixed_credentials_must_match_manifest() -> None:
    text = record(
        row("demo", guard.STATUS_TODO, credentials="[fred_api_key]"),
    )

    found = guard.findings(
        text,
        MINI_RIGHTS,
        NO_LEGS,
        {"demo": ["SomethingElse"]},
        {"demo": ["api_key"]},
    )

    assert any(f.kind == "CREDENTIAL MISMATCH" for f in found)
