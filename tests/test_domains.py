"""Domain registry tests (A1.3 / AC-2).

Registry consistency: every domain derives its table names, REST path
and WS event from one place, derived names never collide, and the
authority baseline only references registered domains.
"""

from collections import UserDict
from pathlib import Path

import pytest
import yaml

from opendata.data import domains as domains_module
from opendata.data import models as models_module
from opendata.data.domains import (
    DomainSpec,
    contract_model,
    display_name,
    dwd_table,
    load_domains,
    ods_table,
    parse_domains,
    require_domain,
    require_domain_semantics,
    rest_path,
    ws_push_event,
)
from opendata.data.models import Bar
from opendata.data.providers.bls import models as bls_models
from opendata.data.providers.fmp import models as fmp_models
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


def valid_v2_yaml_text():
    """A complete synthetic registry using native BLS and quote semantics."""
    return (
        "version: 2\n"
        "domains:\n"
        "  bls_observation:\n"
        "    display_name: BLS observation\n"
        "    rest_path: macro/bls-observation\n"
        "    contract: BlsObservation\n"
        "    temporal_kind: series\n"
        "    time_field: year\n"
        "    natural_key: [series_id, year, period]\n"
        "    filter_dims: [year, period]\n"
        "    storage_mode: upsert\n"
        "    permissions: [query, store]\n"
        "  equity_quote:\n"
        "    display_name: Equity quote snapshot\n"
        "    rest_path: equity/quote\n"
        "    contract: EquityQuote\n"
        "    temporal_kind: snapshot\n"
        "    time_field: null\n"
        "    natural_key: [symbol]\n"
        "    filter_dims: [symbol]\n"
        "    storage_mode: transient\n"
        "    permissions: []\n"
    )


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
        } <= registered

    def test_explicit_p0_priorities_match_requirement_scope(self):
        expected = {
            "stock_daily",
            "stock_adjust",
            "stock_action",
            "financial_statement",
            "financial_indicator",
            "index_constituent",
            "futures_daily",
            "futures_fundamentals",
        }

        assert {domain for domain, spec in load_domains().items() if spec.priority == "P0"} == (
            expected
        )
        assert all(
            spec.priority is None
            for domain, spec in load_domains().items()
            if domain not in expected
        )

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
        assert specs["stock_daily"].priority is None

    def test_priority_must_be_a_known_explicit_tier(self):
        text = valid_yaml_text().replace("contract: Bar", "contract: Bar\n    priority: P3")

        with pytest.raises(ValueError):
            parse_domains(text)

    def test_explicit_priority_is_parsed(self):
        text = valid_yaml_text().replace("contract: Bar", "contract: Bar\n    priority: P0")

        assert parse_domains(text)["stock_daily"].priority == "P0"

    def test_legacy_domain_semantics_remain_unreviewed_and_fail_closed(self):
        specs = parse_domains(valid_yaml_text())

        assert not specs["stock_daily"].semantics_declared
        assert specs["stock_daily"].natural_key == ()
        with pytest.raises(LookupError, match="semantics are not declared"):
            require_domain_semantics("stock_daily")

    def test_version_defaults_to_unreviewed_v1(self):
        text = valid_yaml_text().replace("version: 1\n", "")

        assert not parse_domains(text)["stock_daily"].semantics_declared

    def test_v2_complete_semantics_preserve_native_bls_and_snapshot_fields(self, monkeypatch):
        specs = parse_domains(valid_v2_yaml_text())
        bls = specs["bls_observation"]
        quote = specs["equity_quote"]

        assert bls.semantics_declared
        assert bls.temporal_kind == "series"
        assert bls.time_field == "year"
        assert bls.natural_key == ("series_id", "year", "period")
        assert bls.filter_dims == ("year", "period")
        assert "date" not in models_module.BlsObservation.model_fields
        assert "period" in models_module.BlsObservation.model_fields
        assert quote.semantics_declared
        assert quote.temporal_kind == "snapshot"
        assert quote.time_field is None
        assert quote.permissions == ()
        document_text = valid_v2_yaml_text().replace(
            "    temporal_kind: snapshot\n", "    temporal_kind: document\n"
        )
        assert parse_domains(document_text)["equity_quote"].time_field is None

        monkeypatch.setattr(domains_module, "load_domains", lambda: specs)
        assert require_domain_semantics("bls_observation") is bls
        with pytest.raises((AttributeError, ValueError)):
            quote.semantics_declared = False

    def test_domain_spec_mapping_validation_requires_complete_semantics(self):
        partial = UserDict(
            {
                "display_name": "Quote snapshot",
                "rest_path": "equity/quote",
                "contract": "EquityQuote",
                "temporal_kind": "snapshot",
                "natural_key": ["symbol"],
                "storage_mode": "transient",
            }
        )
        complete = UserDict(
            {
                "display_name": "Quote snapshot",
                "rest_path": "equity/quote",
                "contract": "EquityQuote",
                "temporal_kind": "snapshot",
                "time_field": None,
                "natural_key": ["symbol"],
                "filter_dims": ["symbol"],
                "storage_mode": "transient",
                "permissions": [],
            }
        )

        with pytest.raises(ValueError, match="semantics fields must be declared together"):
            DomainSpec.model_validate(partial)
        spec = DomainSpec.model_validate(complete)
        assert spec.semantics_declared
        assert spec.time_field is None
        assert spec.natural_key == ("symbol",)
        assert spec.filter_dims == ("symbol",)

    def test_legacy_and_declared_domain_specs_cannot_be_mutated(self):
        legacy = parse_domains(valid_yaml_text())["stock_daily"]
        declared = parse_domains(valid_v2_yaml_text())["equity_quote"]
        legacy_original = legacy.model_dump()
        declared_original = declared.model_dump()

        with pytest.raises(ValueError):
            legacy.temporal_kind = "snapshot"
        with pytest.raises(ValueError):
            declared.temporal_kind = "series"
        with pytest.raises(ValueError):
            declared.semantics_declared = False

        assert legacy.model_dump() == legacy_original
        assert declared.model_dump() == declared_original

    def test_complete_semantics_are_allowed_in_v1(self):
        text = valid_v2_yaml_text().replace("version: 2", "version: 1")

        assert parse_domains(text)["bls_observation"].semantics_declared

    def test_v2_requires_complete_semantics_for_each_domain(self):
        text = valid_yaml_text().replace("version: 1", "version: 2")

        with pytest.raises(ValueError, match="version 2 requires all six semantics fields"):
            parse_domains(text)

    def test_partial_semantics_are_rejected_in_v1(self):
        text = valid_yaml_text().replace(
            "    contract: Bar\n", "    contract: Bar\n    temporal_kind: snapshot\n"
        )

        with pytest.raises(ValueError, match="semantics fields must be declared together"):
            parse_domains(text)

    def test_event_semantics_require_a_real_time_field(self):
        text = (
            valid_v2_yaml_text()
            .replace("    temporal_kind: series\n", "    temporal_kind: event\n")
            .replace("    time_field: year\n", "    time_field: null\n")
        )

        with pytest.raises(ValueError, match="event domains require a time_field"):
            parse_domains(text)

    @pytest.mark.parametrize("version_yaml", ["0", "3", "1.0", '"2"', "true", "false", "null"])
    def test_invalid_or_boolean_registry_versions_are_rejected(self, version_yaml):
        text = valid_yaml_text().replace("version: 1", f"version: {version_yaml}")

        with pytest.raises(ValueError, match="version must be integer 1 or 2"):
            parse_domains(text)

    @pytest.mark.parametrize(
        "text",
        [
            valid_yaml_text().replace("version: 1\n", "version: 1\nversion: 1\n"),
            valid_yaml_text().replace(
                "    contract: Bar\n", "    contract: Bar\n    contract: Bar\n"
            ),
            valid_yaml_text().replace(
                "    contract: Bar\n",
                "    nested:\n      repeated: 1\n      repeated: 2\n    contract: Bar\n",
            ),
        ],
    )
    def test_duplicate_yaml_mapping_keys_are_rejected_at_any_depth(self, text):
        with pytest.raises(ValueError, match="duplicate mapping key"):
            parse_domains(text)

    def test_custom_yaml_loader_does_not_change_global_safe_load(self):
        assert yaml.safe_load("value: 1\nvalue: 2\n") == {"value": 2}

    @pytest.mark.parametrize(
        ("old", "new", "message"),
        [
            (
                "    time_field: year\n",
                "    time_field: null\n",
                "series domains require a time_field",
            ),
            (
                "    natural_key: [series_id, year, period]\n",
                "    natural_key: [series_id, year, period, period]\n",
                "natural_key must not contain duplicates",
            ),
            (
                "    filter_dims: [year, period]\n",
                "    filter_dims: [year, period, period]\n",
                "filter_dims must not contain duplicates",
            ),
            (
                "    filter_dims: [year, period]\n",
                "    filter_dims: [year, absent_field]\n",
                "unknown contract field",
            ),
            (
                "    filter_dims: [symbol]\n",
                "    filter_dims: [symbol, timestamp]\n",
                "unknown contract field",
            ),
            (
                "    permissions: [query, store]\n",
                "    permissions: [query, query]\n",
                "permissions must not contain duplicates",
            ),
            (
                "    permissions: []\n",
                "    permissions: [store]\n",
                "transient storage_mode cannot declare store permission",
            ),
            (
                "    natural_key: [series_id, year, period]\n",
                "    natural_key: []\n",
                "natural_key must not be empty",
            ),
        ],
    )
    def test_invalid_semantic_relationships_fail_closed(self, old, new, message):
        with pytest.raises(ValueError, match=message):
            parse_domains(valid_v2_yaml_text().replace(old, new))

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("    time_field: year\n", "    time_field: 2025\n"),
            (
                "    temporal_kind: series\n",
                "    temporal_kind: unknown\n",
            ),
            (
                "    storage_mode: upsert\n",
                "    storage_mode: replace\n",
            ),
            (
                "    natural_key: [series_id, year, period]\n",
                "    natural_key: [series_id, true, period]\n",
            ),
            (
                "    permissions: [query, store]\n",
                "    permissions: [query, admin]\n",
            ),
        ],
    )
    def test_semantic_fields_reject_invalid_types_and_enums(self, old, new):
        with pytest.raises(ValueError):
            parse_domains(valid_v2_yaml_text().replace(old, new))

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

    def test_shared_bls_and_equity_contracts_are_registered(self):
        contracts = {
            "bls_catalog_item": "BlsCatalogItem",
            "bls_footnote": "BlsFootnote",
            "bls_observation": "BlsObservation",
            "equity_historical": "EquityHistorical",
            "equity_quote": "EquityQuote",
        }
        entries = "".join(
            f"  {domain}:\n"
            f"    display_name: {contract}\n"
            f"    rest_path: contracts/{domain}\n"
            f"    contract: {contract}\n"
            for domain, contract in contracts.items()
        )

        specs = parse_domains("domains:\n" + entries)

        assert set(specs) == set(contracts)
        for domain, contract in contracts.items():
            assert domains_module._CONTRACT_MODELS[contract] is getattr(models_module, contract)
            assert specs[domain].contract == contract

    @pytest.mark.parametrize(
        "contract",
        [
            "FredOutputType",
            "FredTransformUnits",
            "BlsCatalogPage",
            "ContractModel",
            "NotAContract",
        ],
    )
    def test_non_contract_exports_are_rejected(self, contract):
        text = valid_yaml_text().replace("contract: Bar", f"contract: {contract}")

        with pytest.raises(ValueError, match="unknown contract model"):
            parse_domains(text)

    def test_central_exports_are_source_contract_objects_without_duplicates(self):
        assert len(models_module.__all__) == len(set(models_module.__all__))
        for name in (
            "BlsCatalogItem",
            "BlsCatalogPage",
            "BlsFootnote",
            "BlsObservation",
        ):
            assert getattr(models_module, name) is getattr(bls_models, name)
        for name in ("EquityHistorical", "EquityQuote"):
            assert getattr(models_module, name) is getattr(fmp_models, name)

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
