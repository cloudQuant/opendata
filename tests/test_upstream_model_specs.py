"""The sheet's column rule, judged on both sides against fixture sources and the pinned tree.

``output_columns`` is what ``declaration_provenance`` demands of a declaration, so a sheet that
over-reports would refuse a correct declaration and a sheet that under-reports would bless an
invented column. Each arm here therefore shows the rule *firing* and *declining*: a construction the
spec file proves narrows the population, and a construction it does not prove leaves the class field
list standing. The live-tree test is the invariance witness -- the three cboe entries and the H.6
entry must read exactly as they did before the rule existed, because nothing in those files proves a
closed column set.
"""

from __future__ import annotations

import ast
import hashlib
import textwrap

import pytest

from scripts.quality.upstream_model_specs import (
    _SELF_TEST_CASES,
    DEFAULT_UPSTREAM_PATH,
    UPSTREAM_COMMIT,
    _declared_fields,
    _output_columns_of,
    extract,
    main,
    run_self_test,
)


def published(source: str, fields: list[str]) -> tuple[list[str], str]:
    """The columns the sheet would publish for ``fields`` under one spec file's data path."""
    tree = ast.parse(textwrap.dedent(source))
    return _output_columns_of(tree, [{"name": name} for name in fields])


POSITIONAL = """
    maturities = ["month_1", "year_1"]

    class ProviderData(StandardData):
        @staticmethod
        def extract_data(query, **kwargs):
            df = read(url)
            df.columns = ["date"] + maturities
            return df
"""


def test_a_positional_rename_demotes_a_field_the_path_cannot_fill() -> None:
    """The fact this rule exists for: upstream names its columns, so the class does not get to."""
    columns, basis = published(POSITIONAL, ["date", "week_4", "month_1", "year_1", "month_2"])
    assert basis == "built"
    assert columns == ["date", "month_1", "year_1"]


UNREADABLE = """
    maturities = ["month_1", "year_1"]

    class ProviderData(StandardData):
        @staticmethod
        def extract_data(query, **kwargs):
            df = read(url)
            df.columns = order_of(df)
            return df
"""


def test_an_unreadable_rename_demotes_nothing() -> None:
    """A set built from the response is not a set this reader can name, so the class survives."""
    columns, basis = published(UNREADABLE, ["date", "week_4", "month_1", "year_1"])
    assert basis == "declared"
    assert columns == ["date", "week_4", "month_1", "year_1"]


def test_a_rename_outside_the_data_path_demotes_nothing() -> None:
    """``transform_query`` builds the request, not the records, so its rename is not evidence."""
    columns, basis = published(
        POSITIONAL.replace("def extract_data(", "def transform_query("),
        ["date", "week_4", "month_1", "year_1"],
    )
    assert basis == "declared"
    assert "week_4" in columns


def test_a_rename_in_a_nested_helper_demotes_nothing() -> None:
    """A helper called by the data path may run under a condition the reader cannot see."""
    helper = """
        maturities = ["month_1"]

        class ProviderData(StandardData):
            @staticmethod
            def extract_data(query, **kwargs):
                return shape(read(url))

            def shape(df):
                df.columns = ["date"] + maturities
                return df
        """
    assert published(helper, ["date", "month_1", "week_4"])[1] == "declared"


DICT_LITERALS = """
    titles = {"M1": "m1", "M2": "m2"}

    class ProviderData(StandardData):
        @staticmethod
        def extract_data(query, **kwargs):
            df = read(url)
            df.columns = ["month"] + list(titles.values())
            return df
"""

DICT_KEYS = DICT_LITERALS.replace("titles.values()", "titles.keys()")


def test_a_dict_literal_names_the_columns_it_writes() -> None:
    """A spec file that maps its series ids to field names closes the set itself.

    ``money_measures.py:14-22`` binds the map and line 84 takes its *values*, which are the field
    names. Taking the keys instead would name the raw ids, and then the fields that do not match an
    id are demoted -- that is what an alias map exists to prevent, so the arm asserts both halves.
    """
    fields = ["month", "m1", "m2", "unpublished"]
    assert published(DICT_LITERALS, fields) == (["month", "m1", "m2"], "built")
    assert published(DICT_KEYS, fields) == (["month"], "built")


