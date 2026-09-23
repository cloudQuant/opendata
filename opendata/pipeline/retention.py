"""Warehouse and cache retention (design §8.5, milestone A4.10).

The policy of design §8.5 is declared, checked and enforced here:

* 日线、财务、元数据 (``ods_*`` / ``dwd_*``) 永久保留
* 分钟线 N 年（可配；不进 MySQL，走 Parquet 归档）
* 原始响应缓存 TTL（可配），不写版本库
* ``dq_diff_report`` 聚合保留 N 天，明细走导出文件

Every purge path resolves the target through :func:`rule_for`, so a
bounded-lifetime asset can never be asked to delete a permanent one
(fail closed instead of dropping history).
"""

from __future__ import annotations

import csv
import enum
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from opendata.core.config import settings
from opendata.pipeline.diff_report import DQ_DIFF_REPORT_TABLE, REPORT_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Engine


class RetentionError(RuntimeError):
    """Raised when a retention request contradicts the declared policy."""


class RetentionMode(enum.Enum):
    """Lifetime kind of one retained asset."""

    PERMANENT = "permanent"
    DAYS = "days"
    YEARS = "years"
    TTL = "ttl"


@dataclass(frozen=True)
class RetentionRule:
    """One row of the declared retention policy.

    Attributes:
        asset: Human-readable asset name (design §8.5 wording).
        pattern: Table name, table prefix or synthetic target it covers.
        mode: Lifetime kind.
        limit: Bound for the bounded modes (``None`` for permanent).
        unit: Unit of ``limit`` for display.
        note: Where the value comes from / why.
    """

    asset: str
    pattern: str
    mode: RetentionMode
    limit: int | None
    unit: str
    note: str


#: Synthetic targets that are not warehouse tables.
MINUTE_ARCHIVE = "minute-archive"
RAW_RESPONSE_CACHE = "raw-response-cache"


def retention_policy() -> tuple[RetentionRule, ...]:
    """Return the declared retention policy.

    Returns:
        One rule per retained asset; the configurable limits are read
        from settings so the declaration and the enforcement cannot
        drift apart.
    """
    return (
        RetentionRule(
            asset="ods 原始行情（日线/财务/元数据）",
            pattern="ods_",
            mode=RetentionMode.PERMANENT,
            limit=None,
            unit="",
            note="设计 §8.5：永久保留；分区维护不做删除",
        ),
        RetentionRule(
            asset="dwd 合并层",
            pattern="dwd_",
            mode=RetentionMode.PERMANENT,
            limit=None,
            unit="",
            note="由 ods 派生，随 ods 永久保留（可重算，不单列生命周期）",
        ),
        RetentionRule(
            asset="dq_diff_report 聚合",
            pattern=DQ_DIFF_REPORT_TABLE,
            mode=RetentionMode.DAYS,
            limit=settings.retention_diff_report_days,
            unit="天",
            note="设计 §8.5：聚合保留 N 天，明细走导出文件（RETENTION_DIFF_REPORT_DAYS）",
        ),
        RetentionRule(
            asset="分钟线归档（Parquet）",
            pattern=MINUTE_ARCHIVE,
            mode=RetentionMode.YEARS,
            limit=settings.retention_minute_years,
            unit="年",
            note="设计 §8.5/D13：分钟线不进 MySQL，保留 N 年（RETENTION_MINUTE_YEARS）",
        ),
        RetentionRule(
            asset="原始响应缓存",
            pattern=RAW_RESPONSE_CACHE,
            mode=RetentionMode.TTL,
            limit=settings.cache_ttl_seconds,
            unit="秒",
            note="设计 §13：只存原始响应，TTL 可配且不写版本库（CACHE_TTL_SECONDS）",
        ),
    )


def _is_covered(target: str) -> bool:
    """Return whether any rule covers the target.

    Args:
        target: Warehouse table name or synthetic target.

    Returns:
        ``True`` when a rule matches by name or prefix.
    """
    return any(
        rule.pattern == target or (rule.pattern.endswith("_") and target.startswith(rule.pattern))
        for rule in retention_policy()
    )


def rule_for(target: str) -> RetentionRule:
    """Resolve the rule covering a table or synthetic target.

    Args:
        target: Warehouse table name or one of the synthetic targets
            (:data:`MINUTE_ARCHIVE`, :data:`RAW_RESPONSE_CACHE`).

    Returns:
        The matching rule; exact matches win over prefixes.

    Raises:
        RetentionError: If nothing covers the target (fail closed: an
            unknown asset has no declared lifetime).
    """
    policy = retention_policy()
    for rule in policy:
        if rule.pattern == target:
            return rule
    for rule in policy:
        if rule.pattern.endswith("_") and target.startswith(rule.pattern):
            return rule
    raise RetentionError(f"no retention rule covers {target!r}; declare one before touching it")


