"""Pipeline retry tests (B3.2 / AC-13: 失败清单一键重试).

The retry primitive is checkpoint-level: failed shards are listed and
reset to ``pending`` so the next run of the same window (the runner
skips only ``done`` shards) re-processes them. Tests cover the service
queries and the HTTP surface.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from opendata.models.pipeline import PipelineProgress, ShardStatus
from opendata.pipeline.retry import list_failed_shards, mark_failed_for_retry


def _shard(
    *,
    pipeline_id: str,
    domain: str = "stock_daily",
    source: str = "ths",
    shard: int,
    status: ShardStatus,
    error: str | None = None,
    window_start: date = date(2026, 9, 23),
    window_end: date = date(2026, 9, 23),
) -> PipelineProgress:
    return PipelineProgress(
        pipeline_id=pipeline_id,
        domain=domain,
        source=source,
        shard=shard,
        window_start=window_start,
        window_end=window_end,
        status=status,
        rows_written=0,
        error=error,
        updated_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
    )


class TestListFailures:
    @pytest.mark.asyncio
    async def test_lists_only_failed_shards(self, test_db):
        test_db.add_all(
            [
                _shard(pipeline_id="p:1", shard=0, status=ShardStatus.FAILED, error="boom"),
                _shard(pipeline_id="p:1", shard=1, status=ShardStatus.DONE),
                _shard(pipeline_id="p:2", shard=0, status=ShardStatus.FAILED, error="nope"),
            ]
        )
        await test_db.commit()

        failures = await list_failed_shards(test_db)

        assert len(failures) == 2
        assert {item.pipeline_id for item in failures} == {"p:1", "p:2"}
        assert all(item.error for item in failures)
        assert failures[0].window_start == date(2026, 9, 23)


class TestMarkForRetry:
    async def test_resets_failed_to_pending_and_clears_the_error(self, test_db):
        test_db.add_all(
            [
                _shard(pipeline_id="p:1", shard=0, status=ShardStatus.FAILED, error="boom"),
                _shard(pipeline_id="p:1", shard=1, status=ShardStatus.DONE),
            ]
        )
        await test_db.commit()

        count = await mark_failed_for_retry(test_db)

        assert count == 1
        row = (
            await test_db.execute(select(PipelineProgress).where(PipelineProgress.shard == 0))
        ).scalar_one()
        assert row.status == ShardStatus.PENDING
        assert row.error is None

    async def test_done_shards_are_never_touched(self, test_db):
        test_db.add(_shard(pipeline_id="p:1", shard=1, status=ShardStatus.DONE, error=None))
        await test_db.commit()

        count = await mark_failed_for_retry(test_db)

        assert count == 0
        row = (
            await test_db.execute(select(PipelineProgress).where(PipelineProgress.shard == 1))
        ).scalar_one()
        assert row.status == ShardStatus.DONE

    async def test_domain_filter_restricts_the_reset(self, test_db):
        test_db.add_all(
            [
                _shard(pipeline_id="p:1", shard=0, status=ShardStatus.FAILED, error="a"),
                _shard(
                    pipeline_id="p:2",
                    shard=0,
                    domain="economy_cpi",
                    status=ShardStatus.FAILED,
                    error="b",
                ),
            ]
        )
        await test_db.commit()

        count = await mark_failed_for_retry(test_db, domain="stock_daily")

        assert count == 1

    async def test_partial_window_fails_closed(self, test_db):
        with pytest.raises(ValueError, match="window"):
            await mark_failed_for_retry(test_db, window=(date(2026, 9, 23), None))


@pytest.mark.asyncio
class TestPipelineApi:
    async def test_failures_endpoint_requires_auth(self, test_client):
        response = await test_client.get("/api/v1/pipeline/failures")

        assert response.status_code == 401

    async def test_retry_endpoint_is_idempotent_and_reports_the_count(
        self, test_client, test_user_token, test_db
    ):
        test_db.add(_shard(pipeline_id="p:1", shard=0, status=ShardStatus.FAILED, error="boom"))
        await test_db.commit()

        headers = {"Authorization": f"Bearer {test_user_token}"}
        first = await test_client.post("/api/v1/pipeline/retry-failed", headers=headers)
        second = await test_client.post("/api/v1/pipeline/retry-failed", headers=headers)

        assert first.status_code == 200
        assert first.json()["data"]["reset"] == 1
        assert second.json()["data"]["reset"] == 0  # nothing left to reset
