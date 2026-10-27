"""Deterministic tests for execution-local cooperative checkpoint cadence."""

from __future__ import annotations

import pytest

from opendata.utils.cooperative_checkpoint import _CooperativeCheckpoint


class _Clock:
    """Tiny monotonic clock advanced directly by the test."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_checkpoint_runs_at_its_deadline_and_explicit_renewal_resets_it() -> None:
    clock = _Clock()
    renewals: list[float] = []
    checkpoint = _CooperativeCheckpoint(
        lambda: renewals.append(clock.value),
        interval_seconds=0.25,
        immediate=False,
        clock=clock,
    )

    checkpoint()
    clock.advance(0.24)
    checkpoint()
    assert renewals == []

    clock.advance(0.02)
    checkpoint()
    assert renewals == pytest.approx([0.26])

    checkpoint.mark_renewed()
    clock.advance(0.24)
    checkpoint()
    assert renewals == pytest.approx([0.26])
    clock.advance(0.02)
    checkpoint()
    assert renewals == pytest.approx([0.26, 0.52])


def test_checkpoint_propagates_failures_and_retries_without_marking_success() -> None:
    clock = _Clock()
    expected = RuntimeError("lease lost")
    attempts = 0

    def renew() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise expected

    checkpoint = _CooperativeCheckpoint(
        renew,
        interval_seconds=0.25,
        immediate=False,
        clock=clock,
    )
    clock.advance(0.3)

    with pytest.raises(RuntimeError) as raised:
        checkpoint()
    assert raised.value is expected

    checkpoint()
    assert attempts == 2


def test_checkpoint_state_is_isolated_per_execution() -> None:
    clock = _Clock()
    first_calls: list[bool] = []
    second_calls: list[bool] = []
    first = _CooperativeCheckpoint(
        lambda: first_calls.append(True),
        interval_seconds=0.25,
        immediate=True,
        clock=clock,
    )
    second = _CooperativeCheckpoint(
        lambda: second_calls.append(True),
        interval_seconds=0.25,
        immediate=True,
        clock=clock,
    )

    first()
    first()
    second()

    assert first_calls == [True]
    assert second_calls == [True]
