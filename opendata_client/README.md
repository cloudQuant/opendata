# opendata-client

`opendata-client` is a small synchronous Python client for the opendata REST
API and its optional WebSocket subscription endpoint. It is installed as an
independent consumer package and does not require the opendata server.

## Install

The base install supports REST queries and installs only the client's runtime
dependencies:

```bash
pip install ./opendata_client
```

Install the optional features only when needed:

```bash
pip install './opendata_client[frames]'  # pandas DataFrame conversion
pip install './opendata_client[ws]'      # synchronous WebSocket subscriptions
pip install './opendata_client[frames,ws]'
```

## REST queries

```python
from opendata_client import OpendataClient

with OpendataClient("https://api.example.com", api_key="od-...") as client:
    page = client.stock_daily(
        ["600519", "000001"],
        start="2024-01-01",
        end="2024-01-31",
        adjust="qfq",
    )
    rows = page.rows
```

`Page.columns` preserves the column order returned by the service. With the
`frames` extra, `page.to_dataframe()` keeps that order and preserves the
service's columns on empty pages.

The SDK accepts page numbers from 1 through 10,000 and page sizes from 1
through 1,000, matching the v1 data-query page-size ceiling. The client checks
that every response echoes the requested page and page size, reports
`count == len(rows)`, and contains only row objects. Malformed or inconsistent
responses raise `OpendataClientError`. Query responses are streamed and capped
at 16 MiB of decoded body data; oversized responses fail with
`OpendataClientError` before the rest of the body is read.

`iter_rows()` and `stock_daily_all()` follow pagination with bounded defaults:
at most 1,000 API pages and 200,000 yielded rows per call. Callers may set
`max_pages` and `max_rows` within hard ceilings of 10,000 pages and 1,000,000
rows. The page budget includes an empty final page when a full page needs a
follow-up to confirm completion. If either budget prevents the client from
confirming that all rows were read, it raises `OpendataClientError` rather
than ending normally and making a partial result look complete. The same
defaults apply when `subscribe(..., pull=True)` reads update rows over REST.

Without `as_of`, these v1 reads use the latest view of the requested data.
Pagination does not create a stable snapshot: data may be corrected or updated
between page requests. The API supports `as_of` for selected point-in-time
queries; the client passes that selector through and does not claim snapshot
guarantees beyond the server's domain-specific contract.

## Provider model metadata

The model directory lists registered identities visible to the credential's
domain scopes. The schema endpoint returns the model's complete validation
JSON Schema, including defaults, enums, references, and nullable fields:

```python
with OpendataClient("https://api.example.com", api_key="od-...") as client:
    models = client.provider_models(source="fred")
    metadata = client.provider_model_schema("fred", "FredSearch")
    query_schema = metadata["schema"]
```

These methods use the base REST install. Their endpoints are
`GET /api/v1/providers/models` and
`GET /api/v1/providers/{source}/models/{model}/schema`. They read metadata
without fetching upstream observations. `verified` reports the registered
capability's verification state; the directory does not certify every model
offered by that provider. Exact model identities reject `auto`.

## WebSocket subscriptions

With the `ws` extra, a subscription can notify the consumer and pull rows over
REST after each update:

```python
with OpendataClient("https://api.example.com", api_key="od-...") as client:
    with client.subscribe("stock_daily", pull=True) as updates:
        for update in updates:
            rows = update.data
```

`DataUpdate.to_dataframe()` is available with the `frames` extra. Its column
order follows each field's first occurrence across the update rows because
WebSocket update frames do not include REST column metadata. Fields introduced
in later rows are retained.
