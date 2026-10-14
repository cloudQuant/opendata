"""Effective PIT period bounds retained by the validated query plan."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, String, Table, create_engine
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    import pytest


def test_validated_plan_keeps_effective_window_bounds_on_private_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_validated_query() -> None:
        from opendata.api import data_query
        from opendata.data.domains import contract_model, dwd_table
        from opendata.pipeline.query import DEFAULT_WINDOW_DAYS

        today = date(2026, 10, 10)

        class FixedDate(date):
            @classmethod
            def today(cls) -> FixedDate:
                return cls(today.year, today.month, today.day)

        class Registry:
            def capabilities(self) -> tuple[SimpleNamespace, ...]:
                return (
                    SimpleNamespace(
                        domain="financial_statement",
                        asset_class="equity",
                        source="ths",
                    ),
                )

        monkeypatch.setattr(data_query, "date", FixedDate)
        monkeypatch.setattr(data_query, "get_registry", Registry)
        monkeypatch.setattr(data_query, "require_domain_access", lambda *_args: None)

        metadata = MetaData()
        contract = contract_model("financial_statement")
        columns = [Column(name, String) for name in contract.model_fields]
        columns.extend(
            Column(name, String) for name in ("source", "_merged_at", "_diff_flag", "_as_of")
        )
        Table(dwd_table("financial_statement"), metadata, *columns)
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        metadata.create_all(engine)
        try:
            plan = await data_query._validated_query(
                engine,
                object(),
                asset_class="equity",
                domain="financial_statement",
                symbols="600000",
                start=None,
                end=None,
                source="ths",
                layer="dwd",
                adjust="none",
                fields=None,
                period=None,
                report_date=None,
                page=1,
                page_size=50,
            )
        finally:
            engine.dispose()

        assert plan.window_end == today
        assert plan.window_start == today - timedelta(days=DEFAULT_WINDOW_DAYS)
        assert plan.query.start == plan.window_start
        assert plan.query.end == plan.window_end

    asyncio.run(exercise_validated_query())
