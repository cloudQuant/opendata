"""Shared declaration-driven engine for provider models (iteration 2, WP2-A).

Provider packages declare models with :mod:`.spec` and build fetchers with
:mod:`.http_json`; neither module imports an upstream SDK or performs I/O at
import time, so a provider's cold import stays as light as its own package.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    build_query_model,
    build_row_model,
    fetch_pages,
    make_http_json_fetcher,
    normalize_record,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
)

__all__ = [
    "ColumnSpec",
    "ModelSpec",
    "PaginationSpec",
    "ParamSpec",
    "ProviderEngineError",
    "build_query_model",
    "build_row_model",
    "fetch_pages",
    "make_http_json_fetcher",
    "normalize_record",
]