MELT = """
    class ProviderData(StandardData):
        @staticmethod
        def transform_data(query, data, **kwargs):
            df = data.reset_index()
            df.columns = ["date"] + ["month_1", "month_3"]
            df = df.melt(id_vars="date", var_name="maturity", value_name="rate")
            return df
"""


def test_a_melt_replaces_the_rename_it_consumes() -> None:
    """The wide columns become rows, so the melted frame's names are the published columns."""
    columns, basis = published(MELT, ["date", "maturity", "rate", "month_1", "month_3"])
    assert basis == "built"
    assert columns == ["date", "maturity", "rate"]


def test_a_melt_publishes_the_names_it_creates() -> None:
    """``extra="allow"`` is what lets a reshaped key reach a record without a declared field."""
    columns = published(MELT, ["date", "month_1", "month_3"])[0]
    assert columns == ["date", "maturity", "rate"]


ALIAS = """
    class ProviderData(StandardData):
        __alias_dict__ = {"symbol": "ticker"}

        @staticmethod
        def extract_data(query, **kwargs):
            df = read(url)
            df.columns = ["date", "ticker"]
            return df
"""


def test_an_alias_maps_the_raw_key_back_to_the_field() -> None:
    """A positional rename names the record key while the field carries the alias."""
    columns, basis = published(ALIAS, ["date", "symbol", "week_4"])
    assert basis == "built"
    assert columns == ["date", "symbol"]


def test_a_field_the_provider_declares_itself_is_never_demoted() -> None:
    """A column written in the provider's own class body is published by that provider."""
    source = """
        class ProviderData(StandardData):
            derived: str = Field(default="")

            @staticmethod
            def extract_data(query, **kwargs):
                df = read(url)
                df.columns = ["date"]
                return df
        """
    assert published(source, ["date", "derived", "week_4"])[0] == ["date", "derived"]


def test_a_computed_field_counts_as_a_declared_column() -> None:
    """A ``@computed_field`` property is a column of ``model_dump()`` like any annotated field."""
    tree = ast.parse(
        textwrap.dedent(
            """
            class YieldCurveData(Data):
                date: dateType = Field(default=None)

                @computed_field
                @property
                def maturity_years(self) -> float:
                    return 1.0

                @property
                def not_published(self) -> str:
                    return ""
            """
        )
    )
    cls = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    assert [field["name"] for field in _declared_fields(cls)] == ["date", "maturity_years"]
    assert _declared_fields(cls)[1]["type"] == "float"


def test_an_inherited_field_the_path_writes_is_still_required() -> None:
    """The keep half of the rule, stated on its own: a rename that names it keeps it demanded."""
    columns = published(POSITIONAL, ["date", "week_4", "month_1", "year_1", "month_2"])[0]
    assert "month_1" in columns and "year_1" in columns
    assert "week_4" not in columns and "month_2" not in columns


def test_the_extractor_ships_its_own_two_sided_proof() -> None:
    """``run_self_test`` is the proof ``main`` gates every sheet write on."""
    arms, fired, notes = run_self_test()
    assert notes == [], notes
    assert fired == arms
    assert arms >= 8, "every clause needs a keep arm and a drop arm"
    labels = [case[0] for case in _SELF_TEST_CASES]
    assert any("demotes the inherited" in label for label in labels), labels
    assert any("keeps every field it declares" in label for label in labels), labels
    assert any("keeps the tenors it writes" in label for label in labels), labels


def test_main_refuses_to_write_a_sheet_when_the_rule_is_unproven(tmp_path, monkeypatch) -> None:
    """A reader whose own proof does not fire may not become the judge's ground truth."""
    label, source, fields, _expected, computed = _SELF_TEST_CASES[0]
    broken = ((label, source, fields, ["not", "the", "answer"], computed),)
    monkeypatch.setattr("scripts.quality.upstream_model_specs._SELF_TEST_CASES", broken)
    out = tmp_path / "sheet.json"
    assert main(["--out", str(out)]) == 2
    assert not out.exists()


