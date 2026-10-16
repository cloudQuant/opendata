"""Wave 16 shard: FMP's stable EOD history row.

``market.py``-style refusal faces aside, this is the one FMP row that ``rows_e.py`` recorded as a
product gap on the strength of a declaration that no longer holds: it asserted
``EquityHistoricalFetcher`` "declares neither name, so it publishes no frozen contract row at all".
The fetcher has always *built* ``opendata.data.models.equity_price.EquityHistorical`` rows --
``transform_data`` validates every source bar into that class and returns them sorted and
deduplicated (``equity_historical.py:145-177``) -- and it re-exports it through
``models/_contracts.py``; what it never published was the ``row_model`` name the witness instrument
reads. Wave 16 adds that one line, and this shard witnesses the row it makes acceptable.
`rows_e.py`'s sentence is therefore superseded, not corrected in place: editing any file of a
provider's shard set changes the digest every claim of that provider pins
(``iter2_claims_producer.py:203-207``), so a docstring fix there would stale every accepted FMP row.

The seam is the governed client, not the engine transport: ``EquityHistoricalFetcher.extract_data``
calls ``fetch_array("historical-price-eod/full", request_params, ctx.timeout)`` with three arguments
(``equity_historical.py:94-98``), and ``fetch_array`` only reaches a transport when one is passed --
otherwise ``_get_response`` issues ``get_shared_http_client().get(...)``
(``models/_client.py:243-261``). So the fixture is a ``{"responses": [...]}`` envelope over
``opendata.data.providers.fmp.models._client``, and one explicit window is one request, well inside
the three-send grant.

The counterfact breaks a required OHLC cell rather than a window rule, and the face is code-only
because that is what ships: ``transform_data`` catches ``(ValidationError, TypeError, ValueError)``
and raises a bare ``FMPProviderError("FMP_BAD_SHAPE")`` (``equity_historical.py:154-159``) without
running it through ``contract_shape_error``, so no column is attributed and none is declared.
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE: Final = "opendata/data/providers/fmp/models/_client.py"
_MODEL_SOURCE: Final = "opendata/data/providers/fmp/models/equity_historical.py"
_CONTRACT: Final = "opendata/data/models/equity_price.py"
_DOMAINS: Final = "opendata/data/domains.yaml"

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.fmp.models._client"],
    "attr": "get_shared_http_client",
}
_WINDOW: Final[dict[str, str]] = {"start_date": "2026-01-05", "end_date": "2026-01-09"}


def _eod_row(day: str, close: float) -> dict[str, Any]:
    """One ``historical-price-eod/full`` bar in the keys the stable route publishes."""
    open_ = round(close * 0.9951, 4)
    return {
        "symbol": "AAPL",
        "date": day,
        "open": open_,
        "high": round(close * 1.0069, 4),
        "low": round(open_ * 0.9893, 4),
        "close": close,
        "volume": 43_182_600,
        "vwap": round((open_ + close) / 2, 4),
        "change": round(close - open_, 4),
        "changePercent": round((close / open_ - 1) * 100, 4),
    }


def _equity_historical_document() -> dict[str, Any]:
    """Two consecutive settled bars inside the requested window, served as one array."""
    return {
        "responses": [
            {
                "url_contains": "/stable/historical-price-eod/full",
                "status": 200,
                "body": [_eod_row("2026-01-06", 253.92), _eod_row("2026-01-07", 258.86)],
            }
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityHistorical": {
        "client_seam": _SEAM,
        "fixture_document": _equity_historical_document,
        "query": {"symbol": "AAPL", **_WINDOW},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", 0, "open"],
            "column": "open",
        },
        "refusal_class": "FMPProviderError",
        "refusal_code": "FMP_BAD_SHAPE",
        "tamper_reason": (
            "EquityHistorical declares open a required float -- an EOD bar with no session open is "
            "not a bar -- so model_validate raises a missing-field error that transform_data funnels "
            "into the bare FMP_BAD_SHAPE without echoing the row; a bar whose open was dropped would "
            "otherwise be published as a complete trading day and priced off its close alone"
        ),
        "source_files": [_MODEL_SOURCE, _FMP_SOURCE],
        "contract_files": [_CONTRACT, _DOMAINS],
    },
}
