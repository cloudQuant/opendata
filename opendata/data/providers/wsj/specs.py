"""Declared public documentation and unresolved ETF-mover response contracts."""

from __future__ import annotations

from typing import Final

DECLARED_MODELS: Final[tuple[str, ...]] = ()
NOT_DECLARED: Final[dict[str, str]] = dict.fromkeys(
    ("ETFGainers", "ETFLosers", "ETFActive"),
    (
        "The official WSJ Market Data pages were reachable as public-page references but the "
        "unauthenticated HTML request returned HTTP 401; no official response schema or public "
        "programmatic endpoint was documented."
    ),
)
