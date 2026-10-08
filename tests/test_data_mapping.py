"""Source field-mapping tests (A4.5, design §8.2).

The mapping table is the per-source contract of the cross-check and
(dwd merge) path: it renames source columns to the contract fields,
converts units and normalizes business keys. Two counterexamples from
the design must fail loudly rather than pass silently:

* a source whose column names do not match the mapping (rejected, not
  ignored);
* a unit that was not converted (the comparison then reports a
  deviation, covered in ``test_cross_check``).

The three domain-level 口径 (``adjust`` / ``suspension`` /
``denominator``) are tested here too, including the loader's refusal to
build a mapping that omits one (AC-9|01: the conventions have to live in
the table, not in whichever Python file happens to use them).
"""

from datetime import date
from json import loads

import pandas as pd
import pytest
import yaml

from opendata.data.mapping import (
    DEFAULT_TOLERANCE,
    FieldMapping,
    load_mapping,
    mapping_sources,
    normalize_frame,
    require_adjust_basis,
    require_comparable_calibers,
    require_domain_mapping,
)


class TestLoadMapping:
    def test_loads_the_shipped_akshare_mapping(self):
        mapping = load_mapping("akshare")

        assert mapping.source == "akshare"
        assert "stock_daily" in mapping.domains
        assert "index_constituent" in mapping.domains

    def test_stock_daily_mapping_matches_the_real_em_columns(self):
        domain = require_domain_mapping("akshare", "stock_daily")

        assert domain.key == ("symbol", "trade_date")
        assert domain.fields["close"].source_column == "收盘"
        assert domain.fields["volume"].scale == 100  # 手 -> 股
        assert domain.fields["symbol"].normalize == "plain"

    def test_price_fields_get_a_looser_tolerance(self):
        domain = require_domain_mapping("akshare", "stock_daily")

        assert domain.tolerance("close") == pytest.approx(1e-4)
        assert domain.tolerance("volume") == DEFAULT_TOLERANCE

    def test_unknown_source_fails_closed(self):
        with pytest.raises(LookupError, match="unknown source"):
            load_mapping("not_a_source")

    def test_unknown_domain_fails_closed(self):
        with pytest.raises(LookupError, match="unknown domain"):
            require_domain_mapping("akshare", "not_a_domain")


class TestNormalizeFrame:
    def test_renames_converts_units_and_normalizes_keys(self):
        raw = pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "股票代码": ["600519.SH"],
                "开盘": [1685.0],
                "最高": [1690.0],
                "最低": [1680.0],
                "收盘": [1688.0],
                "成交量": [30000.0],
                "成交额": [5.06e9],
            }
        )

        normalized = normalize_frame(raw, require_domain_mapping("akshare", "stock_daily"))

        assert set(normalized.columns) >= {"symbol", "trade_date", "close", "volume"}
        row = normalized.iloc[0]
        assert row["symbol"] == "600519"  # 600519.SH -> 600519
        assert row["volume"] == 3_000_000.0  # lots -> shares
        assert row["close"] == 1688.0

    def test_missing_source_column_fails_closed(self):
        """Counterexample 1: unmapped column names must not pass silently."""
        raw = pd.DataFrame({"日期": ["2024-01-02"], "股票代码": ["600519"], "开pan": [1.0]})

        with pytest.raises(ValueError, match="收盘"):
            normalize_frame(raw, require_domain_mapping("akshare", "stock_daily"))

    def test_contract_columns_keep_the_mapping_order(self):
        mapping = require_domain_mapping("akshare", "index_constituent")
        raw = pd.DataFrame(
            {
                "日期": [date(2024, 7, 31)],
                "指数代码": ["000300"],
                "成分券代码": ["600519"],
                "权重": [4.15],
            }
        )

        normalized = normalize_frame(raw, mapping)

        assert list(normalized.columns) == ["index_symbol", "symbol", "as_of", "weight"]


class TestFieldMappingModel:
    def test_defaults_are_identity(self):
        field = FieldMapping(source_column="收盘")

        assert field.scale == 1.0
        assert field.normalize is None


