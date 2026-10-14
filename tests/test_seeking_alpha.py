"""The slice records documented UI capabilities without inventing wire contracts."""

from opendata.data.providers.seeking_alpha.specs import DECLARED_MODELS, NOT_DECLARED


def test_models_without_official_machine_contract_are_not_bound() -> None:
    assert DECLARED_MODELS == ()
    assert set(NOT_DECLARED) == {
        "CalendarEarnings",
        "ForwardEpsEstimates",
        "ForwardSalesEstimates",
    }
    assert all("schema" in reason or "protocol" in reason for reason in NOT_DECLARED.values())
