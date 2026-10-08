"""Contract and dependency-boundary tests for shared equity price models."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from opendata.data.models.equity_price import EquityHistorical, EquityQuote
from opendata.data.providers.fmp.models._contracts import (
    EquityHistorical as FmpEquityHistorical,
)
from opendata.data.providers.fmp.models._contracts import EquityQuote as FmpEquityQuote


def _schema_digest(model: type[EquityHistorical] | type[EquityQuote]) -> str:
    schema = json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(schema.encode()).hexdigest()


def test_fmp_contracts_reexport_the_shared_model_objects() -> None:
    assert FmpEquityHistorical is EquityHistorical
    assert FmpEquityQuote is EquityQuote


def test_shared_equity_price_schemas_match_the_pre_move_contracts() -> None:
    assert _schema_digest(EquityHistorical) == (
        "8eb0cc46c9829b09526432c01e44d61ef63b92ce42aae253b801120b01cf2a32"
    )
    assert _schema_digest(EquityQuote) == (
        "b5b19862d0222dc68632946a7551591834ab7826f7296763fd9165dec5fb2fb4"
    )


def test_shared_price_models_roundtrip_mixed_nullable_numeric_values() -> None:
    large_volume = 2**53 + 1
    historical_rows = [
        EquityHistorical.model_validate(
            {
                "symbol": "AAPL",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 103.0,
                "low": 99.5,
                "close": 102.0,
                "volume": large_volume,
                "query_window_scope": "explicit",
            }
        ),
        EquityHistorical.model_validate(
            {
                "symbol": "AAPL",
                "date": "2026-01-03",
                "open": 100.0,
                "high": 103.0,
                "low": 99.5,
                "close": 102.0,
                "volume": None,
                "query_window_scope": "explicit",
            }
        ),
        EquityHistorical.model_validate(
            {
                "symbol": "AAPL",
                "date": "2026-01-04",
                "open": 100.0,
                "high": 103.0,
                "low": 99.5,
                "close": 102.0,
                "volume": 1.5,
                "query_window_scope": "explicit",
            }
        ),
    ]
    quote_rows = [
        EquityQuote.model_validate({"symbol": "AAPL", "price": 102.0, "volume": large_volume}),
        EquityQuote.model_validate({"symbol": "MSFT", "price": 200.0, "volume": None}),
        EquityQuote.model_validate({"symbol": "GOOG", "price": 150.0, "volume": 1.5}),
    ]

    historical_frame = EquityHistorical.to_frame(historical_rows)
    quote_frame = EquityQuote.to_frame(quote_rows)

    for frame, field in ((historical_frame, "volume"), (quote_frame, "volume")):
        assert frame[field].dtype == object
        assert frame[field].iloc[0] == large_volume
        assert type(frame[field].iloc[0]) is int
        assert frame[field].iloc[1] is None
        assert frame[field].iloc[2] == 1.5
        assert type(frame[field].iloc[2]) is float
    assert EquityHistorical.from_frame(historical_frame) == historical_rows
    assert EquityQuote.from_frame(quote_frame) == quote_rows


def test_shared_price_module_limits_opendata_imports_to_base_contract() -> None:
    source_path = Path(__file__).resolve().parents[1] / "opendata/data/models/equity_price.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)

    allowed_opendata_import = "opendata.data.models.base"
    assert all(
        not (module == "opendata" or module.startswith("opendata."))
        or module == allowed_opendata_import
        for module in imported_modules
    )
