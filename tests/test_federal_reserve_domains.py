"""The two federal_reserve engine domains, registered the way the cboe models are.

A declaration whose domain is unregistered names rows no query or ingest service can accept:
``contract_model(domain)`` only resolves for a domain listed in ``opendata/data/domains.yaml``,
and ``build_row_model`` publishes that contract class whenever the domain's semantics are
declared (the cboe precedent, ``cboe_*`` in that file). These tests show each new domain
(a) resolves via ``load_domains()`` with its declared semantics, (b) yields a ``contract_model()``
whose fields are exactly the declaration's column names, in order, and (c) is the class the
serving path accepts -- an offline fetch over the declared CSV decoder returns rows of exactly
that class.

Every face here carries a counterfact: mutating the contract (drop a field, rename one) must
make the in-repo publisher refuse and the auditor print ``CONTRACT_COLUMNS_DISAGREE``, so a
green membership check cannot hide a drifted contract.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError, create_model

from opendata.data.domains import (
    _CONTRACT_MODELS,
    contract_model,
    load_domains,
    require_domain_semantics,
)
from opendata.data.models.base import ContractModel
from opendata.data.providers._engine.http_json import build_row_model, make_http_json_fetcher
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    fixture_context,
    synthetic_raw_page,
    valid_query_kwargs,
)
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.federal_reserve import specs
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.registry import ProviderRegistry

if TYPE_CHECKING:
    from opendata.data.domains import DomainSpec
    from opendata.data.providers._engine.spec import ModelSpec

#: Each domain paired with the declaration that names it and the semantics it must carry.
#: The display fields below were derived from the declaration's own evidence: ``time_field``
#: and ``natural_key`` from the single required non-null column (``month`` / ``date``),
#: ``filter_dims`` from the same key because this wide table publishes no dimension columns
#: (the cboe precedent lists the natural key and nothing else; the maturity columns are values),
#: and ``temporal_kind: series`` because every row is one observation of a fixed period.
DOMAIN_CASES = [
    (
        "federal_reserve_money_measures",
        specs.MONEY_MEASURES,
        {
            "contract": "FederalReserveMoneyMeasure",
            "temporal_kind": "series",
            "time_field": "month",
            "natural_key": ("month",),
            "filter_dims": ("month",),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
    ),
    (
        "federal_reserve_treasury_rates",
        specs.TREASURY_RATES,
        {
            "contract": "FederalReserveTreasuryRate",
            "temporal_kind": "series",
            "time_field": "date",
            "natural_key": ("date",),
            "filter_dims": ("date",),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
    ),
]

DOMAINS = pytest.mark.parametrize(
    ("domain", "spec", "semantics"),
    DOMAIN_CASES,
    ids=[case[0] for case in DOMAIN_CASES],
)


def _field_pair(field: Any) -> tuple[Any, Any]:
    """Rebuild one pydantic field as ``(annotation, default)`` for ``create_model``."""
    if field.is_required():
        return (field.annotation, ...)
    return (field.annotation, field.default)


def _mutated_contract(
    contract_name: str,
    *,
    drop: str | None = None,
    rename: tuple[str, str] | None = None,
) -> type[ContractModel]:
    """Clone the shipped contract with one field dropped or renamed -- the drift counterfact."""
    fields = dict(_CONTRACT_MODELS[contract_name].model_fields)
    if drop is not None:
        fields.pop(drop)
    if rename is not None:
        old, new = rename
        fields[new] = fields.pop(old)
    return create_model(  # type: ignore[call-overload]
        f"Mutated{contract_name}",
        __base__=ContractModel,
        **{name: _field_pair(field) for name, field in fields.items()},
    )


def _auditor():
    """Load the audit module by path; the tests read its rules, never edit them."""
    path = Path(__file__).resolve().parents[1] / "scripts/quality/declaration_provenance.py"
    module_spec = importlib.util.spec_from_file_location("declaration_provenance", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    # ``dataclasses`` resolves annotations through ``sys.modules[cls.__module__]``; the module
    # must be registered before its body executes or its ``Finding`` dataclass refuses to build.
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


class TestTheDomainsAreRegistered:
    """``load_domains()`` resolves each fed domain with reviewed, cboe-shaped semantics."""

    @DOMAINS
    def test_entry_carries_the_declared_semantics(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        resolved: DomainSpec = require_domain_semantics(domain)
        assert resolved.semantics_declared is True
        for field_name, expected in semantics.items():
            assert getattr(resolved, field_name) == expected, field_name
        assert resolved.display_name
        assert spec.domain == domain

    @DOMAINS
    def test_transient_storage_declares_no_store_permission(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        """The cboe precedent: transient means no warehouse DDL, so ``store`` is refused."""
        resolved = require_domain_semantics(domain)
        assert resolved.storage_mode == "transient"
        assert "store" not in resolved.permissions
        assert "query" in resolved.permissions

    def test_the_retracted_yield_curve_domain_was_not_registered(self) -> None:
        """Retracting a declaration must not leave a domain behind (the oecd precedent)."""
        assert "federal_reserve_yield_curve" not in load_domains()
        with pytest.raises(LookupError):
            require_domain_semantics("federal_reserve_yield_curve")


class TestContractNamesExactlyTheDeclaredColumns:
    """``contract_model(domain).model_fields`` equals the declaration, name for name."""

    @DOMAINS
    def test_fields_match_in_declaration_order(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        contract = contract_model(domain)
        declared = [column.name for column in spec.columns]
        assert list(contract.model_fields) == declared

    @DOMAINS
    def test_required_and_nullable_fields_follow_the_declaration(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        """The time column is required and non-null; every measure accepts the published null.

        TreasuryRates' maturities stay ``str`` because the pinned body prints its own ``ND``
        token -- a numeric field would refuse real rows -- and MoneyMeasures reads its blank
        sentinel as ``None``, which a bare ``float`` field would reject.
        """
        contract = contract_model(domain)
        for column in spec.columns:
            field = contract.model_fields[column.name]
            assert field.is_required() == column.required, column.name
            accepts_null = type(None) in getattr(field.annotation, "__args__", ())
            if column.required:
                assert accepts_null == bool(column.nullable), column.name
            else:
                assert accepts_null, column.name


class TestServingPathAcceptsTheContract:
    """The row class the query/ingest services accept is exactly this contract."""

    @DOMAINS
    def test_row_model_is_the_contract_itself(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        """``build_row_model`` publishes the reviewed contract, not a synthesized look-alike."""
        assert build_row_model(spec) is contract_model(domain)

    @DOMAINS
    def test_registry_publishes_the_domain_capability(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider(SOURCE, registry)
        by_domain = {capability.domain: capability for capability in capabilities}
        assert domain in by_domain
        assert by_domain[domain].source == SOURCE
        assert by_domain[domain].verified is False
        fetcher = registry.resolve_model(SOURCE, spec.model)
        assert fetcher.row_model is contract_model(domain)

    @DOMAINS
    def test_offline_fetch_returns_rows_of_the_contract_class(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        """Two synthetic H.6/H.15 rows decode past the declared preamble and ARE the contract."""
        fetcher_type = make_http_json_fetcher(SOURCE, spec)
        transport = FixedResponseTransport(synthetic_raw_page(spec, 2))
        fetcher_type.http_transport = transport  # type: ignore[attr-defined]
        fetcher = fetcher_type()
        kwargs = valid_query_kwargs(spec)
        rows = list(
            fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=3), **kwargs)  # type: ignore[attr-defined]
        )
        assert len(rows) == 2
        for row in rows:
            assert type(row) is contract_model(domain)
            assert set(row.model_dump()) == {column.name for column in spec.columns}


class TestDriftIsRefused:
    """Counterfacts: each face above turns red the moment the contract drifts."""

    @DOMAINS
    def test_dropping_a_contract_field_makes_the_publisher_refuse(
        self,
        domain: str,
        spec: ModelSpec,
        semantics: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without this arm, ``build_row_model`` publishing a look-alike would pass unseen."""
        dropped = spec.columns[-1].name
        mutated = _mutated_contract(semantics["contract"], drop=dropped)
        monkeypatch.setitem(_CONTRACT_MODELS, semantics["contract"], mutated)
        with pytest.raises(ValueError) as raised:
            build_row_model(spec)
        assert dropped in str(raised.value)

    @DOMAINS
    def test_renaming_the_time_column_fails_domain_validation(
        self,
        domain: str,
        spec: ModelSpec,
        semantics: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Registration and contract must move together.

        The registry re-validates every entry against ``_CONTRACT_MODELS`` and refuses a domain
        whose ``time_field``/``natural_key`` names a field the contract no longer has. That is
        ``load_domains``' own guard; the probe calls ``parse_domains`` on the shipped text
        because ``load_domains`` answers from an ``lru_cache`` once the file has been read.
        """
        time_field = semantics["time_field"]
        mutated = _mutated_contract(
            semantics["contract"], rename=(time_field, f"{time_field}_typo")
        )
        monkeypatch.setitem(_CONTRACT_MODELS, semantics["contract"], mutated)
        import opendata.data.domains as domains_module

        registry_text = (
            Path(__file__).resolve().parents[1] / "opendata/data/domains.yaml"
        ).read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="unknown contract field"):
            domains_module.parse_domains(registry_text)

    @DOMAINS
    def test_auditor_prints_contract_columns_disagree_on_drift(
        self,
        domain: str,
        spec: ModelSpec,
        semantics: dict[str, Any],
    ) -> None:
        """The audit face is live: clean now, and one dropped field flips it to a violation."""
        auditor = _auditor()
        assert auditor.domain_disagree(spec, auditor.registered_domains()) == []
        dropped = spec.columns[-1].name
        population = auditor.registered_domains()
        reviewed, fields = population[domain]
        assert reviewed and dropped in fields
        population[domain] = (True, fields - {dropped})
        codes = {finding.code for finding in auditor.domain_disagree(spec, population)}
        assert codes == {"CONTRACT_COLUMNS_DISAGREE"}

    @DOMAINS
    def test_an_unregistered_domain_is_the_other_violation_face(
        self, domain: str, spec: ModelSpec, semantics: dict[str, Any]
    ) -> None:
        """Removing the registration reopens exactly the violation this change closed."""
        auditor = _auditor()
        population = auditor.registered_domains()
        assert domain in population
        del population[domain]
        codes = {finding.code for finding in auditor.domain_disagree(spec, population)}
        assert codes == {"DOMAIN_UNREGISTERED"}


class TestPublishedTokenTypes:
    """The two type decisions a reviewer will be asked to justify, pinned as behaviour."""

    def test_money_month_is_a_source_string_and_required(self) -> None:
        contract = contract_model("federal_reserve_money_measures")
        row = contract(month="1959-01", m1=1.0)
        assert row.month == "1959-01"
        assert row.m2 is None
        with pytest.raises(ValidationError):
            contract(m1=1.0)

    def test_treasury_maturity_keeps_the_published_nd_token(self) -> None:
        contract = contract_model("federal_reserve_treasury_rates")
        row = contract(date=date(1962, 1, 2), month_1="ND", year_10="4.62")
        assert row.month_1 == "ND"
        assert row.year_10 == "4.62"
        assert row.year_2 is None
        # A numeric maturity column would have refused the very row the body publishes.
        args = set(contract.model_fields["month_1"].annotation.__args__)
        assert args == {str, type(None)}
        assert contract.model_fields["date"].annotation is date
