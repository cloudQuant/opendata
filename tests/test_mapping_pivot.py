"""The wide→long melt declaration of the 口径映射表 (AC-9|01, C54).

``normalize_frame`` projects one source column onto one contract field,
so a wide financial page - one row per report period, one column per
科目 - has nothing to project ``item`` from. C54 moved that decision
(what counts as a科目, and which column carries the row's report period
and announce date) into the table as a ``pivot`` block, which the three
melters read instead of carrying their own lists.

Two faces are checked: the loader refuses a half-declared melt (what a
科目 is cannot be half a 口径), and the melters follow the table - a
declaration the code ignores would be decoration, and AC-9|01's coverage
half would be arithmetic rather than wiring.
"""

import ast
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from opendata.data.mapping import (
    load_mapping,
    mapping_as_json,
    mapping_sources,
    normalize_frame,
    require_adjust_basis,
    require_comparable_calibers,
    require_domain_mapping,
    require_pivot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
P0_MIGRATION = REPO_ROOT / "alembic_data/versions/20260923-0001_ods_dwd_p0.py"


def _p0_domains() -> tuple[str, ...]:
    """The P0 domains, from the same ``_DWD_TABLES`` the probe divides by.

    The judge's denominator is the migration's own list, so the guard has
    to read it from there rather than from a list of names typed here.
    """
    tree = ast.parse(P0_MIGRATION.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(getattr(target, "id", "") == "_DWD_TABLES" for target in node.targets)
        ):
            return tuple(
                ast.unparse(key).strip("'\"") for key in node.value.keys if key is not None
            )
    raise AssertionError(f"_DWD_TABLES is not a mapping in {P0_MIGRATION}")


def _write_source(tmp_path: Path, source: str, domains: dict[str, Any]) -> None:
    """Write one mapping file and make the loader see it.

    The loader memoizes per source, so a test that rewrites a table to show
    the melt following it has to drop that memo - otherwise the second
    declaration would be read from the first one's cached mapping.
    """
    from opendata.data import mapping as mapping_module

    payload = {"version": 1, "source": source, "domains": domains}
    (tmp_path / f"{source}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    mapping_module.load_mapping.cache_clear()


@pytest.fixture
def tmp_mappings(tmp_path, monkeypatch):
    """Point the loader at an empty directory and keep the real cache intact."""
    from opendata.data import mapping as mapping_module

    monkeypatch.setattr(mapping_module, "_MAPPINGS_DIR", tmp_path)
    mapping_module.load_mapping.cache_clear()
    yield tmp_path
    mapping_module.load_mapping.cache_clear()


#: A grouped melt that satisfies the loader; each case below breaks it in
#: exactly one place.
_GROUPED: dict[str, Any] = {
    "key": ["symbol", "statement_type", "report_period", "item"],
    "adjust": "not_applicable",
    "suspension": "unmeasured",
    "denominator": "key_union",
    "fields": {
        name: {"from": name}
        for name in (
            "symbol",
            "statement_type",
            "report_period",
            "announce_date",
            "item",
            "value",
        )
    },
    "pivot": {
        "mode": "groups",
        "item_field": "item",
        "value_field": "value",
        "group_field": "statement_type",
        "row_columns": {"announce_date": "report_date_ms"},
        "groups": {"income": ["operating_income", "net_profit"], "balance": ["assets_total"]},
    },
}

#: A passthrough melt in the same shape.
_PASSTHROUGH: dict[str, Any] = {
    **{k: v for k, v in _GROUPED.items() if k != "pivot"},
    "pivot": {
        "mode": "passthrough",
        "item_field": "item",
        "value_field": "value",
        "group_field": "statement_type",
        "row_columns": {"report_period": "报告日", "announce_date": "公告日期"},
        "excluded": ["数据源", "币种"],
    },
}


def _pivot(domain: dict[str, Any], **changes: object) -> dict[str, Any]:
    """One copy of ``domain`` with its pivot block changed in ``changes``."""
    return {**domain, "pivot": {**domain["pivot"], **changes}}


def _without_pivot_key(domain: dict[str, Any], key: str) -> dict[str, Any]:
    pivot = {name: value for name, value in domain["pivot"].items() if name != key}
    return {**domain, "pivot": pivot}


class TestShippedMeltDeclarations:
    """What the shipped table declares for the two financial P0 domains."""

    def test_the_financial_p0_domains_are_declared_as_wide(self):
        assert require_pivot("ths", "financial_statement").mode == "groups"
        assert require_pivot("akshare", "financial_statement").mode == "passthrough"
        assert require_pivot("akshare", "financial_indicator").mode == "passthrough"

    def test_every_p0_domain_of_the_a4_1_migration_is_mapped(self):
        """The coverage half of AC-9|01, read through the loader.

        The probe compares its denominator (the migration's ``_DWD_TABLES``)
        against a regex over the yaml text; this asserts the same arithmetic
        through ``load_mapping``, so a domain the probe counts but the loader
        cannot build is caught here instead of by the flip.
        """
        declared = {name for source in mapping_sources() for name in load_mapping(source).domains}

        assert set(_p0_domains()) <= declared
        assert {"financial_statement", "financial_indicator"} <= declared

    def test_the_grouped_item_vocabulary_is_pinned(self):
        """The three statements' 科目 sets are measured, not derived from code.

        ``financial_statement_items`` reads the table and the adapter tests
        build their fixtures from it, so the pin has to be a literal here:
        otherwise a deletion from the table would move the expectation with
        it and nothing would notice.
        """
        pivot = require_pivot("ths", "financial_statement")

        assert len(pivot.groups["income"]) == 13
        assert pivot.groups["income"][:2] == ("operating_income", "operating_costs")
        assert pivot.groups["income"][-1] == "basic_eps"
        assert len(pivot.groups["balance"]) == 7
        assert "holder_equity_total" in pivot.groups["balance"]
        assert len(pivot.groups["cashflow"]) == 6
        assert "act_cash_flow_net" in pivot.groups["cashflow"]
        # Measured 2026-09-25: every 科目 belongs to exactly one statement.
        groups = [set(columns) for columns in pivot.groups.values()]
        assert groups[0] & groups[1] == groups[0] & groups[2] == groups[1] & groups[2] == set()

    def test_the_passthrough_page_columns_are_pinned(self):
        statement = require_pivot("akshare", "financial_statement")
        indicator = require_pivot("akshare", "financial_indicator")

        assert statement.excluded == frozenset({"数据源", "是否审计", "币种", "类型", "更新日期"})
        assert statement.row_columns == {"report_period": "报告日", "announce_date": "公告日期"}
        assert statement.group_field == "statement_type"
        assert indicator.excluded == frozenset({"SECUCODE"})
        assert indicator.row_columns == {
            "report_period": "REPORT_DATE",
            "announce_date": "NOTICE_DATE",
        }
        # The indicator page is one undivided series: no group key.
        assert indicator.group_field is None
        assert indicator.item_field == "indicator"

    def test_a_melt_does_not_borrow_a_price_caliber(self):
        """A statement has no price basis and no measured suspension shape."""
        for domain in ("financial_statement", "financial_indicator"):
            mapping = require_domain_mapping("akshare", domain)
            assert require_adjust_basis(domain) == "not_applicable"
            assert (mapping.adjust, mapping.suspension, mapping.denominator) == (
                "not_applicable",
                "unmeasured",
                "key_union",
            )

    def test_the_two_financial_legs_cannot_be_compared_yet(self):
        """``unmeasured`` refuses: the legs do not share a merge key (AC-4)."""
        ths = require_domain_mapping("ths", "financial_statement")
        akshare = require_domain_mapping("akshare", "financial_statement")

        with pytest.raises(RuntimeError, match="no measured suspension shape"):
            require_comparable_calibers(ths, akshare)


class TestMeltReaders:
    """The three reads a melter has, and what each refuses."""

    def test_a_1_to_1_domain_declares_no_melt(self):
        with pytest.raises(RuntimeError, match="declares no pivot block"):
            require_pivot("ths", "stock_daily")

    def test_an_unmapped_domain_has_no_melt_to_read(self):
        with pytest.raises(LookupError, match="unknown domain"):
            require_pivot("akshare", "not_a_domain")

    def test_an_undeclared_group_has_no_item_vocabulary(self):
        with pytest.raises(RuntimeError, match="declares no group 'shadow'"):
            require_pivot("ths", "financial_statement").item_columns("shadow")

    def test_a_grouped_melt_refuses_the_frame_s_own_keys(self):
        """``groups`` may not fall back to passthrough: the frame would decide."""
        with pytest.raises(RuntimeError, match="do not decide what a科目 is"):
            require_pivot("ths", "financial_statement").line_items(["thscode", "net_profit"])

    def test_a_passthrough_melt_refuses_item_columns(self):
        with pytest.raises(RuntimeError, match="declares no item groups"):
            require_pivot("akshare", "financial_statement").item_columns("资产负债表")

    def test_passthrough_items_are_the_frame_minus_the_declared_metadata(self):
        pivot = require_pivot("akshare", "financial_statement")
        columns = ["报告日", "货币资金", "数据源", "应收账款", "公告日期", "币种"]

        assert pivot.line_items(columns) == ("货币资金", "应收账款")

    def test_frame_order_is_kept(self):
        pivot = require_pivot("akshare", "financial_indicator")

        assert pivot.line_items(["ROEJQ", "SECUCODE", "EPSJB"]) == ("ROEJQ", "EPSJB")

    def test_row_columns_are_read_from_the_table(self):
        assert require_pivot("ths", "financial_statement").row_column("announce_date") == (
            "report_date_ms"
        )

    def test_an_undeclared_row_field_fails_closed(self):
        """A melt may read a column only because the table names it."""
        with pytest.raises(RuntimeError, match="declares no row column for 'currency'"):
            require_pivot("ths", "financial_statement").row_column("currency")


class TestPivotParsingGuards:
    """A half-declared melt stops the load instead of melting something else."""

    def test_an_unknown_pivot_key_fails_closed(self, tmp_mappings):
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, units="元")})

        with pytest.raises(RuntimeError, match="unknown keys"):
            load_mapping("ths")

    def test_an_unknown_mode_fails_closed(self, tmp_mappings):
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, mode="melt")})

        with pytest.raises(RuntimeError, match="declares mode 'melt'"):
            load_mapping("ths")

    def test_a_melt_into_an_undeclared_field_fails_closed(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, value_field="amount")},
        )

        with pytest.raises(RuntimeError, match="needs value_field to name a declared field"):
            load_mapping("ths")

    def test_item_and_value_cannot_melt_into_one_field(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, value_field="item")},
        )

        with pytest.raises(RuntimeError, match="same field"):
            load_mapping("ths")

    def test_the_two_modes_cannot_both_own_the_item_set(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, excluded=["thscode"])},
        )

        with pytest.raises(RuntimeError, match="allow-list and a deny-list"):
            load_mapping("ths")

    def test_a_groups_map_is_not_read_under_passthrough(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "akshare",
            {"financial_statement": _pivot(_PASSTHROUGH, groups={"income": ["货币资金"]})},
        )

        with pytest.raises(RuntimeError, match="groups map under mode 'passthrough'"):
            load_mapping("akshare")

    def test_a_passthrough_melt_has_to_name_the_page_columns(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "akshare",
            {"financial_statement": _without_pivot_key(_PASSTHROUGH, "excluded")},
        )

        with pytest.raises(RuntimeError, match="needs an 'excluded' list"):
            load_mapping("akshare")

    def test_a_grouped_melt_without_groups_fails_closed(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _without_pivot_key(_GROUPED, "groups")},
        )

        with pytest.raises(RuntimeError, match="mode 'groups' without a groups mapping"):
            load_mapping("ths")

    def test_an_empty_group_fails_closed(self, tmp_mappings):
        groups = {**_GROUPED["pivot"]["groups"], "cashflow": []}
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, groups=groups)})

        with pytest.raises(RuntimeError, match="needs a non-empty column list"):
            load_mapping("ths")

    def test_a_group_may_not_repeat_a_item(self, tmp_mappings):
        groups = {**_GROUPED["pivot"]["groups"], "income": ["net_profit", "net_profit"]}
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, groups=groups)})

        with pytest.raises(RuntimeError, match="repeats a科目"):
            load_mapping("ths")

    def test_a_row_column_cannot_also_be_a_item(self, tmp_mappings):
        """One column cannot be both the row's 披露日 and a 科目 of it."""
        groups = {**_GROUPED["pivot"]["groups"], "income": ["report_date_ms"]}
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, groups=groups)})

        with pytest.raises(RuntimeError, match="both as a科目 and as a row-level column"):
            load_mapping("ths")

    def test_a_group_has_to_be_keyed(self, tmp_mappings):
        """Rows of two statements sharing one business key would overwrite."""
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, group_field="announce_date")},
        )

        with pytest.raises(RuntimeError, match="is not a key field"):
            load_mapping("ths")

    def test_a_grouped_melt_has_to_name_its_group_field(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _without_pivot_key(_GROUPED, "group_field")},
        )

        with pytest.raises(RuntimeError, match="is not a key field"):
            load_mapping("ths")

    def test_row_columns_has_to_be_a_field_to_column_mapping(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, row_columns=["announce_date"])},
        )

        with pytest.raises(RuntimeError, match="row_columns to be a field-to-column mapping"):
            load_mapping("ths")

    def test_a_row_field_has_to_be_a_declared_field(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, row_columns={"currency": "report_currency"})},
        )

        with pytest.raises(RuntimeError, match="row field 'currency'"):
            load_mapping("ths")

    def test_the_melt_cannot_read_a_field_it_produces(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, row_columns={"item": "科目"})},
        )

        with pytest.raises(RuntimeError, match="the melt produces that field itself"):
            load_mapping("ths")

    def test_a_row_column_has_to_be_named(self, tmp_mappings):
        _write_source(
            tmp_mappings,
            "ths",
            {"financial_statement": _pivot(_GROUPED, row_columns={"announce_date": ""})},
        )

        with pytest.raises(RuntimeError, match="needs a column name for row field"):
            load_mapping("ths")

    def test_a_malformed_pivot_block_fails_closed(self, tmp_mappings):
        _write_source(tmp_mappings, "ths", {"financial_statement": {**_GROUPED, "pivot": "income"}})

        with pytest.raises(RuntimeError, match="has a malformed pivot block"):
            load_mapping("ths")

    def test_a_group_list_has_to_be_strings(self, tmp_mappings):
        groups = {**_GROUPED["pivot"]["groups"], "income": [None]}
        _write_source(tmp_mappings, "ths", {"financial_statement": _pivot(_GROUPED, groups=groups)})

        with pytest.raises(RuntimeError, match="needs string columns"):
            load_mapping("ths")


