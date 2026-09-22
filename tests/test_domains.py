"""Domain registry tests (A1.3 / AC-2).

Registry consistency: every domain derives its table names, REST path
and WS event from one place, derived names never collide, and the
authority baseline only references registered domains.
"""

from pathlib import Path

import pytest

from opendata.data import domains as domains_module
from opendata.data.domains import (
    contract_model,
    display_name,
    dwd_table,
    load_domains,
    ods_table,
    parse_domains,
    require_domain,
    rest_path,
    ws_push_event,
)
from opendata.data.models import Bar
from opendata.data.registry import authority_baseline


def valid_yaml_text():
    """A minimal valid registry for the parse tests."""
    return """
version: 1
domains:
  stock_daily:
    display_name: A股日线
    rest_path: stock/daily
    contract: Bar
"""


class TestRegistry:
    def test_all_p0_domains_registered(self):
        registered = set(load_domains())
        assert {
            "stock_daily",
            "stock_adjust",
            "stock_action",
            "financial_statement",
            "financial_indicator",
            "index_constituent",
            "futures_daily",
            "futures_fundamentals",
            "instrument",
            "trading_calendar",
        } <= registered

    def test_every_domain_derives_all_names(self):
        for domain in load_domains():
            assert dwd_table(domain) == f"dwd_{domain}"
            assert rest_path(domain).startswith("/api/v1/data/")
            assert ws_push_event(domain) == f"data.{domain}"
            assert display_name(domain)
            assert contract_model(domain) is not None

    def test_authority_baseline_only_references_registered_domains(self):
        # Cross-file consistency: routing never sees an unknown domain.
        assert set(authority_baseline()) <= set(load_domains())


class TestDerivations:
    def test_ods_table(self):
        assert ods_table("stock_daily", "akshare") == "ods_stock_daily_akshare"

    def test_dwd_table(self):
        assert dwd_table("stock_daily") == "dwd_stock_daily"

    def test_rest_path(self):
        assert rest_path("stock_daily") == "/api/v1/data/stock/daily"
        assert rest_path("instrument") == "/api/v1/data/meta/instruments"

    def test_ws_push_event(self):
        assert ws_push_event("stock_daily") == "data.stock_daily"

    def test_contract_model(self):
        assert contract_model("stock_daily") is Bar
        assert contract_model("futures_daily") is Bar

    def test_unknown_domain_fails_closed(self):
        with pytest.raises(LookupError, match="unknown domain"):
            require_domain("nope")
        with pytest.raises(LookupError, match="unknown domain"):
            ods_table("nope", "akshare")
        with pytest.raises(LookupError, match="unknown domain"):
            rest_path("nope")

    def test_invalid_source_fails_closed(self):
        with pytest.raises(ValueError, match="invalid source identifier"):
            ods_table("stock_daily", "Bad-Source")
        with pytest.raises(ValueError, match="invalid source identifier"):
            ods_table("stock_daily", "")


class TestParseValidation:
    def test_valid_text_parses(self):
        specs = parse_domains(valid_yaml_text())
        assert specs["stock_daily"].display_name == "A股日线"

    def test_malformed_yaml_rejected(self):
        with pytest.raises(ValueError, match="not valid YAML"):
            parse_domains("{{{")

    def test_missing_domains_key_rejected(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_domains("version: 1")

    def test_empty_domains_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_domains("domains: {}")

    def test_invalid_domain_id_rejected(self):
        text = valid_yaml_text().replace("stock_daily:", "Stock-Daily:")
        with pytest.raises(ValueError, match="invalid domain identifier"):
            parse_domains(text)

    def test_unknown_field_rejected(self):
        text = valid_yaml_text().replace("contract: Bar", "contract: Bar\n  extra: 1")
        with pytest.raises(ValueError):
            parse_domains(text)

    def test_missing_field_rejected(self):
        text = valid_yaml_text().replace("    rest_path: stock/daily\n", "")
        with pytest.raises(ValueError):
            parse_domains(text)

    def test_unknown_contract_rejected(self):
        text = valid_yaml_text().replace("contract: Bar", "contract: Nope")
        with pytest.raises(ValueError, match="unknown contract model"):
            parse_domains(text)

    def test_single_segment_rest_path_rejected(self):
        text = valid_yaml_text().replace("rest_path: stock/daily", "rest_path: stock")
        with pytest.raises(ValueError, match="invalid rest_path"):
            parse_domains(text)

    def test_duplicate_rest_path_rejected(self):
        text = (
            valid_yaml_text()
            + """
  futures_daily:
    display_name: 期货日线
    rest_path: stock/daily
    contract: Bar
"""
        )
        with pytest.raises(ValueError, match="rest_path .* used by"):
            parse_domains(text)

    def test_empty_display_name_rejected(self):
        text = valid_yaml_text().replace("display_name: A股日线", 'display_name: "  "')
        with pytest.raises(ValueError, match="display_name"):
            parse_domains(text)

    def test_oversized_domain_id_rejected(self):
        long_id = "a" * 64
        text = valid_yaml_text().replace("stock_daily:", f"{long_id}:")
        with pytest.raises(ValueError, match="exceeds"):
            parse_domains(text)

    def test_load_domains_fails_closed_on_missing_file(self, monkeypatch):
        domains_module.load_domains.cache_clear()
        monkeypatch.setattr(domains_module, "_DOMAINS_PATH", Path("/nonexistent.yaml"))
        with pytest.raises(RuntimeError, match="unreadable"):
            load_domains()
        domains_module.load_domains.cache_clear()
