"""The slice records documented UI capabilities without inventing wire contracts.

Wave 38 moved ``CalendarEarnings`` out of the refused set: the route, its required filter and its
page cursor were answered by the source itself in an unauthenticated round, and the model is now
bound. The assertions below therefore read both sides -- what is refused, what is bound, and that
the two together still account for all three inventory models, so no row can vanish quietly.
"""

from opendata.data.providers.catalog import get_provider
from opendata.data.providers.seeking_alpha.specs import DECLARED_MODELS, NOT_DECLARED

INVENTORY_MODELS = frozenset({"CalendarEarnings", "ForwardEpsEstimates", "ForwardSalesEstimates"})


def test_models_without_official_machine_contract_are_not_bound() -> None:
    assert DECLARED_MODELS == ()
    assert set(NOT_DECLARED) == {
        "ForwardEpsEstimates",
        "ForwardSalesEstimates",
    }
    assert all("schema" in reason or "protocol" in reason for reason in NOT_DECLARED.values())


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
    assert bound == {"CalendarEarnings"}
    assert provider.is_implemented
    assert provider.credentials == (), "the calendar route was read without any credential"
