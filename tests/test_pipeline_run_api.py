"""Manual pipeline-run trigger tests (AC-13, 验收文档 §0-4 fallback).

The batch itself is covered in :mod:`tests.test_pipeline_jobs`; here the
HTTP surface is checked with the job function stubbed, so a test never
touches the warehouse or a provider.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from opendata.api import pipeline as run_api
from opendata.pipeline.jobs import JobResult
from opendata.pipeline.runner import PipelineOutcome, Window

WINDOW = Window(start=date(2026, 9, 24), end=date(2026, 9, 24))


@pytest.fixture(autouse=True)
def _isolated_run_registry():
    """Keep the in-memory run registry from leaking between tests."""
    run_api._RUNS.clear()
    run_api._TASKS.clear()
    yield
    run_api._RUNS.clear()
    run_api._TASKS.clear()


def _result(failures: int = 0) -> JobResult:
    outcome = PipelineOutcome(
        pipeline_id="stock_daily:ths:2026-09-24..2026-09-24",
        shards_total=2,
        shards_done=2,
        shards_failed=failures,
        rows_written=42,
    )
    return JobResult(
        domain="stock_daily",
        sources=("ths",),
        window=WINDOW,
        symbols=3,
        outcomes={"ths": outcome},
        dwd_rows=40,
        freshness={"ods:ths": "2026-09-24", "dwd": "2026-09-24"},
    )


@pytest.fixture
def headers(test_user_token: str) -> dict[str, str]:
    """Authenticated header for the pipeline endpoints."""
    return {"Authorization": f"Bearer {test_user_token}"}


class TestPipelineRunTrigger:
    async def test_run_requires_authentication(self, test_client) -> None:
        response = await test_client.post("/api/v1/pipeline/run")

        assert response.status_code == 401

    async def test_unsupported_domain_is_refused_before_the_batch_starts(
        self, test_client, headers
    ) -> None:
        response = await test_client.post(
            "/api/v1/pipeline/run", headers=headers, params={"domain": "index_daily"}
        )

        assert response.status_code == 400
        assert "no pipeline builder" in response.json()["detail"]
        assert run_api._RUNS == {}

    async def test_accepted_run_reports_a_run_id_and_echoes_the_request(
        self, test_client, headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_job(**kwargs: Any) -> JobResult:
            return _result()

        monkeypatch.setattr(run_api, "run_incremental_job", fake_job)

        response = await test_client.post(
            "/api/v1/pipeline/run",
            headers=headers,
            params={"source": "ths", "symbols": ["600519", "000001"]},
        )

        assert response.status_code == 202
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["request"]["source"] == "ths"
        assert await run_api._TASKS[data["run_id"]] is None

    async def test_the_finished_run_is_readable_by_run_id(
        self, test_client, headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_job(**kwargs: Any) -> JobResult:
            return _result()

        monkeypatch.setattr(run_api, "run_incremental_job", fake_job)
        started = await test_client.post("/api/v1/pipeline/run", headers=headers)
        run_id = started.json()["data"]["run_id"]
        await run_api._TASKS[run_id]

        response = await test_client.get(f"/api/v1/pipeline/run/{run_id}", headers=headers)

        body = response.json()["data"]
        assert response.status_code == 200
        assert body["status"] == "succeeded"
        assert body["result"]["dwd_rows"] == 40
        assert body["result"]["freshness"]["dwd"] == "2026-09-24"

    async def test_a_failing_batch_is_reported_not_raised(
        self, test_client, headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken_job(**kwargs: Any) -> JobResult:
            raise RuntimeError("upstream refused")

        monkeypatch.setattr(run_api, "run_incremental_job", broken_job)
        started = await test_client.post("/api/v1/pipeline/run", headers=headers)
        run_id = started.json()["data"]["run_id"]
        await run_api._TASKS[run_id]

        body = (await test_client.get(f"/api/v1/pipeline/run/{run_id}", headers=headers)).json()

        assert body["data"]["status"] == "failed"
        assert body["data"]["error"] == "RuntimeError: upstream refused"

    async def test_partial_run_is_marked_when_shards_failed(
        self, test_client, headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def partial_job(**kwargs: Any) -> JobResult:
            return _result(failures=1)

        monkeypatch.setattr(run_api, "run_incremental_job", partial_job)
        started = await test_client.post("/api/v1/pipeline/run", headers=headers)
        run_id = started.json()["data"]["run_id"]
        await run_api._TASKS[run_id]

        body = (await test_client.get(f"/api/v1/pipeline/run/{run_id}", headers=headers)).json()

        assert body["data"]["status"] == "partial"
        assert body["data"]["result"]["per_source"]["ths"]["shards_failed"] == 1

    async def test_an_unknown_run_id_is_not_found(self, test_client, headers) -> None:
        response = await test_client.get("/api/v1/pipeline/run/000000000000", headers=headers)

        assert response.status_code == 404