#: What the pinned tree must keep reading, model by model: the four entries whose files prove no
#: closed set (so the class field list must stand), and the entries the rule does fire on.
LIVE_DECLARED = {
    "cboe::AvailableIndices": 12,
    "cboe::IndexConstituents": 15,
    "cboe::IndexSearch": 11,
    "fred::EconomicCalendar": 13,
    "yfinance::IndexHistorical": 7,
}


@pytest.mark.skipif(
    not DEFAULT_UPSTREAM_PATH.is_dir(),
    reason=f"the pinned upstream tree {DEFAULT_UPSTREAM_PATH} is not on this machine",
)
def test_the_rule_fires_only_where_the_source_proves_it() -> None:
    """Invariance on the five files that prove nothing, plus the two the audited models read.

    ``fred/economic_calendar.py:67-71`` renames a page table that is concatenated into something
    wider and ``yfinance/index_historical.py:101`` renames in the query path, so both must stay on
    the class field list; a rule that fired there would be narrowing columns the endpoint publishes.
    """
    sheet = extract(DEFAULT_UPSTREAM_PATH)
    models = sum(data["declared_model_count"] for data in sheet.values())
    assert len(sheet) == 32, "the provider denominator must not move"
    assert models == 350, "the model denominator must not move"

    for label, count in LIVE_DECLARED.items():
        source, name = label.split("::")
        entry = sheet[source]["models"][name]
        assert entry["column_basis"] == "declared", label
        assert len(entry["output_columns"]) == count, label

    treasury = sheet["federal_reserve"]["models"]["TreasuryRates"]
    assert treasury["column_basis"] == "built"
    assert treasury["output_columns"] == [
        "date",
        "month_1",
        "month_3",
        "month_6",
        "year_1",
        "year_2",
        "year_3",
        "year_5",
        "year_7",
        "year_10",
        "year_20",
        "year_30",
    ], "upstream renames to exactly these twelve (treasury_rates.py:96)"

    money = sheet["federal_reserve"]["models"]["MoneyMeasures"]
    assert money["column_basis"] == "built"
    assert money["output_columns"] == [
        "month",
        "m1",
        "m2",
        "currency",
        "demand_deposits",
        "retail_money_market_funds",
        "other_liquid_deposits",
        "small_denomination_time_deposits",
    ], "the eight names titles maps to (money_measures.py:14-22, 84)"

    curve = sheet["federal_reserve"]["models"]["YieldCurve"]
    assert curve["column_basis"] == "built"
    assert curve["output_columns"] == ["date", "maturity", "maturity_years", "rate"], (
        "the long triple the endpoint publishes, plus the computed column the shared model derives "
        "from maturity (standard_models/yield_curve.py:55-60)"
    )
    assert curve["spec_file"].endswith("models/yield_curve.py")


@pytest.mark.skipif(
    not DEFAULT_UPSTREAM_PATH.is_dir(),
    reason=f"the pinned upstream tree {DEFAULT_UPSTREAM_PATH} is not on this machine",
)
def test_every_demoted_entry_is_reproducible_from_the_bytes_it_cites() -> None:
    """``spec_sha256`` is the file the rule read, so a demotion can be re-derived later."""
    sheet = extract(DEFAULT_UPSTREAM_PATH)
    built = [
        entry
        for data in sheet.values()
        for entry in data["models"].values()
        if entry["column_basis"] == "built"
    ]
    assert len(built) == 21, "the demoted population is a measured count, not a claim"
    for entry in built:
        on_disk = (DEFAULT_UPSTREAM_PATH / entry["spec_file"]).read_text(encoding="utf-8")
        assert hashlib.sha256(on_disk.encode()).hexdigest() == entry["spec_sha256"]
    assert UPSTREAM_COMMIT == "3e071fcc2cd9f891cac6040ae60296dba76dab46"
