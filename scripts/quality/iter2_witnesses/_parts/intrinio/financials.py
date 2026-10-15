"""Wave 8 shard: offline witnesses for intrinio's eight unwitnessed financial rows.

These eight rows are the hand-written slice of the provider
(``opendata/data/providers/intrinio/models/iter2_financials.py``), not its declarative
``ModelSpec`` rows: every fetch leg goes through the module-level ``_request`` helper, which
calls the engine's ``_http_get_json``, which imports ``get_shared_http_client`` *inside the
function body* (``opendata/data/providers/_engine/http_json.py:825``). The client is therefore
resolved at request time from ``opendata.data.http_client``, so this shard witnesses Branch B:
one ``client_seam`` over that module and a ``{"responses": [...]}`` envelope, exactly the seam
``tests/test_intrinio_financials_iter2.py:105`` already patches. No ``http_transport`` is
declared here because these fetchers never read one -- they own their paging and their headers,
so a ``FixedResponseTransport`` would be ignored and the leg would hit the socket breaker.

The envelope ordering is the reason the seam is expressive enough for the two-stage shape:
``/companies/AAPL/fundamentals`` and ``/fundamentals/<id>/standardized_financials`` both contain
``/fundamentals``, so the detail entry is declared first and the seam's first-match rule routes
each URL to its own body. A single engine document could not do this: ``_FundamentalsPage`` and
``_StandardizedPage`` are both ``extra="forbid"``, so no one JSON body validates as both.

Precondition (``row_model``) -- five of these fetchers declared only ``output_model`` and the
estimate/PE fetchers declared the wire-parse class, so the shard's provider-local change is the
``row_model`` declaration itself. Each printed resolution below is the output of
``python -c "from opendata.data.domains import contract_model; print(contract_model(d).__module__
+ '.' + contract_model(d).__name__)"`` run on the gate interpreter, and every one lands in
``opendata/data/models/intrinio_financials.py`` -- the same class the fetcher validates its rows
with, so no look-alike is involved:

``opendata.data.models.intrinio_financials`` holds all eight classes; each domain printed
``<module>.<class>``, and the module part is identical for every line:

    intrinio_income_statement      -> ...intrinio_financials.IntrinioIncomeStatement
    intrinio_balance_sheet         -> ...intrinio_financials.IntrinioBalanceSheet
    intrinio_cash_flow_statement   -> ...intrinio_financials.IntrinioCashFlowStatement
    intrinio_financial_ratios      -> ...intrinio_financials.IntrinioFinancialRatios
    intrinio_key_metrics           -> ...intrinio_financials.IntrinioKeyMetrics
    intrinio_reported_financials   -> ...intrinio_financials.IntrinioReportedFinancials
    intrinio_forward_eps_estimates -> ...intrinio_financials.IntrinioForwardEpsEstimate
    intrinio_forward_pe_estimates  -> ...intrinio_financials.IntrinioForwardPeEstimate

Every refusal is the provider's own typed, code-bearing error
(``IntrinioFinancialError``, a ``ProviderEngineError`` subclass declared by this provider
package), and every ``tamper`` breaks one cell a contract in this slice declares: an
integer-only ``FinancialAmount`` wide column, a tag's unit in ``tag_units``, the fiscal period
the query promised, a numeric fact value, the tag/value pairing the mapper refuses to guess
between, the required ``axis``/``member`` pair of an XBRL dimension, the "one fiscal-period
field is required" alias rule, and the identifier the snapshot is addressed by. Following the
branch-B convention the base ``intrinio`` module already uses for its hand-written rows, no
``expected_missing``/``expected_rejected``/``expected_invalid`` face is declared: this slice's
refusals carry a code and a status face, and a per-column attribution would claim a detail the
shipped error does not publish.
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.http_client"],
    "attr": "get_shared_http_client",
}
_SOURCE_FILES: Final[tuple[str, ...]] = (
    "opendata/data/providers/intrinio/models/iter2_financials.py",
    "opendata/data/providers/_engine/http_json.py",
)
_CONTRACT_FILES: Final[tuple[str, ...]] = (
    "opendata/data/models/intrinio_financials.py",
    "opendata/data/domains.yaml",
)
_REFUSAL_CLASS: Final = "IntrinioFinancialError"


def _refusal(code: str) -> str:
    """The code ``iter2_financials._fail`` actually raises for one of its own labels."""
    return f"INTRINIO_FINANCIALS_{code}"


def _company(ticker: str = "AAPL") -> dict[str, Any]:
    """One company identity in the keys ``_CompanyRef`` allows (extra is forbidden)."""
    return {
        "id": f"com_{ticker.lower()}",
        "ticker": ticker,
        "name": "Apple Inc." if ticker == "AAPL" else "Microsoft Corporation",
        "cik": "0000320193" if ticker == "AAPL" else "0000789019",
        "lei": "X0ZZ0WYF7QIF98IC0L06",
    }


def _fundamental(statement_code: str, *, fiscal_period: str = "FY") -> dict[str, Any]:
    """The period header ``_Fundamental`` requires, with a UTC filing instant.

    ``filing_date`` carries an offset because ``_datetime_from_source`` refuses a naive or
    date-only stamp: the row's ``filed_at`` is only publishable from a moment the source said in
    its own zone, so a fixture that spelled it as a bare date would be refused for a reason that
    belongs to the fixture rather than to the contract.
    """
    return {
        "id": "fund_AAPL_2024_FY",
        "statement_code": statement_code,
        "fiscal_year": 2024,
        "fiscal_period": fiscal_period,
        "type": "calculated" if statement_code == "calculations" else "reported",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "filing_date": "2025-02-03T13:00:00Z",
        "is_latest": True,
        "company": _company(),
    }


def _fundamentals_page(statement_code: str, *, fiscal_period: str = "FY") -> dict[str, Any]:
    """Stage one: the one requested period, plus the terminal cursor the contract documents."""
    item = dict(_fundamental(statement_code, fiscal_period=fiscal_period))
    item.pop("company")
    return {
        "fundamentals": [item],
        "company": _company(),
        "next_page": None,
    }


def _fact(tag: str, value: object, unit: str, *, sequence: int = 1) -> dict[str, Any]:
    """One standardized observation in the keys ``_StandardizedFact``/``_DataTag`` allow."""
    return {
        "data_tag": {
            "id": f"tag_{tag}",
            "name": tag.replace("_", " ").title(),
            "tag": tag,
            "parent": "Revenues",
            "sequence": sequence,
            "factor": "+",
            "balance": "credit",
            "unit": unit,
        },
        "value": value,
    }


def _standardized_page(
    statement_code: str, facts: list[dict[str, Any]], *, fiscal_period: str = "FY"
) -> dict[str, Any]:
    """Stage two: the facts and the period header that must agree with stage one."""
    return {
        "standardized_financials": facts,
        "fundamental": _fundamental(statement_code, fiscal_period=fiscal_period),
        "company": _company(),
        "next_page": None,
    }


def _two_stage_envelope(list_page: dict[str, Any], detail_page: dict[str, Any]) -> dict[str, Any]:
    """Route the detail URL before the list URL: both contain ``/fundamentals``."""
    return {
        "responses": [
            {"url_contains": "standardized_financials", "body": detail_page},
            {"url_contains": "companies", "body": list_page},
        ]
    }


def _intrinio_balance_sheet_document() -> dict[str, Any]:
    """FY2024 balance sheet: two USD-tagged amounts under the two documented pointers."""
    return _two_stage_envelope(
        _fundamentals_page("balance_sheet_statement"),
        _standardized_page(
            "balance_sheet_statement",
            [
                _fact("cashandequivalents", 48_612_000_000, "USD"),
                _fact("totalassets", 244_649_000_000, "USD", sequence=2),
            ],
        ),
    )


def _intrinio_income_statement_document() -> dict[str, Any]:
    """FY2024 income statement: a USD amount row and a per-share row, the two units promised."""
    return _two_stage_envelope(
        _fundamentals_page("income_statement"),
        _standardized_page(
            "income_statement",
            [
                _fact("totalrevenue", 383_285_000_000, "USD"),
                _fact("basiceps", 6.08, "USD Per Share", sequence=2),
            ],
        ),
    )


def _intrinio_cash_flow_statement_document() -> dict[str, Any]:
    """FY2024 cash-flow statement: operating cash flow and consolidated net income."""
    return _two_stage_envelope(
        _fundamentals_page("cash_flow_statement"),
        _standardized_page(
            "cash_flow_statement",
            [
                _fact("netcashfromoperatingactivities", 116_049_000_000, "USD"),
                _fact("netincome", 96_996_000_000, "USD", sequence=2),
            ],
        ),
    )


def _intrinio_financial_ratios_document() -> dict[str, Any]:
    """FY2024 calculations: a unitless float ratio and a priced multiple ratio."""
    return _two_stage_envelope(
        _fundamentals_page("calculations"),
        _standardized_page(
            "calculations",
            [
                _fact("currentratio", 1.04, "Float"),
                _fact("pricetoearnings", 33.21, "Multiple", sequence=2),
            ],
        ),
    )


def _intrinio_key_metrics_document() -> dict[str, Any]:
    """FY2024 calculations for the metrics row.

    Market capitalization is published twice with one value: the mapper documents that a tag it
    already resolved may repeat, and only a *disagreement* between the two cells is the
    contradiction it refuses, so the clean body has to carry the agreement the guard tolerates.
    """
    return _two_stage_envelope(
        _fundamentals_page("calculations"),
        _standardized_page(
            "calculations",
            [
                _fact("marketcap", 3_100_000_000_000, "USD"),
                _fact("marketcap", 3_100_000_000_000, "USD", sequence=2),
                _fact("nwc", 125_000_000_000, "USD", sequence=3),
            ],
        ),
    )


def _reported_fact(tag: str, value: object, *, sequence: int = 1, depth: int = 0) -> dict[str, Any]:
    """One as-reported XBRL fact with the dimensional context the contract keeps."""
    return {
        "xbrl_tag": {
            "tag": f"us-gaap:{tag}",
            "name": tag,
            "balance": "debit",
            "unit": "USD",
            "abstract": False,
            "sequence": sequence,
            "depth": depth,
        },
        "value": value,
        "dimensions": [
            {
                "axis": "us-gaap:LongtermDebtTypeAxis",
                "member": "us-gaap:ConvertibleDebtMember",
            }
        ],
    }


def _intrinio_reported_financials_document() -> dict[str, Any]:
    """Stage one (``reported_only=true``) plus the as-reported XBRL facts of one period."""
    return {
        "responses": [
            {
                "url_contains": "reported_financials",
                "body": {
                    "reported_financials": [
                        _reported_fact("Cash", 28_400_000_000),
                        _reported_fact("AccountsReceivableNet", 33_860_000_000, sequence=2),
                    ],
                    "fundamental": _fundamental("income_statement"),
                    "company": _company(),
                    "next_page": None,
                },
            },
            {"url_contains": "companies", "body": _fundamentals_page("income_statement")},
        ]
    }


def _intrinio_forward_eps_estimates_document() -> dict[str, Any]:
    """One annual Zacks EPS consensus period under the endpoint's ``estimates`` pointer."""
    return {
        "responses": [
            {
                "url_contains": "zacks/eps_estimates",
                "body": {
                    "estimates": [
                        {
                            "company": _company(),
                            "date": "2025-12-31",
                            "fiscal_year": 2026,
                            "fiscal_period": "FY",
                            "calendar_year": 2026,
                            "calendar_period": "FY",
                            "count": 24,
                            "mean": 2.71,
                            "median": 2.7,
                            "high": 2.95,
                            "low": 2.44,
                            "standard_deviation": 0.11,
                            "percent_change": -0.4,
                            "mean_7_days_ago": 2.72,
                            "mean_30_days_ago": 2.69,
                            "mean_60_days_ago": 2.66,
                            "mean_90_days_ago": 2.61,
                        }
                    ],
                    "next_page": None,
                },
            }
        ]
    }


