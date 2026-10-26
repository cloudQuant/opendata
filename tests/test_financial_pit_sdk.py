"""SDK wire contract for source-scoped financial PIT queries."""

from datetime import datetime, timezone

import httpx

from opendata_client import OpendataClient


def test_query_passes_as_of_and_market_and_exposes_pit_metadata():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "rows": [{"symbol": "600000", "revision": 1}],
                    "columns": ["symbol", "revision"],
                    "page": 1,
                    "page_size": 500,
                    "count": 1,
                    "pit": {"as_of": "2024-04-02T00:00:00+00:00", "source": "ths"},
                },
            },
        )

    as_of = datetime(2024, 4, 2, tzinfo=timezone.utc)
    with OpendataClient(
        "http://api.test",
        api_key="od-test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        page = client.query(
            "equity",
            "financial_statement",
            symbols="600000",
            source="ths",
            market="CN",
            as_of=as_of,
        )

    assert seen["path"] == "/api/v1/data/equity/financial_statement"
    assert seen["params"]["as_of"] == as_of.isoformat()
    assert seen["params"]["market"] == "CN"
    assert page.rows == [{"symbol": "600000", "revision": 1}]
    assert page.pit == {"as_of": "2024-04-02T00:00:00+00:00", "source": "ths"}
