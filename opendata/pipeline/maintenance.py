"""Scheduled maintenance jobs that need an explicit operational policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from opendata.pipeline.retention import (
    purge_diff_report,
    purge_minute_archives,
    purge_raw_response_cache,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine


@dataclass(frozen=True)
class RetentionAction:
    """The candidate and delete counts for one bounded retention target."""

    target: str
    candidates: int | None
    deleted: int | None
    error: str | None = None
    files_deleted: int | None = None

    def as_dict(self) -> dict[str, int | str | None]:
        """Render this action as a JSON-friendly mapping."""
        return {
            "target": self.target,
            "candidates": self.candidates,
            "deleted": self.deleted,
            "error": self.error,
            "files_deleted": self.files_deleted,
        }


@dataclass(frozen=True)
class RetentionRun:
    """One report-only or executing retention pass."""

    dry_run: bool
    actions: tuple[RetentionAction, ...]

    def as_dict(self) -> dict[str, object]:
        """Render counts and failures without hiding partial maintenance errors."""
        return {
            "dry_run": self.dry_run,
            "actions": [action.as_dict() for action in self.actions],
            "failures": [
                {"target": action.target, "error": action.error}
                for action in self.actions
                if action.error is not None
            ],
        }


def run_retention(
    engine: Engine,
    *,
    dry_run: bool = True,
    cache_root: Path | str | None = None,
    minute_root: Path | str | None = None,
    minute_metadata_engine: Engine | None = None,
) -> RetentionRun:
    """Run bounded retention and report candidates separately from deletions.

    Args:
        engine: Warehouse engine. Tests pass an isolated SQLite engine.
        dry_run: Count candidates only; true is the safe default.
        cache_root: Override for configured raw-response cache path.
        minute_root: Override for the configured minute archive path.
        minute_metadata_engine: Distinct main-database engine for the archive index.

    Returns:
        A result with one independent action per bounded retention target.
    """
    from opendata.core.config import settings

    # CACHE_DIR may be shared with other application caches. Scheduled cleanup
    # owns only this raw-response namespace; arbitrary sibling cache files are
    # never considered retention candidates.
    resolved_cache_root = (
        Path(cache_root) if cache_root is not None else Path(settings.cache_dir) / "raw_responses"
    )
    resolved_minute_root = (
        Path(minute_root) if minute_root is not None else Path(settings.data_dir) / "minute_archive"
    )
    minute_action = _run_minute_action(
        engine,
        minute_metadata_engine,
        root=resolved_minute_root,
        dry_run=dry_run,
    )
    actions = (
        _run_action(
            "dq_diff_report",
            candidates=lambda: purge_diff_report(engine, dry_run=True),
            delete=lambda: purge_diff_report(engine, dry_run=False),
            dry_run=dry_run,
        ),
        minute_action,
        _run_action(
            "raw-response-cache",
            candidates=lambda: purge_raw_response_cache(root=resolved_cache_root, dry_run=True),
            delete=lambda: purge_raw_response_cache(root=resolved_cache_root, dry_run=False),
            dry_run=dry_run,
        ),
    )
    return RetentionRun(dry_run=dry_run, actions=actions)


def _run_minute_action(
    warehouse_engine: Engine,
    metadata_engine: Engine | None,
    *,
    root: Path | str,
    dry_run: bool,
) -> RetentionAction:
    """Run minute-file retention only with an isolated mainDB index engine."""
    if metadata_engine is None:
        return RetentionAction(
            "minute-archive",
            candidates=None,
            deleted=None,
            error="minute archive deletion requires an injected main-database metadata engine",
            files_deleted=None,
        )
    if metadata_engine is warehouse_engine or metadata_engine.url == warehouse_engine.url:
        return RetentionAction(
            "minute-archive",
            candidates=None,
            deleted=None,
            error="minute archive metadata engine must be distinct from the warehouse engine",
            files_deleted=None,
        )
    try:
        result = purge_minute_archives(
            root,
            metadata_engine=metadata_engine,
            dry_run=dry_run,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error(f"retention action minute-archive failed: {error}")
        return RetentionAction(
            "minute-archive",
            candidates=None,
            deleted=None,
            error=error,
            files_deleted=None,
        )
    errors = "; ".join(result.errors) if result.errors else None
    return RetentionAction(
        "minute-archive",
        candidates=result.candidates,
        deleted=0 if dry_run else result.deleted,
        error=errors,
        files_deleted=0 if dry_run else result.files_deleted,
    )


def _run_action(
    target: str,
    *,
    candidates: Callable[[], int],
    delete: Callable[[], int],
    dry_run: bool,
) -> RetentionAction:
    """Measure and optionally execute one independent bounded action."""
    try:
        candidate_count = candidates()
        deleted_count = 0 if dry_run else delete()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error(f"retention action {target} failed: {error}")
        return RetentionAction(target, candidates=None, deleted=None, error=error)
    return RetentionAction(target, candidates=candidate_count, deleted=deleted_count)


__all__ = ["RetentionAction", "RetentionRun", "run_retention"]
