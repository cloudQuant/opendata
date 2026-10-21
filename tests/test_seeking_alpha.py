"""The slice records documented UI capabilities without inventing wire contracts.

Wave 38 moved ``CalendarEarnings`` out of the refused set: the route, its required filter and its
page cursor were answered by the source itself in an unauthenticated round, and the model is now
bound. The assertions below therefore read both sides -- what is refused, what is bound, and that
the two together still account for all three inventory models, so no row can vanish quietly.
"""

from opendata.data.providers.catalog import get_provider
from opendata.data.providers.seeking_alpha.specs import DECLARED_MODELS, NOT_DECLARED

INVENTORY_MODELS = frozenset({"CalendarEarnings", "ForwardEpsEstimates", "ForwardSalesEstimates"})


def test_every_inventory_model_is_now_bound_by_a_descriptor() -> None:
    """The refusal ledger is empty: both estimates rows landed in wave 55."""
    assert DECLARED_MODELS == ()
    assert NOT_DECLARED == {}


def test_the_bound_and_refused_sets_partition_the_inventory_models() -> None:
    """Every inventory row is either bound by a descriptor or named in the refusal ledger."""
    provider = get_provider("seeking_alpha")
    bound = {
        model_id
        for binding in provider.fetcher_bindings
        for model_id in binding.canonical_model_ids
    }
    assert bound & set(NOT_DECLARED) == set(), "bound *and* refused"
    assert bound | set(NOT_DECLARED) == INVENTORY_MODELS, "a silently dropped identity"
    assert bound == INVENTORY_MODELS
    assert provider.is_implemented
    assert provider.credentials == (), "all three routes were read without any credential"