def _intrinio_forward_pe_estimates_document() -> dict[str, Any]:
    """The forward-PE snapshot the identifier is addressed by: one document, one row."""
    return {
        "responses": [
            {
                "url_contains": "zacks/forward_pe/AAPL",
                "body": {
                    "ticker": "AAPL",
                    "company_name": "Apple Inc.",
                    "updated_date": "2026-02-17",
                    "forward_pe_year1": 28.9,
                    "forward_pe_year2": 26.4,
                    "forward_pe_year3": 24.1,
                    "forward_pe_year4": 22.5,
                    "forward_pe_year5": 21.0,
                    "forward_peg_ratio_year1": 1.32,
                    "last_ttm_eps": 6.08,
                },
            }
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "BalanceSheet": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_balance_sheet_document,
        "query": {"symbol": "AAPL", "period": "annual", "limit": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "standardized_financials", 1, "data_tag", "unit"],
            "value": "Shares",
            "column": "totalassets",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("TAG_UNIT_MISMATCH"),
        "tamper_reason": (
            "IntrinioBalanceSheetFetcher.tag_units declares every mapped balance-sheet tag, "
            "'totalassets' among them, to be a USD amount, and IntrinioBalanceSheet types the "
            "wide column as an integer FinancialAmount, so a fact whose data_tag unit says Shares "
            "cannot be written into total_assets without silently restating a share count as "
            "money: the mapper refuses it as INTRINIO_FINANCIALS_TAG_UNIT_MISMATCH"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "IncomeStatement": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_income_statement_document,
        "query": {"symbol": "AAPL", "period": "annual", "limit": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "standardized_financials", 0, "value"],
            "value": 383_285_000_000.5,
            "column": "totalrevenue",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("CONTRACT_INVALID"),
        "tamper_reason": (
            "the tag 'totalrevenue' is mapped onto IntrinioIncomeStatement.revenue, which is a "
            "FinancialAmount whose guard accepts a JSON integer or null and refuses any other "
            "number, so publishing a half-dollar in that cell is a contract violation the row "
            "must refuse as INTRINIO_FINANCIALS_CONTRACT_INVALID rather than a revenue figure "
            "rounded to the nearest dollar"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "CashFlowStatement": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_cash_flow_statement_document,
        "query": {"symbol": "AAPL", "period": "annual", "limit": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", "fundamentals", 0, "fiscal_period"],
            "value": "Q1",
            "column": "fiscal_period",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("FREQUENCY_MISMATCH"),
        "tamper_reason": (
            "the query asked period 'annual', which _period_type sends as type=FY, and "
            "_matches_frequency only lets fiscal_period 'FY' through for it, so a fundamentals "
            "list item labelled 'Q1' is not the annual cash-flow period the row was asked for: "
            "INTRINIO_FINANCIALS_FREQUENCY_MISMATCH, because accepting it would publish a quarter "
            "of cash flows under an annual contract row"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "FinancialRatios": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_financial_ratios_document,
        "query": {"symbol": "AAPL", "limit": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "standardized_financials", 0, "value"],
            "value": True,
            "column": "currentratio",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("BAD_SHAPE"),
        "tamper_reason": (
            "a fact value is a FinancialValue, whose guard refuses a bool before pydantic can "
            "widen it, and IntrinioFinancialRatios.current_ratio is a FinancialFloat with the "
            "same refusal, so a flag in the current-ratio cell is the shape failure "
            "INTRINIO_FINANCIALS_BAD_SHAPE rather than a solvency ratio of 1.0"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "KeyMetrics": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_key_metrics_document,
        "query": {"symbol": "AAPL", "limit": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "standardized_financials", 1, "value"],
            "value": 3_100_000_000_500,
            "column": "marketcap",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("AMBIGUOUS_TAG_MAPPING"),
        "tamper_reason": (
            "both facts carry the tag 'marketcap', which IntrinioKeyMetricsFetcher maps onto the "
            "single column market_cap, and the mapper only tolerates a repeat that agrees with "
            "what it already wrote, so two different values for one mapped tag is the "
            "contradiction INTRINIO_FINANCIALS_AMBIGUOUS_TAG_MAPPING refuses instead of guessing "
            "which capitalization the key metric means"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "ReportedFinancials": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_reported_financials_document,
        "query": {"symbol": "AAPL", "statement_type": "income", "limit": 1},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "reported_financials", 0, "dimensions", 0, "member"],
            "column": "member",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("BAD_SHAPE"),
        "tamper_reason": (
            "an as-reported fact keeps its dimensional context as an axis/member pair, and both "
            "_ReportedDimension.member and IntrinioReportedDimension.member are required "
            "StrictStr, so a fact published with an axis and no member is a half-said dimension: "
            "INTRINIO_FINANCIALS_BAD_SHAPE, because a row that dropped the member would silently "
            "relabel which segment of the filing the XBRL value belongs to"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "ForwardEpsEstimates": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_forward_eps_estimates_document,
        "query": {"symbol": "AAPL", "period": "annual", "limit": 1},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "estimates", 0, "fiscal_period"],
            "column": "fiscal_period",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("BAD_SHAPE"),
        "tamper_reason": (
            "_EpsEstimateRow documents that a consensus period names its fiscal period either "
            "through 'fiscal_period' or through the deprecated alias 'fiscal_quarter', and its "
            "model validator refuses the row when neither is present, so a period that says "
            "nothing about which fiscal span it forecasts is INTRINIO_FINANCIALS_BAD_SHAPE "
            "instead of an estimate with a null fiscal period"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
    "ForwardPeEstimates": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_forward_pe_estimates_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "ticker"],
            "value": "MSFT",
            "column": "ticker",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _refusal("SYMBOL_MISMATCH"),
        "tamper_reason": (
            "the forward-PE snapshot is addressed by identifier in the URL "
            "(/zacks/forward_pe/AAPL) and its ticker cell is the row's symbol, so "
            "transform_data verifies the two agree: an 'MSFT' answer to an AAPL request is "
            "INTRINIO_FINANCIALS_SYMBOL_MISMATCH rather than a valuation multiple filed under "
            "the wrong company"
        ),
        "source_files": [*_SOURCE_FILES],
        "contract_files": list(_CONTRACT_FILES),
    },
}
