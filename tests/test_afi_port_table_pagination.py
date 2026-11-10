"""Behavioural tests for the pagination core ported from ai-for-investor."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from opendata.afi_port.services.table_pagination import (
    STRATEGY_PRIMARY_KEY,
    STRATEGY_ROWID,
    STRATEGY_UNIQUE_KEY,
    StableOrderingUnavailableError,
    TableOrdering,
    TotalCountCache,
    TotalCountKey,
    quote_identifier,
    reflect_table_ordering,
    resolve_ordering,
    resolve_total_count,
)


def _order(**kwargs: Any) -> TableOrdering:
    defaults: dict[str, Any] = {
        "table_name": "t",
        "dialect_name": "mysql",
        "pk_columns": (),
        "unique_constraints": (),
        "column_nullable": {},
        "has_selectable_rowid": False,
    }
    defaults.update(kwargs)
    return resolve_ordering(**defaults)


def test_primary_key_beats_a_unique_key() -> None:
    ordering = _order(
        pk_columns=("id",),
        unique_constraints=(("code",),),
        column_nullable={"id": False, "code": False},
    )
    assert ordering.strategy == STRATEGY_PRIMARY_KEY
    assert ordering.columns == ("id",)


def test_not_null_unique_key_is_used_and_candidates_are_deterministic() -> None:
    ordering = _order(
        unique_constraints=(("b", "a"), ("only",)),
        column_nullable={"a": False, "b": False, "only": False},
    )
    assert ordering.strategy == STRATEGY_UNIQUE_KEY
    assert ordering.columns == ("only",)


def test_nullable_unique_key_is_not_a_total_order() -> None:
    with pytest.raises(StableOrderingUnavailableError):
        _order(
            unique_constraints=(("code",),),
            column_nullable={"code": True},
        )


def test_sqlite_falls_back_to_rowid_but_mysql_fails_closed() -> None:
    sqlite_ordering = _order(dialect_name="sqlite", has_selectable_rowid=True)
    assert sqlite_ordering.strategy == STRATEGY_ROWID
    assert sqlite_ordering.columns == ("rowid",)

    with pytest.raises(StableOrderingUnavailableError, match="no provable stable ordering"):
        _order()


def test_unquotable_primary_key_is_skipped_instead_of_emitted() -> None:
    ordering = _order(pk_columns=("bad name",), dialect_name="sqlite", has_selectable_rowid=True)
    assert ordering.strategy == STRATEGY_ROWID


def test_quote_identifier_accepts_chinese_and_rejects_sql_metacharacters() -> None:
    assert quote_identifier("收盘") == "`收盘`"
    for hostile in ("x; DROP TABLE t", "a`b", "1abc"):
        with pytest.raises(StableOrderingUnavailableError):
            quote_identifier(hostile)


def test_ordering_response_is_explicit_about_the_total_order() -> None:
    ordering = TableOrdering(strategy=STRATEGY_PRIMARY_KEY, columns=("id",))
    assert ordering.as_response() == {
        "strategy": "primary_key",
        "columns": ["id"],
        "direction": "asc",
        "is_total_order": True,
    }


def test_reflects_primary_key_unique_key_and_rowid_from_sqlite_schema() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE with_pk (id INTEGER PRIMARY KEY, v TEXT)"))
        connection.execute(text("CREATE TABLE with_unique (code TEXT NOT NULL UNIQUE, v TEXT)"))
        connection.execute(text("CREATE TABLE keyless (v TEXT)"))

        assert reflect_table_ordering(connection, "with_pk").strategy == STRATEGY_PRIMARY_KEY
        assert reflect_table_ordering(connection, "with_unique").strategy == STRATEGY_UNIQUE_KEY
        assert reflect_table_ordering(connection, "keyless").strategy == STRATEGY_ROWID


def test_total_count_cache_expires_evicts_and_bounds_entries() -> None:
    now = [100.0]
    cache = TotalCountCache(ttl_seconds=10.0, max_entries=2, clock=lambda: now[0])
    key = TotalCountKey(database="db", table_name="t")

    cache.put(key, 7)
    assert cache.get(key) == (7, 0.0)

    now[0] = 109.9
    assert cache.get(key) == (7, pytest.approx(9.9))

    now[0] = 110.0
    assert cache.get(key) is None

    first = TotalCountKey(database="db", table_name="a")
    second = TotalCountKey(database="db", table_name="b")
    third = TotalCountKey(database="db", table_name="c")
    cache.put(first, 1)
    cache.put(second, 2)
    cache.put(third, 3)
    assert len(cache) == 2
    assert cache.get(first) is None

    cache.evict_table("b")
    assert cache.get(second) is None
    cache.clear()
    assert len(cache) == 0


def test_resolve_total_count_measures_once_and_reports_cached_age() -> None:
    pytest.importorskip("aiosqlite")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
                await connection.execute(text("INSERT INTO t (id) VALUES (1), (2), (3)"))

            cache = TotalCountCache(ttl_seconds=60.0)
            key = TotalCountKey(database="db", table_name="t")

            async with engine.connect() as connection:
                total, source, age = await resolve_total_count(
                    connection,
                    count_sql="SELECT COUNT(*) FROM t",
                    cache=cache,
                    key=key,
                )
                assert (total, source) == (3, "live")
                assert age == 0.0

                cached_total, cached_source, cached_age = await resolve_total_count(
                    connection,
                    count_sql="SELECT COUNT(*) FROM t",
                    cache=cache,
                    key=key,
                )
                assert (cached_total, cached_source) == (3, "cached")
                assert cached_age >= 0.0
        finally:
            await engine.dispose()

    asyncio.run(scenario())
