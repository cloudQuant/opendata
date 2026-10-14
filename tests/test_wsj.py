"""The WSJ slice preserves gaps while public response access is unverified."""

from opendata.data.providers.wsj.specs import DECLARED_MODELS, NOT_DECLARED


def test_etf_lists_without_official_response_contract_are_not_bound() -> None:
    assert DECLARED_MODELS == ()
    assert set(NOT_DECLARED) == {"ETFGainers", "ETFLosers", "ETFActive"}
    assert all("401" in reason and "schema" in reason for reason in NOT_DECLARED.values())
