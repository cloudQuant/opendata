"""Strict local checks for the Fuyao child transport attempt binding."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from opendata.pipeline import fuyao_financial_process as process_module


def _build_fetcher_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace only fetcher construction and return captured trusted arguments."""
    captured: list[dict[str, Any]] = []

    class RecordingFetcher:
        """Record builder arguments without constructing a transport."""

        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(process_module, "ThsFinancialObservationFetcher", RecordingFetcher)
    return captured


def _build_fetcher_kwargs() -> dict[str, Any]:
    """Return synthetic dependencies accepted by the private fixed builder."""
    return {
        "credentials": object(),
        "source_attempt_gate": object(),
        "bundle": SimpleNamespace(
            instrument_registry=object(),
            unit_profiles={"income": object()},
        ),
    }


def test_child_fetcher_builder_uses_exactly_one_configured_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed cap of one is passed unchanged into the fetcher constructor."""
    captured = _build_fetcher_arguments(monkeypatch)
    dependencies = _build_fetcher_kwargs()

    process_module._build_fetcher(**dependencies, max_attempts=1)

    assert len(captured) == 1
    assert captured[0]["max_attempts"] == 1
    assert captured[0]["credentials"] is dependencies["credentials"]
    assert captured[0]["source_attempt_gate"] is dependencies["source_attempt_gate"]


def test_child_fetcher_builder_preserves_the_three_attempt_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that omit a deployment override retain the historical cap of three."""
    captured = _build_fetcher_arguments(monkeypatch)

    process_module._build_fetcher(**_build_fetcher_kwargs())

    assert len(captured) == 1
    assert captured[0]["max_attempts"] == 3


@pytest.mark.parametrize("max_attempts", [True, 0, 4, 1.0, None])
def test_child_fetcher_builder_rejects_invalid_caps_before_construction(
    monkeypatch: pytest.MonkeyPatch,
    max_attempts: object,
) -> None:
    """Malformed or non-exact caps cannot create a fetcher or reach its transport."""
    captured = _build_fetcher_arguments(monkeypatch)

    with pytest.raises(process_module.FuyaoFinancialProcessError):
        process_module._build_fetcher(
            **_build_fetcher_kwargs(),
            max_attempts=cast("int", max_attempts),
        )

    assert captured == []


def test_child_result_schema_exposes_the_bound_transport_attempt_limit() -> None:
    """The parent can verify the effective child limit in the fixed result DTO."""
    assert "transport_max_attempts" in process_module._RESULT_FIELDS
