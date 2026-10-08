# Warehouse query modes

The read and CSV export routes are:

```text
GET /api/v1/data/{asset_class}/{domain}
GET /api/v1/data/{asset_class}/{domain}/export
```

The canonical pair stays available; for example, A-share daily data remains at
`/api/v1/data/equity/stock_daily`. The exact two-segment `rest_path` declared
in `domains.yaml` is also accepted as an alias, so `stock/daily` resolves to
the same registered `equity` / `stock_daily` identity:

```text
GET /api/v1/data/stock/daily
GET /api/v1/data/stock/daily/export
GET /api/v1/data/domains/stock/daily/freshness
```

Alias requests use the canonical domain id for authorization scopes, table
selection, filters, freshness, and price adjustment. Responses, the catalog,
and export filenames also retain the canonical domain and registered asset
class. Only an exact registered alias with a registered capability is valid;
unknown or ambiguous pairs return 404. A valid alias denied by an API-key
scope returns 403 before warehouse reflection or reads.

Both routes default to `layer=dwd&source=auto`. DWD returns the merged contract
view. Its field names and symbols use contract spelling, and an explicit
`source=ths` or `source=akshare` filters the DWD `source` provenance column.
`source=auto` keeps the merged view.

Use `layer=ods` with an explicit registered source to read that source's raw
table. ODS field names, symbol values, and units stay in the source's native
form; they are not renamed, normalized, or unit-converted. For example:

```text
/api/v1/data/equity/stock_daily?layer=ods&source=ths&symbols=600519.SH&fields=close_price
/api/v1/data/equity/stock_daily?layer=ods&source=akshare&symbols=600519&fields=收盘,成交量
```

The inclusive `start` and `end` date bounds use the source date column declared
by that source's mapping. If no bounds are supplied, the API applies its
bounded default window. Symbol values are matched literally against the raw
source symbol column. Field filters are checked against actual table columns;
the reflected ODS primary key remains in the selected columns and determines
stable page and export ordering.

Direct ODS reads require a 1:1 mapping with a mapped date column and a matching
table schema. Wide or pivoted sources return HTTP 400 rather than attempting a
partial or misleading projection. Price adjustment (`qfq`/`hfq`) applies only
to DWD contract rows; ODS adjustment requests return HTTP 400. CSV export uses
the same validation, native fields, filters, and ordering as the JSON query and
reads in bounded batches.

Financial domains also accept `period=YYYY-MM-DD` to match the exact contract
`report_period`, and `report_date=YYYY-MM-DD` to match `announce_date`. These
filters apply to both JSON and CSV queries. Nonfinancial domains reject them
with HTTP 400. ODS accepts them only when that source declares a direct native
column mapping; pivoted financial sources return HTTP 400.

The export validates its window, selected fields, and filter columns before it
starts the CSV response. A database failure after streaming begins interrupts
the response, so an incomplete CSV body must not be treated as a completed
export.
