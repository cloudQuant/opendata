"""Pipeline task capacity defaults and input bounds."""

import pytest
from pydantic import ValidationError

from opendata.api.schemas import PipelineTaskParameters, validate_pipeline_task_parameters


def test_omitted_limit_uses_job_capacity_and_stays_omitted_from_payload() -> None:
    """An omitted cap validates as 10,000 but leaves the persisted job default unset."""
    parameters = PipelineTaskParameters(domain="stock_daily", source="ths")

    assert parameters.limit == 10_000
    assert parameters.symbols is None
    assert validate_pipeline_task_parameters({"domain": "stock_daily", "source": "ths"}) == {
        "domain": "stock_daily",
        "source": "ths",
    }


def test_explicit_5000_limit_is_preserved() -> None:
    """An explicit 5,000-symbol request remains an explicit override."""
    normalized = validate_pipeline_task_parameters(
        {"domain": "stock_daily", "source": "ths", "limit": 5_000}
    )

    assert normalized["limit"] == 5_000


def test_limit_accepts_50000_and_rejects_values_above_capacity() -> None:
    """The scheduled cap accepts its configured ceiling and rejects larger values."""
    maximum = PipelineTaskParameters(domain="stock_daily", source="ths", limit=50_000)
    assert maximum.limit == 50_000

    with pytest.raises(ValidationError):
        PipelineTaskParameters(domain="stock_daily", source="ths", limit=50_001)


def test_explicit_symbol_list_keeps_5000_item_limit() -> None:
    """Explicit symbol lists stay capped at 5,000 while omission remains available."""
    symbols = [f"TEST{index:04d}" for index in range(5_000)]
    parameters = PipelineTaskParameters(domain="stock_daily", source="ths", symbols=symbols)
    assert len(parameters.symbols or []) == 5_000

    with pytest.raises(ValidationError):
        PipelineTaskParameters(
            domain="stock_daily",
            source="ths",
            symbols=[*symbols, "TEST5000"],
        )
