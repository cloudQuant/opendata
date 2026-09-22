"""
Direct tests for SchedulerService to maximize coverage.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from opendata.services.scheduler_service import (
    SchedulerService,
    get_scheduler_service,
    init_scheduler_service,
)


class TestSchedulerServiceInit:
    def test_init(self):
        svc = SchedulerService()
        assert svc.scheduler is None

    def test_get_scheduler_initializes(self):
        svc = SchedulerService()
        scheduler = svc.get_scheduler()
        assert scheduler is not None

    def test_get_scheduler_singleton(self):
        svc = SchedulerService()
        s1 = svc.get_scheduler()
        s2 = svc.get_scheduler()
        assert s1 is s2


class TestBuildTrigger:
    def test_cron(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("cron", {"cron_expression": "0 8 * * *"})
        assert trigger is not None

    def test_cron_alt_key(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("cron", {"cron": "0 8 * * *"})
        assert trigger is not None

    def test_cron_missing_expr(self):
        svc = SchedulerService()
        with pytest.raises(ValueError, match="Missing cron"):
            svc._build_trigger("cron", {})

    def test_interval(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("interval", {"minutes": 5})
        assert trigger is not None

    def test_interval_with_none(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("interval", {"minutes": 5, "hours": None})
        assert trigger is not None

    def test_date_string(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("date", {"run_date": "2025-01-01T00:00:00"})
        assert trigger is not None

    def test_date_datetime(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("date", {"run_date": datetime.now()})
        assert trigger is not None

    def test_once(self):
        svc = SchedulerService()
        trigger = svc._build_trigger("once", {})
        assert trigger is not None

    def test_unknown(self):
        svc = SchedulerService()
        with pytest.raises(ValueError, match="Unknown trigger"):
            svc._build_trigger("bad_type", {})


class TestStartShutdown:
    @pytest.mark.asyncio
    async def test_start(self):
        svc = SchedulerService()
        await svc.start()
        assert svc.scheduler.running is True
        await svc.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown(self):
        svc = SchedulerService()
        await svc.start()
        await svc.shutdown()
        # After shutdown, scheduler exists but we verify no error was raised

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        svc = SchedulerService()
        await svc.start()
        await svc.start()  # Should not error
        await svc.shutdown()


class TestAddRemoveJob:
    @pytest.mark.asyncio
    async def test_add_cron_job(self):
        svc = SchedulerService()
        await svc.start()
        result = await svc.add_job(
            job_id="test_j1",
            func=lambda: None,
            trigger_type="cron",
            trigger_args={"cron_expression": "0 8 * * *"},
            job_name="Test Job",
        )
        assert result["job_id"] == "test_j1"
        await svc.shutdown()

    @pytest.mark.asyncio
    async def test_remove_job(self):
        svc = SchedulerService()
        await svc.start()
        await svc.add_job("rm_j", lambda: None, "cron", {"cron_expression": "0 8 * * *"})
        assert await svc.remove_job("rm_j") is True
        assert await svc.remove_job("nonexistent") is False
        await svc.shutdown()

    @pytest.mark.asyncio
    async def test_pause_resume_job(self):
        svc = SchedulerService()
        await svc.start()
        await svc.add_job("pr_j", lambda: None, "cron", {"cron_expression": "0 8 * * *"})
        assert await svc.pause_job("pr_j") is True
        assert await svc.pause_job("nonexistent") is False
        assert await svc.resume_job("pr_j") is True
        assert await svc.resume_job("nonexistent") is False
        await svc.shutdown()

    @pytest.mark.asyncio
    async def test_run_job_now(self):
        svc = SchedulerService()
        await svc.start()
        await svc.add_job("rn_j", lambda: None, "cron", {"cron_expression": "0 8 * * *"})
        assert await svc.run_job_now("rn_j") is True
        assert await svc.run_job_now("nonexistent") is False
        await svc.shutdown()


class TestGetJobs:
    @pytest.mark.asyncio
    async def test_get_jobs(self):
        svc = SchedulerService()
        await svc.start()
        await svc.add_job("gj1", lambda: None, "cron", {"cron_expression": "0 8 * * *"})
        jobs = svc.get_jobs()
        assert len(jobs) >= 1
        assert jobs[0]["id"] == "gj1"
        await svc.shutdown()

    @pytest.mark.asyncio
    async def test_get_job(self):
        svc = SchedulerService()
        await svc.start()
        await svc.add_job("gj2", lambda: None, "cron", {"cron_expression": "0 8 * * *"})
        job = svc.get_job("gj2")
        assert job is not None
        assert svc.get_job("nonexistent") is None
        await svc.shutdown()

    def test_get_jobs_no_scheduler(self):
        svc = SchedulerService()
        # scheduler is None initially but get_jobs calls get_scheduler which inits it
        # so we test get_job with None
        svc2 = SchedulerService()
        assert svc2.get_job("x") is None  # scheduler None case handled


class TestJobListener:
    def test_success_event(self):
        svc = SchedulerService()
        event = MagicMock()
        event.exception = None
        event.job_id = "test"
        svc._job_executed_listener(event)  # Should not raise

    def test_error_event(self):
        svc = SchedulerService()
        event = MagicMock()
        event.exception = RuntimeError("fail")
        event.job_id = "test"
        svc._job_executed_listener(event)  # Should not raise


class TestGlobalFunctions:
    def test_init_and_get(self):
        svc = init_scheduler_service()
        assert svc is not None
        assert get_scheduler_service() is svc