class TestDenormalizeFrame:
    """The write-side inverse that lands contract rows in an ods table."""

    def test_source_key_is_the_ods_spelling_of_the_business_key(self):
        ths = require_domain_mapping("ths", "stock_daily")
        akshare = require_domain_mapping("akshare", "stock_daily")
        assert ths.source_key == ("thscode", "trade_date")
        assert akshare.source_key == ("股票代码", "日期")

    def test_contract_rows_return_to_the_source_columns_with_units_undone(self):
        from opendata.data.mapping import denormalize_frame

        mapping = require_domain_mapping("akshare", "stock_daily")
        source = pd.DataFrame(
            {
                "股票代码": ["600519.SH"],
                "日期": [date(2024, 1, 2)],
                "开盘": [1.0],
                "最高": [2.0],
                "最低": [0.5],
                "收盘": [1.5],
                "成交量": [10.0],
                "成交额": [15.0],
            }
        )
        back = denormalize_frame(normalize_frame(source, mapping), mapping)
        assert list(back.columns) == list(source.columns)
        # The 手 -> 股 scale is undone; a plain-normalized key cannot be
        # re-suffixed, which is why this path serves feeds that deliver
        # the source symbol (the fuyao bars), not the akshare writes.
        assert list(back["成交量"]) == [10.0]
        assert list(back["股票代码"]) == ["600519"]

    def test_a_declared_millisecond_column_is_derived_back(self):
        from opendata.data.mapping import denormalize_frame
        from opendata.pipeline.dump_import import shanghai_dates

        mapping = require_domain_mapping("ths", "stock_daily")
        frame = pd.DataFrame(
            {
                "symbol": ["600519.SH"],
                "trade_date": [date(2024, 1, 2)],
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [10.0],
                "amount": [15.0],
            }
        )
        projected = denormalize_frame(frame, mapping)
        assert list(shanghai_dates(projected["date_ms"])) == [date(2024, 1, 2)]
        assert projected["date_ms"].dtype == "int64"
        assert projected["date_ms"].iloc[0] == 1_704_124_800_000

    def test_millisecond_conversion_handles_timezone_and_nat(self):
        from opendata.data.mapping import denormalize_frame

        mapping = require_domain_mapping("ths", "stock_daily")
        frame = pd.DataFrame(
            {
                "symbol": ["600519.SH", "000001.SZ"],
                "trade_date": pd.Series(
                    [pd.Timestamp("2024-01-01T16:00:00Z"), pd.NaT], dtype="object"
                ),
                "open": [1.0, 1.0],
                "high": [2.0, 2.0],
                "low": [0.5, 0.5],
                "close": [1.5, 1.5],
                "volume": [10.0, 10.0],
                "amount": [15.0, 15.0],
            }
        )

        projected = denormalize_frame(frame, mapping)

        assert projected["date_ms"].iloc[0] == 1_704_124_800_000
        assert pd.isna(projected["date_ms"].iloc[1])
        assert str(projected["date_ms"].dtype) == "Int64"

    def test_the_index_leg_lands_in_ods_and_reads_back_as_contract_rows(self):
        """C5: the ths index bars take the same write path as the stock bars."""
        from opendata.data.mapping import denormalize_frame
        from opendata.pipeline.dump_import import shanghai_dates

        mapping = require_domain_mapping("ths", "index_daily")
        assert mapping.source_key == ("thscode", "trade_date")

        contract = pd.DataFrame(
            {
                "symbol": ["000300.SH"],
                "trade_date": [date(2026, 9, 24)],
                "open": [4618.73],
                "high": [4640.08],
                "low": [4604.77],
                "close": [4611.44],
                "volume": [21486958000.0],
                "amount": [535708360000.0],
            }
        )
        source = denormalize_frame(contract, mapping)

        assert list(source.columns[:8]) == [
            "thscode",
            "trade_date",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
        ]
        assert source["thscode"].tolist() == ["000300.SH"]  # ods keeps the source spelling
        assert list(shanghai_dates(source["date_ms"])) == [date(2026, 9, 24)]

        back = normalize_frame(source, mapping)

        assert back["symbol"].tolist() == ["000300"]  # dwd keeps the plain key
        assert back["close"].tolist() == [4611.44]
        assert back["amount"].tolist() == [535708360000.0]
        assert back["volume"].tolist() == [21486958000.0]  # 股，无 手 换算

    def test_the_derivative_legs_keep_their_exchange_suffix(self):
        """C6: futures/option bars round-trip with the qualified code intact.

        Stripping the suffix (what ``index_daily`` does) would collide across
        exchanges and recycle codes after delivery, so these two mappings
        declare no ``normalize`` and the row read back must still say
        ``RB2610.SHF``.
        """
        from opendata.data.mapping import denormalize_frame
        from opendata.pipeline.dump_import import shanghai_dates

        legs = (("futures_daily", "RB2610.SHF"), ("option_daily", "MO2612-C-7600.CFE"))
        for domain, symbol in legs:
            mapping = require_domain_mapping("ths", domain)
            assert mapping.source_key == ("thscode", "trade_date")
            contract = pd.DataFrame(
                {
                    "symbol": [symbol],
                    "trade_date": [date(2026, 6, 1)],
                    "open": [3156.0],
                    "high": [3189.0],
                    "low": [3156.0],
                    "close": [3177.0],
                    "volume": [679167.0],
                    "amount": [21554984000.0],
                }
            )

            source = denormalize_frame(contract, mapping)
            back = normalize_frame(source, mapping)

            assert source["thscode"].tolist() == [symbol]
            assert list(shanghai_dates(source["timestamp"])) == [date(2026, 6, 1)]
            assert back["symbol"].tolist() == [symbol]
            assert back["close"].tolist() == [3177.0]
            assert back["amount"].tolist() == [21554984000.0]

    def test_a_missing_contract_field_fails_closed(self):
        from opendata.data.mapping import denormalize_frame

        mapping = require_domain_mapping("ths", "stock_daily")
        with pytest.raises(ValueError, match="fail closed"):
            denormalize_frame(pd.DataFrame([{"symbol": "600519.SH"}]), mapping)


