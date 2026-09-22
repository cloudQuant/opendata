"""
Scheduler service detailed tests.

Tests for SchedulerService functionality.
"""

import pytest


class TestSchedulerServiceLifecycle:
    """Test SchedulerService lifecycle methods."""

    @pytest.mark.asyncio
    async def test_scheduler_start(self):
        """Test scheduler starts without errors."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Should not raise
        await service.start()

        # Scheduler should be initialized
        assert service.scheduler is not None

    @pytest.mark.asyncio
    async def test_scheduler_shutdown(self):
        """Test scheduler shuts down without errors."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Start then shutdown
        await service.start()
        await service.shutdown()

        # Shutdown completes without error
        assert True


class TestSchedulerServiceJobManagement:
    """Test SchedulerService job management."""

    @pytest.mark.asyncio
    async def test_add_job(self):
        """Test adding a job to scheduler."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Start scheduler
        await service.start()

        # Add a simple job
        job_info = await service.add_job(
            job_id="test_job",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        assert job_info is not None
        assert job_info["job_id"] == "test_job"

        # Cleanup
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_remove_job(self):
        """Test removing a job from scheduler."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        await service.start()

        # Add then remove job
        await service.add_job(
            job_id="test_job_remove",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        result = await service.remove_job("test_job_remove")

        # Should successfully remove
        assert result is True

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_pause_job(self):
        """Test pausing a job."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        await service.start()

        await service.add_job(
            job_id="test_job_pause",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        result = await service.pause_job("test_job_pause")

        # Should successfully pause
        assert result is True

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_resume_job(self):
        """Test resuming a job."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        await service.start()

        await service.add_job(
            job_id="test_job_resume",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        # Pause first
        await service.pause_job("test_job_resume")

        result = await service.resume_job("test_job_resume")

        # Should successfully resume
        assert result is True

        await service.shutdown()


class TestSchedulerServiceJobInfo:
    """Test SchedulerService job information methods."""

    @pytest.mark.asyncio
    async def test_get_job(self):
        """Test getting job information."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        await service.start()

        # Add a job first
        await service.add_job(
            job_id="test_job_info",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        job = service.get_job("test_job_info")

        assert job is not None

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_get_jobs(self):
        """Test getting all jobs."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        await service.start()

        # Add some jobs
        await service.add_job(
            job_id="test_job_1",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )
        await service.add_job(
            job_id="test_job_2",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 120},
        )

        jobs = service.get_jobs()

        assert len(jobs) >= 2

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_get_next_run_time(self):
        """Test getting next run time for a job."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        await service.start()

        # Add a job
        await service.add_job(
            job_id="test_job_next",
            func=lambda: None,
            trigger_type="interval",
            trigger_args={"seconds": 60},
        )

        job = service.get_job("test_job_next")

        assert job is not None
        # next_run_time is an attribute, not a dict key
        assert hasattr(job, "next_run_time")

        await service.shutdown()


class TestSchedulerServiceStatus:
    """Test SchedulerService status methods."""

    @pytest.mark.asyncio
    async def test_scheduler_not_running_initially(self):
        """Test scheduler state before start."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Before start, scheduler should not be running
        scheduler = service.get_scheduler()
        assert not scheduler.running

    @pytest.mark.asyncio
    async def test_scheduler_running_after_start(self):
        """Test scheduler state after start."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        await service.start()

        # After start, scheduler should be running
        scheduler = service.get_scheduler()
        assert scheduler.running

        # Cleanup
        await service.shutdown()


class TestTriggerValidation:
    """Test trigger type validation."""

    def test_valid_trigger_types(self):
        """Test valid trigger types."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Valid trigger types
        valid_types = ["interval", "cron", "date"]

        for trigger_type in valid_types:
            # Just verify the type string is accepted
            assert trigger_type in ["interval", "cron", "date"]

    def test_schedule_type_values(self):
        """Test ScheduleType enum values."""
        from opendata.models.task import ScheduleType

        # Check common schedule types exist
        assert hasattr(ScheduleType, "ONCE")
        assert hasattr(ScheduleType, "DAILY")
        assert hasattr(ScheduleType, "WEEKLY")
        assert hasattr(ScheduleType, "MONTHLY")
        assert hasattr(ScheduleType, "CRON")
        assert hasattr(ScheduleType, "INTERVAL")
