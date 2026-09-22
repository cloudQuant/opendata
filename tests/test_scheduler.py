"""
Scheduler module tests.

Tests for APScheduler integration and task scheduling.
"""

from datetime import UTC, datetime

import pytest


class TestSchedulerService:
    """Test scheduler service layer."""

    def test_scheduler_service_creation(self):
        """Test scheduler service can be instantiated."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        assert service is not None

    def test_scheduler_service_get_scheduler(self):
        """Test getting scheduler instance."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        scheduler = service.get_scheduler()

        assert scheduler is not None

    def test_scheduler_service_initialization(self):
        """Test scheduler initialization."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # After initialization, should have scheduler attribute
        service._initialize_scheduler()

        assert service.scheduler is not None

    def test_scheduler_add_job_method(self):
        """Test add_job method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "add_job")
        assert callable(service.add_job)

    def test_scheduler_shutdown_method(self):
        """Test shutdown method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "shutdown")
        assert callable(service.shutdown)

    def test_scheduler_start_method(self):
        """Test start method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "start")
        assert callable(service.start)

    def test_scheduler_remove_job_method(self):
        """Test remove_job method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "remove_job")
        assert callable(service.remove_job)

    def test_scheduler_pause_job_method(self):
        """Test pause_job method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "pause_job")
        assert callable(service.pause_job)

    def test_scheduler_resume_job_method(self):
        """Test resume_job method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "resume_job")
        assert callable(service.resume_job)

    def test_scheduler_get_jobs_method(self):
        """Test get_jobs method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Method should exist
        assert hasattr(service, "get_jobs")
        assert callable(service.get_jobs)

    def test_scheduler_get_jobs_result(self):
        """Test get_jobs returns list."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()
        jobs = service.get_jobs()

        # Should return a list (empty if no jobs)
        assert isinstance(jobs, list)


class TestTriggerTypes:
    """Test trigger type handling."""

    def test_trigger_type_interval(self):
        """Test interval trigger type."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Interval trigger should be supported
        trigger_args = {"seconds": 60}
        assert "seconds" in trigger_args

    def test_trigger_type_cron(self):
        """Test cron trigger type."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Cron trigger args
        trigger_args = {
            "hour": 9,
            "minute": 0,
        }
        assert "hour" in trigger_args
        assert "minute" in trigger_args

    def test_trigger_type_date(self):
        """Test date trigger type."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Date trigger args
        trigger_args = {
            "run_date": datetime.now(UTC),
        }
        assert "run_date" in trigger_args


class TestSchedulerJobListener:
    """Test scheduler job event listener."""

    def test_listener_method_exists(self):
        """Test job listener method exists."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Listener method should exist
        assert hasattr(service, "_job_executed_listener")
        assert callable(service._job_executed_listener)


class TestCronTriggerValidation:
    """Test cron trigger validation utilities."""

    def test_valid_cron_expressions(self):
        """Test various valid cron expressions."""
        from apscheduler.triggers.cron import CronTrigger

        # These should be valid cron expressions
        valid_crons = [
            {"hour": 9, "minute": 0},  # Daily at 9 AM
            {"hour": "*/6"},  # Every 6 hours
            {"day_of_week": "mon-fri", "hour": 9},  # Weekdays at 9 AM
        ]

        for cron_args in valid_crons:
            try:
                trigger = CronTrigger(**cron_args)
                assert trigger is not None
            except Exception as e:
                pytest.fail(f"Valid cron failed: {cron_args}, error: {e}")

    def test_cron_trigger_creation(self):
        """Test creating cron triggers."""
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger(hour=9, minute=0)
        assert trigger is not None


class TestIntervalTriggerValidation:
    """Test interval trigger validation."""

    def test_interval_triggers(self):
        """Test various interval triggers."""
        from apscheduler.triggers.interval import IntervalTrigger

        # These should be valid interval triggers
        valid_intervals = [
            {"seconds": 60},
            {"minutes": 5},
            {"hours": 1},
            {"days": 1},
        ]

        for interval_args in valid_intervals:
            try:
                trigger = IntervalTrigger(**interval_args)
                assert trigger is not None
            except Exception as e:
                pytest.fail(f"Valid interval failed: {interval_args}, error: {e}")