def assert_policy_covers(tables: Iterable[str]) -> None:
    """Assert every given table has a declared lifetime.

    Args:
        tables: Warehouse tables that must be covered.

    Raises:
        RetentionError: If any table is uncovered, naming all of them.
    """
    uncovered = [table for table in tables if not _is_covered(table)]
    if uncovered:
        raise RetentionError(f"no retention rule covers: {', '.join(sorted(uncovered))}")


def _identifier(value: str) -> str:
    """Return a backtick-quoted identifier after a word-character check.

    Args:
        value: Table or column name.

    Returns:
        The quoted identifier.

    Raises:
        RetentionError: If the name is not a plain identifier.
    """
    if not value or not value.replace("_", "").isalnum():
        raise RetentionError(f"{value!r} is not a plain SQL identifier")
    return f"`{value}`"


def purge_expired_rows(
    engine: Engine,
    *,
    table: str,
    date_column: str,
    older_than_days: int | None = None,
) -> int:
    """Delete rows older than the table's declared retention.

    Args:
        engine: Warehouse engine.
        table: Table to purge; must be a bounded (``days``) rule.
        date_column: Timestamp column driving the cutoff.
        older_than_days: Override for the rule's limit.

    Returns:
        Number of deleted rows.

    Raises:
        RetentionError: If the table is permanent/uncovered, the limit
            is not positive, or a name is not a plain identifier.
    """
    rule = rule_for(table)
    if rule.mode is not RetentionMode.DAYS:
        raise RetentionError(
            f"{table} is declared {rule.mode.value} ({rule.asset}); refusing to purge it"
        )
    days = older_than_days if older_than_days is not None else rule.limit
    if not days or days <= 0:
        raise RetentionError(f"refusing to purge {table} with a non-positive window: {days!r}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    statement = f"DELETE FROM {_identifier(table)} WHERE {_identifier(date_column)} < :cutoff"
    with engine.begin() as connection:
        result = connection.execute(text(statement), {"cutoff": cutoff})
    return int(result.rowcount or 0)


def purge_diff_report(engine: Engine) -> int:
    """Purge ``dq_diff_report`` rows past the declared window.

    Args:
        engine: Warehouse engine.

    Returns:
        Number of deleted rows.
    """
    return purge_expired_rows(
        engine,
        table=DQ_DIFF_REPORT_TABLE,
        date_column="checked_at",
    )


def export_diff_details(engine: Engine, path: Path | str) -> int:
    """Export the whole diff detail table to CSV (design §8.5).

    The aggregate stays in MySQL for N days; the details live in the
    exported file, which is what the retention policy points at.

    Args:
        engine: Warehouse engine.
        path: Destination CSV path (parent directories are created).

    Returns:
        Number of exported rows.
    """
    columns = ", ".join(_identifier(column) for column in REPORT_COLUMNS)
    statement = f"SELECT {columns} FROM {_identifier(DQ_DIFF_REPORT_TABLE)} ORDER BY `checked_at`"
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        rows = connection.execute(text(statement)).all()
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        writer.writerows(rows)
    return len(rows)


def purge_minute_archives(
    root: Path | str,
    *,
    keep_years: int | None = None,
    today: datetime | None = None,
) -> list[Path]:
    """Remove minute-line Parquet year partitions past the window.

    The archive layout is ``domain/symbol/year`` (design §13); every
    directory named after a year outside the retained window is
    removed. A missing root is a no-op: archives that were never
    written must not fail a maintenance run.

    Args:
        root: Archive root directory.
        keep_years: Override for the rule's limit.
        today: Override for "now" (tests).

    Returns:
        The removed year directories.

    Raises:
        RetentionError: If the window is not positive.
    """
    rule = rule_for(MINUTE_ARCHIVE)
    years = keep_years if keep_years is not None else rule.limit
    if not years or years <= 0:
        raise RetentionError(f"refusing to purge {MINUTE_ARCHIVE} with window {years!r}")
    archive_root = Path(root)
    if not archive_root.exists():
        return []
    cutoff_year = (today or datetime.now(timezone.utc)).year - years + 1
    removed = []
    for candidate in sorted(archive_root.rglob("*")):
        name = candidate.name
        if candidate.is_dir() and len(name) == 4 and name.isdigit() and int(name) < cutoff_year:
            shutil.rmtree(candidate)
            removed.append(candidate)
    return removed


def purge_raw_response_cache(
    *,
    root: Path | str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> int:
    """Remove cached raw responses older than the TTL (design §13).

    Args:
        root: Cache root; defaults to the configured ``CACHE_DIR``.
        ttl_seconds: Override for the rule's limit.
        now: Override for "now" (tests).

    Returns:
        Number of removed files.

    Raises:
        RetentionError: If the TTL is not positive.
    """
    rule = rule_for(RAW_RESPONSE_CACHE)
    ttl = ttl_seconds if ttl_seconds is not None else rule.limit
    if not ttl or ttl <= 0:
        raise RetentionError(f"refusing to purge {RAW_RESPONSE_CACHE} with TTL {ttl!r}")
    cache_root = Path(root) if root is not None else Path(settings.cache_dir)
    if not cache_root.exists():
        return 0
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - ttl
    removed = 0
    for path in cache_root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