class TestMeltersFollowTheTable:
    """The declaration is load-bearing: change it and the melt moves."""

    def test_the_fuyao_group_accessor_reads_the_table(self, tmp_mappings):
        from opendata_fuyao.endpoints import financial_statement_items

        _write_source(
            tmp_mappings,
            "ths",
            {
                "financial_statement": _pivot(
                    _GROUPED, groups={"income": ["only_this_item"], "balance": ["assets_total"]}
                )
            },
        )

        assert financial_statement_items("income") == ("only_this_item",)

    def test_the_fuyao_group_accessor_refuses_an_undeclared_statement(self, tmp_mappings):
        from opendata_fuyao import FuyaoError
        from opendata_fuyao.endpoints import financial_statement_items

        _write_source(tmp_mappings, "ths", {"financial_statement": _GROUPED})

        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_financial_statement_type"):
            financial_statement_items("cashflow")

    def test_the_sina_melt_drops_what_the_table_declares_as_metadata(self, tmp_mappings):
        from opendata.data.providers.akshare.models.financial_statement import (
            AkshareFinancialStatementFetcher,
        )

        frame = pd.DataFrame(
            {
                "报告日": ["2024-06-30"],
                "公告日期": ["2024-08-30"],
                "货币资金": [5.8e10],
                "更新日": [20240830],  # a numeric page column: an item unless declared
                "数据源": ["新浪"],
            }
        )
        fetcher = AkshareFinancialStatementFetcher()
        params = fetcher.transform_query(symbol="600519", statement_type="资产负债表")
        _write_source(tmp_mappings, "akshare", {"financial_statement": _PASSTHROUGH})

        items = {row.item for row in fetcher.transform_data(frame, params)}
        assert items == {"货币资金", "更新日"}

        _write_source(
            tmp_mappings,
            "akshare",
            {"financial_statement": _pivot(_PASSTHROUGH, excluded=["数据源", "币种", "更新日"])},
        )

        assert {row.item for row in fetcher.transform_data(frame, params)} == {"货币资金"}

    def test_the_em_announce_guard_names_the_declared_column(self, tmp_mappings, monkeypatch):
        import opendata_http
        from opendata.data.protocol import FetchContext
        from opendata.data.providers.akshare.models.financial_indicator import (
            AkshareFinancialIndicatorFetcher,
        )

        frame = pd.DataFrame({"REPORT_DATE": ["2024-06-30"], "EPSJB": [23.88]})
        monkeypatch.setattr(
            opendata_http, "stock_financial_analysis_indicator_em", lambda **kwargs: frame
        )
        indicator = {
            **{k: v for k, v in _PASSTHROUGH.items() if k != "pivot"},
            "key": ["symbol", "report_period", "indicator"],
            "fields": {
                name: {"from": name}
                for name in ("symbol", "report_period", "announce_date", "indicator", "value")
            },
            "pivot": {
                "mode": "passthrough",
                "item_field": "indicator",
                "value_field": "value",
                "row_columns": {"report_period": "REPORT_DATE", "announce_date": "公告日期"},
                "excluded": ["SECUCODE"],
            },
        }
        _write_source(tmp_mappings, "akshare", {"financial_indicator": indicator})

        with pytest.raises(ValueError, match="lacks 公告日期"):
            AkshareFinancialIndicatorFetcher().extract_data(
                AkshareFinancialIndicatorFetcher().transform_query(symbol="600519"),
                FetchContext(),
            )

    def test_the_wide_frame_cannot_be_projected_one_to_one(self):
        """Why a melt is needed at all: the 1:1 reader refuses the wide page."""
        mapping = require_domain_mapping("akshare", "financial_statement")
        wide = pd.DataFrame({"报告日": ["2024-06-30"], "货币资金": [5.8e10], "公告日期": ["x"]})

        with pytest.raises(ValueError, match="missing mapped columns"):
            normalize_frame(wide, mapping)


class TestPivotExport:
    """The melt ships with the mapping, so evidence can name the vocabulary."""

    def test_the_melt_is_exported_with_the_mapping(self):
        from json import loads

        rendered = loads(mapping_as_json(require_domain_mapping("ths", "financial_statement")))

        assert rendered["pivot"]["mode"] == "groups"
        assert rendered["pivot"]["group_field"] == "statement_type"
        assert rendered["pivot"]["row_columns"] == {"announce_date": "report_date_ms"}
        assert len(rendered["pivot"]["groups"]["income"]) == 13
        assert rendered["pivot"]["excluded"] == []

    def test_a_1_to_1_domain_exports_no_item_vocabulary(self):
        from json import loads

        rendered = loads(mapping_as_json(require_domain_mapping("ths", "stock_daily")))

        assert rendered["pivot"] is None

    def test_the_exported_melt_is_the_one_the_reader_returns(self):
        from json import loads

        mapping = require_domain_mapping("akshare", "financial_indicator")
        rendered = loads(mapping_as_json(mapping))

        assert set(rendered["pivot"]["excluded"]) == set(mapping.pivot.excluded)
        assert rendered["pivot"]["item_field"] == mapping.pivot.item_field == "indicator"
