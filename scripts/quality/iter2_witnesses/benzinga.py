"""Offline witness configuration for the ``benzinga`` provider.

Witnesses run exclusively through the offline seam: the engine's
``fetch_pages(..., transport=...)`` and the fetchers' ``http_transport`` attribute,
served by ``FixedResponseTransport`` from ``opendata.data.providers._engine.testing``.
No leg touches the network and no ``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.
"""

from __future__ import annotations

from typing import Any, Final

_EPOCH_SAMPLE: Final[int] = 1_767_312_000  # 2026-01-02T00:00:00Z, synthetic sample.


def _benzinga_price_target_document() -> dict[str, Any]:
    """One Ratings-calendar record in the keys ``models/price_target.py`` reads."""
    return {
        "ratings": [
            {
                "ticker": "AAPL",
                "date": "2026-01-02",
                "time": "09:30:00",
                "exchange": "NASDAQ",
                "analyst": "Fixture Research",
                "analyst_name": "Fixture Analyst",
                "analyst_id": "FA-1",
                "name": "Fixture Apple Corp",
                "currency": "USD",
                "action_company": "Upgrades",
                "action_pt": "Raises PT",
                "rating_current": "Buy",
                "rating_prior": "Hold",
                "importance": 3,
                "pt_current": 210.5,
                "pt_prior": "200.25",
                "adjusted_pt_current": 211.25,
                "adjusted_pt_prior": None,
                "notes": "",
                "url_news": "https://www.benzinga.com/fixtures/news",
                "url_calendar": "https://www.benzinga.com/fixtures/calendar",
                "id": "fixture-pt-1",
                "updated": _EPOCH_SAMPLE,
            }
        ]
    }


def _benzinga_analyst_accuracy() -> dict[str, Any]:
    """The nested accuracy block the analyst-search row mapper reads."""
    return {
        "one_month_average_return": 12.5,
        "one_month_smart_score": 7.5,
        "one_month_stdev": 3.25,
        "one_month_success_rate": 80.0,
        "one_month_loss_count": 2,
        "overall_average_return": 25.0,
        "overall_success_rate": 62.5,
        "smart_score": 91.5,
        "total_ratings": 8,
        "total_ratings_percentile": 50.0,
    }


def _benzinga_analyst_search_document() -> dict[str, Any]:
    """One analyst record under the array the search endpoint publishes."""
    return {
        "analyst_ratings_analyst": [
            {
                "firm_id": "FF-1",
                "firm_name": "Fixture Research",
                "id": "FA-1",
                "name_first": "Fixture",
                "name_full": "Fixture Analyst",
                "name_last": "Analyst",
                "ratings_accuracy": _benzinga_analyst_accuracy(),
                "updated": _EPOCH_SAMPLE,
            }
        ]
    }


def _benzinga_news_record(item_id: int) -> dict[str, Any]:
    """One news item in the keys the company/world-news row mappers read."""
    return {
        "id": item_id,
        "author": "Fixture Desk",
        "created": "Mon, 02 Jan 2026 09:30:00 -0500",
        "updated": "Mon, 02 Jan 2026 09:31:00 -0500",
        "title": "A fixture market headline",
        "teaser": "Fixture teaser",
        "body": "Fixture body text.",
        "url": "https://www.benzinga.com/fixtures/story",
        "channels": [{"name": "General Market"}, {"name": "Options"}],
        "stocks": [{"name": "AAPL", "exchange": "NASDAQ"}],
        "tags": [{"name": "fixture-tag"}],
    }


def _benzinga_company_news_document() -> list[dict[str, Any]]:
    """A one-item company-news answer."""
    return [_benzinga_news_record(36_444_586)]


def _benzinga_world_news_document() -> list[dict[str, Any]]:
    """A two-item world-news answer."""
    return [_benzinga_news_record(36_444_587), _benzinga_news_record(36_444_588)]


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.benzinga.registration",
    "credential_env": {"BENZINGA_API_KEY": "fixture-token-never-sent"},
    "allowed_hosts": ["api.benzinga.com"],
    "models": {
        "PriceTarget": {
            "fixture_document": _benzinga_price_target_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "set", "path": ["ratings", 0, "updated"], "value": "not-an-epoch"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "BENZINGA_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a non-integer 'updated' cell makes the documented epoch-seconds "
                "converter refuse the record; the adapter must surface BENZINGA_SHAPE_INVALID "
                "as a ProviderEngineError carrying code/status/rejected/missing/invalid"
            ),
            "source_files": [
                "opendata/data/providers/benzinga/models/price_target.py",
                "opendata/data/providers/benzinga/specs.py",
            ],
            "contract_files": [
                "opendata/data/models/benzinga.py",
                "opendata/data/domains.yaml",
            ],
        },
        "AnalystSearch": {
            "fixture_document": _benzinga_analyst_search_document,
            "query": {},
            "tamper": {"op": "drop", "path": ["analyst_ratings_analyst", 0, "ratings_accuracy"]},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "BENZINGA_SHAPE_INVALID",
            "tamper_reason": (
                "removing 'ratings_accuracy' from the analyst record makes the nested "
                "accuracy mapper raise, and the adapter must surface BENZINGA_SHAPE_INVALID "
                "as a ProviderEngineError with the typed refusal shape"
            ),
            "source_files": [
                "opendata/data/providers/benzinga/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/benzinga.py",
                "opendata/data/domains.yaml",
            ],
        },
        "CompanyNews": {
            "fixture_document": _benzinga_company_news_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "drop", "path": [0, "id"]},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "BENZINGA_SHAPE_INVALID",
            "tamper_reason": (
                "dropping the required integer 'id' makes the strict row contract refuse "
                "the assembled row; the adapter must surface BENZINGA_SHAPE_INVALID as a "
                "ProviderEngineError with the typed refusal shape"
            ),
            "source_files": [
                "opendata/data/providers/benzinga/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/benzinga.py",
                "opendata/data/domains.yaml",
            ],
        },
        "WorldNews": {
            "fixture_document": _benzinga_world_news_document,
            "query": {},
            "tamper": {"op": "drop", "path": [0, "stocks"]},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "BENZINGA_SHAPE_INVALID",
            "tamper_reason": (
                "dropping the 'stocks' array makes the tag-array mapper refuse the record "
                "before any row is built; the adapter must surface BENZINGA_SHAPE_INVALID as "
                "a ProviderEngineError with the typed refusal shape"
            ),
            "source_files": [
                "opendata/data/providers/benzinga/models/iter2.py",
            ],
            "contract_files": [
                "opendata/data/models/benzinga.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
