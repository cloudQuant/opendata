"""Disk-backed affected-key sequences for bounded pipeline hooks."""

from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, overload


def _encode_key(key: Sequence[object]) -> str:
    """Encode supported business-key scalars without pickle."""
    encoded: list[dict[str, object]] = []
    for value in key:
        if value is None:
            encoded.append({"type": "none"})
        elif isinstance(value, bool):
            encoded.append({"type": "bool", "value": value})
        elif isinstance(value, datetime):
            encoded.append({"type": "datetime", "value": value.isoformat()})
        elif isinstance(value, date):
            encoded.append({"type": "date", "value": value.isoformat()})
        elif isinstance(value, str):
            encoded.append({"type": "str", "value": value})
        elif isinstance(value, int):
            encoded.append({"type": "int", "value": value})
        elif isinstance(value, float) and math.isfinite(value):
            encoded.append({"type": "float", "value": value})
        elif isinstance(value, Decimal) and value.is_finite():
            encoded.append({"type": "decimal", "value": str(value)})
        else:
            raise TypeError(f"unsupported affected-key value type: {type(value).__name__}")
    return json.dumps(encoded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decode_key(payload: str) -> tuple[object, ...]:
    """Decode a key emitted by :func:`_encode_key`."""
    values: list[object] = []
    for item in json.loads(payload):
        value_type = item["type"]
        value = item.get("value")
        if value_type == "none":
            values.append(None)
        elif value_type == "bool":
            values.append(bool(value))
        elif value_type == "datetime":
            values.append(datetime.fromisoformat(value))
        elif value_type == "date":
            values.append(date.fromisoformat(value))
        elif value_type == "str":
            values.append(str(value))
        elif value_type == "int":
            values.append(int(value))
        elif value_type == "float":
            values.append(float(value))
        elif value_type == "decimal":
            values.append(Decimal(value))
        else:  # pragma: no cover - database payload is private to this class
            raise ValueError(f"unknown affected-key value tag: {value_type!r}")
    return tuple(values)


@dataclass(frozen=True)
class SymbolPartitioning:
    """Partition affected keys by a mapping-normalized symbol."""

    symbol_key_index: int
    normalize_symbol: Callable[[object], object]
    batch_size: int = 50

    def __post_init__(self) -> None:
        """Validate the symbol index and the upper bound on each hook batch."""
        if self.symbol_key_index < 0:
            raise ValueError("symbol_key_index must be nonnegative")
        if not 1 <= self.batch_size <= 50:
            raise ValueError("bounded hook batch_size must be between 1 and 50")

    def for_key(self, key: Sequence[object]) -> str:
        """Return the normalized symbol stored at the configured key index."""
        if self.symbol_key_index >= len(key):
            raise ValueError("affected key has no configured symbol field")
        normalized = self.normalize_symbol(key[self.symbol_key_index])
        if not isinstance(normalized, str) or not normalized:
            raise ValueError("normalized affected-key symbol must be a nonempty string")
        return normalized

    def for_request(self, symbol: str) -> str:
        """Return the normalized form used to bind a request symbol to keys."""
        normalized = self.normalize_symbol(symbol)
        if not isinstance(normalized, str) or not normalized:
            raise ValueError("normalized request symbol must be a nonempty string")
        return normalized

    def canonicalize_key(self, key: Sequence[object]) -> tuple[object, ...]:
        """Return a deduplication key with only its symbol normalized."""
        if self.symbol_key_index >= len(key):
            raise ValueError("affected key has no configured symbol field")
        values = list(key)
        values[self.symbol_key_index] = self.for_key(key)
        return tuple(values)


class AffectedKeySpool(Sequence[tuple[object, ...]]):
    """Private SQLite-backed, insertion-ordered affected-key sequence.

    Keys are JSON-encoded with explicit scalar type tags and stored on disk.
    The spool owns a mode-0600 temporary database and removes it (and any
    SQLite sidecars) whenever its context exits.
    """

    _FETCH_SIZE = 256
    _COMMIT_EVERY = 512

    def __init__(self, *, directory: str | os.PathLike[str] | None = None) -> None:
        """Create a private spool database, optionally beneath ``directory``.

        Args:
            directory: Temporary-file directory; the platform default is used
                when omitted.
        """
        descriptor, path = tempfile.mkstemp(
            prefix="opendata-affected-keys-", suffix=".sqlite3", dir=directory
        )
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._count = 0
        self._since_commit = 0
        self._closed = False
        try:
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._connection = sqlite3.connect(self.path)
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-2048")
            self._connection.executescript(
                "CREATE TABLE affected_keys ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "symbol TEXT NOT NULL, payload TEXT NOT NULL, "
                "dedupe_payload TEXT NOT NULL UNIQUE);"
                "CREATE INDEX affected_keys_symbol_seq ON affected_keys(symbol, seq);"
                "CREATE TABLE affected_key_counts ("
                "symbol TEXT PRIMARY KEY, count INTEGER NOT NULL);"
            )
            self._connection.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> AffectedKeySpool:
        """Return the active spool context manager."""
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        """Close and remove the spool without suppressing an active exception."""
        self.close()
        return False

    def append(
        self,
        key: Sequence[object],
        *,
        symbol: str,
        dedupe_key: Sequence[object] | None = None,
    ) -> None:
        """Append one original key unless its typed dedupe tuple was stored."""
        connection = self._ensure_open()
        payload = _encode_key(key)
        dedupe_payload = _encode_key(key if dedupe_key is None else dedupe_key)
        cursor = connection.execute(
            "INSERT OR IGNORE INTO affected_keys(symbol, payload, dedupe_payload) VALUES (?, ?, ?)",
            (symbol, payload, dedupe_payload),
        )
        if cursor.rowcount == 1:
            connection.execute(
                "INSERT INTO affected_key_counts(symbol, count) VALUES (?, 1) "
                "ON CONFLICT(symbol) DO UPDATE SET count=count + 1",
                (symbol,),
            )
            self._count += 1
            self._since_commit += 1
            if self._since_commit >= self._COMMIT_EVERY:
                connection.commit()
                self._since_commit = 0
            return
        existing = connection.execute(
            "SELECT symbol FROM affected_keys WHERE dedupe_payload = ?", (dedupe_payload,)
        ).fetchone()
        if existing is not None and existing[0] != symbol:
            raise ValueError("one affected key normalized to multiple partition symbols")

    def partition(self, symbols: Sequence[str]) -> Sequence[tuple[object, ...]]:
        """Return a lazy sequence view limited to normalized symbols."""
        self._ensure_open()
        return _AffectedKeyView(self, tuple(dict.fromkeys(symbols)))

    def __len__(self) -> int:
        """Return the in-memory O(1) count of inserted unique keys."""
        self._ensure_open()
        return self._count

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        """Iterate the complete key sequence in insertion order."""
        return self.iter_keys()

    @overload
    def __getitem__(self, index: int) -> tuple[object, ...]: ...

    @overload
    def __getitem__(self, index: slice) -> list[tuple[object, ...]]: ...

    def __getitem__(self, index: int | slice) -> tuple[object, ...] | list[tuple[object, ...]]:
        """Read one key or an explicitly requested slice from disk."""
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            selected = set(range(start, stop, step))
            return [key for position, key in enumerate(self) if position in selected]
        offset = index if index >= 0 else len(self) + index
        if offset < 0 or offset >= len(self):
            raise IndexError(index)
        connection = self._ensure_open()
        connection.commit()
        row = connection.execute(
            "SELECT payload FROM affected_keys ORDER BY seq LIMIT 1 OFFSET ?", (offset,)
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return _decode_key(row[0])

    def iter_keys(self, symbols: Sequence[str] | None = None) -> Iterator[tuple[object, ...]]:
        """Yield keys in insertion order using bounded SQLite fetches."""
        connection = self._ensure_open()
        connection.commit()
        self._since_commit = 0
        if symbols is None:
            cursor = connection.execute("SELECT payload FROM affected_keys ORDER BY seq")
        else:
            unique_symbols = tuple(dict.fromkeys(symbols))
            if not unique_symbols:
                return
            placeholders = ",".join("?" for _ in unique_symbols)
            cursor = connection.execute(
                f"SELECT payload FROM affected_keys WHERE symbol IN ({placeholders}) ORDER BY seq",  # noqa: S608  # bound values; placeholder count only
                unique_symbols,
            )
        try:
            while True:
                rows = cursor.fetchmany(self._FETCH_SIZE)
                if not rows:
                    break
                for (payload,) in rows:
                    yield _decode_key(payload)
        finally:
            cursor.close()

    def _count_for_symbols(self, symbols: Sequence[str]) -> int:
        connection = self._ensure_open()
        unique_symbols = tuple(dict.fromkeys(symbols))
        if not unique_symbols:
            return 0
        placeholders = ",".join("?" for _ in unique_symbols)
        row = connection.execute(
            f"SELECT COALESCE(SUM(count), 0) FROM affected_key_counts "  # noqa: S608
            f"WHERE symbol IN ({placeholders})",
            unique_symbols,
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        """Close and unlink the temporary database, idempotently."""
        if self._closed:
            return
        self._closed = True
        connection, self._connection = self._connection, None
        try:
            if connection is not None:
                connection.close()
        finally:
            for path in (
                self.path,
                Path(f"{self.path}-journal"),
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise RuntimeError("affected-key spool is closed")
        return self._connection


class _AffectedKeyView(Sequence[tuple[object, ...]]):
    def __init__(self, spool: AffectedKeySpool, symbols: tuple[str, ...]) -> None:
        self._spool = spool
        self._symbols = symbols

    def __len__(self) -> int:
        return self._spool._count_for_symbols(self._symbols)

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return self._spool.iter_keys(self._symbols)

    @overload
    def __getitem__(self, index: int) -> tuple[object, ...]: ...

    @overload
    def __getitem__(self, index: slice) -> list[tuple[object, ...]]: ...

    def __getitem__(self, index: int | slice) -> tuple[object, ...] | list[tuple[object, ...]]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            selected = set(range(start, stop, step))
            return [key for position, key in enumerate(self) if position in selected]
        offset = index if index >= 0 else len(self) + index
        if offset < 0 or offset >= len(self):
            raise IndexError(index)
        for position, key in enumerate(self):
            if position == offset:
                return key
        raise IndexError(index)
