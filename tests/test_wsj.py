"""WSJ ETF movers are bound from the live public JSON response (REFERENCE-EXCEPTION-01)."""

from opendata.data.providers.wsj.provider import PROVIDER
from opendata.data.providers.wsj.specs import DECLARED_MODELS, NOT_DECLARED


def test_etf_lists_are_bound_from_the_live_public_response() -> None:
    """The three public mover tables answered 200 JSON, so they are declared and bound."""
    assert DECLARED_MODELS == ("ETFGainers", "ETFLosers", "ETFActive")
    assert NOT_DECLARED == {}
    assert {binding.canonical_model_ids for binding in PROVIDER.fetcher_bindings} == {
        ("ETFGainers",),
        ("ETFLosers",),
        ("ETFActive",),
    }
