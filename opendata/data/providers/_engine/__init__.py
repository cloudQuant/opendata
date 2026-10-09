"""Shared declaration-driven engine for provider models (iteration 2, WP2-A).

Provider packages declare models with :mod:`.spec` and build fetchers with
:mod:`.http_json`; :mod:`.decoders` is the body-shape face the declaration selects --
a JSON document, delimited text, or one member of a zip archive. A declaration may also
select rows out of what the source published, with the :class:`~.spec.RowFilterSpec`
predicates :func:`.http_json.apply_row_filters` evaluates. No module here
imports an upstream SDK or performs I/O at import time, so a provider's cold import
stays as light as its own package.
"""

from __future__ import annotations

from opendata.data.providers._engine.decoders import ResponseDecodeError, decode_body
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    apply_row_filters,
    build_query_model,
    build_row_model,
    fetch_pages,
    make_http_json_fetcher,
    normalize_record,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
    RowFilterSpec,
)

__all__ = [
    "ColumnSpec",
    "DecoderSpec",
    "ModelSpec",
    "PaginationSpec",
    "ParamSpec",
    "ProviderEngineError",
    "RowFilterSpec",
    "ResponseDecodeError",
    "apply_row_filters",
    "build_query_model",
    "build_row_model",
    "decode_body",
    "fetch_pages",
    "make_http_json_fetcher",
    "normalize_record",
]