#: A domain entry that satisfies the loader, used as the base each case
#: below breaks in exactly one place.
_VALID_DOMAIN = {
    "key": ["symbol", "trade_date"],
    "adjust": "unadjusted",
    "suspension": "absent_row",
    "denominator": "key_union",
    "fields": {
        "symbol": {"from": "symbol"},
        "trade_date": {"from": "trade_date"},
        "close": {"from": "close"},
    },
}


def _write_source(tmp_path, source: str, domains: dict) -> None:
    """Write one mapping file into a tmp directory (the loader reads it there)."""
    payload = {"version": 1, "source": source, "domains": domains}
    (tmp_path / f"{source}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


@pytest.fixture
def tmp_mappings(tmp_path, monkeypatch):
    """Point the loader at an empty directory and keep the real cache intact."""
    from opendata.data import mapping as mapping_module

    monkeypatch.setattr(mapping_module, "_MAPPINGS_DIR", tmp_path)
    mapping_module.load_mapping.cache_clear()
    yield tmp_path
    mapping_module.load_mapping.cache_clear()


class TestDomainCaliberDeclarations:
    """The three domain-level 口径 are table fields, not code preferences."""

    def test_every_shipped_domain_declares_all_three(self):
        for source in mapping_sources():
            for domain in load_mapping(source).domains.values():
                assert domain.adjust in {"unadjusted", "not_applicable"}, domain
                assert domain.suspension in {
                    "absent_row",
                    "zero_price_row",
                    "not_applicable",
                    "unmeasured",
                }, domain
                assert domain.denominator == "key_union", domain

    def test_the_stock_daily_legs_declare_the_measured_conventions(self):
        """C53 measured these from ods, so a drift here is a real drift.

        ``scripts/ops/suspension_shape_check.py``: ths' ``adjusted`` column is
        ``none`` on every row and neither leg emits a 0-price halted bar
        (zero_close=0) while both omit whole trading days (8,240 / 55 days).
        """
        for source in ("ths", "akshare"):
            domain = require_domain_mapping(source, "stock_daily")
            assert (domain.adjust, domain.suspension, domain.denominator) == (
                "unadjusted",
                "absent_row",
                "key_union",
            )

    def test_the_derivative_legs_declare_unmeasured_instead_of_a_guess(self):
        """No ``ods_futures_daily_ths``/``ods_option_daily_ths`` exists, so the
        suspension shape of those legs has no evidence: the table says so."""
        for domain_name in ("futures_daily", "option_daily"):
            assert require_domain_mapping("ths", domain_name).suspension == "unmeasured"

    def test_an_undeclared_caliber_fails_closed(self, tmp_mappings):
        domain = dict(_VALID_DOMAIN)
        del domain["suspension"]
        _write_source(tmp_mappings, "ths", {"stock_daily": domain})

        with pytest.raises(RuntimeError, match="does not declare 'suspension'"):
            load_mapping("ths")

    def test_a_caliber_outside_the_enum_fails_closed(self, tmp_mappings):
        domain = {**_VALID_DOMAIN, "adjust": "qfq"}
        _write_source(tmp_mappings, "ths", {"stock_daily": domain})

        with pytest.raises(RuntimeError, match="declares adjust='qfq'"):
            load_mapping("ths")

    def test_an_unknown_domain_key_fails_closed(self, tmp_mappings):
        domain = {**_VALID_DOMAIN, "halted_as_zero": True}
        _write_source(tmp_mappings, "ths", {"stock_daily": domain})

        with pytest.raises(RuntimeError, match="unknown keys"):
            load_mapping("ths")

    def test_the_calibers_are_exported_with_the_mapping(self):
        from opendata.data.mapping import mapping_as_json

        rendered = loads(mapping_as_json(require_domain_mapping("ths", "stock_daily")))

        assert rendered["adjust"] == "unadjusted"
        assert rendered["suspension"] == "absent_row"
        assert rendered["denominator"] == "key_union"


class TestCaliberReaders:
    """What the two seams do with the declarations."""

    def test_agreeing_legs_give_one_price_basis(self):
        assert require_adjust_basis("stock_daily") == "unadjusted"

    def test_an_unmapped_domain_has_no_basis_to_claim(self):
        with pytest.raises(LookupError, match="price basis is unknown"):
            require_adjust_basis("not_a_domain")

    def test_disagreeing_bases_refuse_the_domain(self, tmp_mappings):
        _write_source(tmp_mappings, "ths", {"stock_daily": _VALID_DOMAIN})
        _write_source(
            tmp_mappings,
            "akshare",
            {"stock_daily": {**_VALID_DOMAIN, "adjust": "not_applicable"}},
        )

        with pytest.raises(RuntimeError, match="cannot merge two price bases"):
            require_adjust_basis("stock_daily")

    def test_comparable_calibers_come_back_from_the_table(self):
        ths = require_domain_mapping("ths", "stock_daily")
        akshare = require_domain_mapping("akshare", "stock_daily")

        assert require_comparable_calibers(ths, akshare) == ("key_union", "absent_row")

    def test_different_suspension_shapes_refuse_the_compare(self):
        from opendata.data.mapping import DomainMapping

        ths = require_domain_mapping("ths", "stock_daily")
        zero_rows = DomainMapping(
            domain="stock_daily",
            key=ths.key,
            fields=ths.fields,
            adjust="unadjusted",
            suspension="zero_price_row",
            denominator="key_union",
        )

        with pytest.raises(RuntimeError, match="halted days differently"):
            require_comparable_calibers(ths, zero_rows)

    def test_an_unmeasured_shape_refuses_the_compare(self):
        from opendata.data.mapping import DomainMapping

        ths = require_domain_mapping("ths", "stock_daily")
        unmeasured = DomainMapping(
            domain="stock_daily",
            key=ths.key,
            fields=ths.fields,
            adjust="unadjusted",
            suspension="unmeasured",
            denominator="key_union",
        )

        with pytest.raises(RuntimeError, match="halted days differently"):
            require_comparable_calibers(ths, unmeasured)
        with pytest.raises(RuntimeError, match="no measured suspension shape"):
            require_comparable_calibers(unmeasured, unmeasured)

    def test_a_denominator_the_compare_does_not_implement_refuses(self):
        from opendata.data.mapping import DomainMapping

        ths = require_domain_mapping("ths", "stock_daily")
        other_side = DomainMapping(
            domain="stock_daily",
            key=ths.key,
            fields=ths.fields,
            adjust="unadjusted",
            suspension="absent_row",
            denominator="left_only",
        )

        with pytest.raises(RuntimeError, match="denominator 'key_union'"):
            require_comparable_calibers(ths, other_side)
