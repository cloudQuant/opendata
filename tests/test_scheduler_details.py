"""
Scheduler service detailed tests.

Tests for SchedulerService functionality.
"""

import asyncio

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from opendata.services.scheduler_service import SchedulerService


class TestSchedulerServiceLifecycle:
    """Test SchedulerService lifecycle methods."""

    @pytest.mark.asyncio
    async def test_scheduler_start(self):
        """Test scheduler starts without errors."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Should not raise
        await service.start()

        # 启动的可观察后果：实例存在且真的在跑（只断 `is not None` 时 start() 空转也绿）
        assert service.scheduler is not None
        assert service.scheduler.running is True
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_scheduler_shutdown(self):
        """Test scheduler shuts down without errors."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Start then shutdown
        await service.start()
        await service.shutdown()

        # APScheduler 的 stop 是 call_soon_threadsafe 投递的：先让循环走一拍再看状态。
        # 「shutdown 没抛异常」不是判据（那正是曾经的 assert True）；状态必须翻转。
        await asyncio.sleep(0)

        assert service.scheduler is not None
        assert service.scheduler.running is False


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

    @pytest.mark.parametrize(
        ("trigger_type", "trigger_args", "expected"),
        [
            ("cron", {"cron_expression": "0 8 * * 1-5"}, CronTrigger),
            ("interval", {"minutes": 30}, IntervalTrigger),
            ("date", {"run_date": "2024-01-02T09:30:00"}, DateTrigger),
            ("once", {}, DateTrigger),
        ],
    )
    def test_accepted_trigger_types_build_their_apscheduler_trigger(
        self, trigger_type: str, trigger_args: dict, expected: type
    ):
        """「类型字符串被接受」的真含义是能建出对应的触发器，不是字符串在某个表里。

        原先的断言写成 ``assert trigger_type in ["interval", "cron", "date"]``：
        两边都是测试自己的字面量，换掉生产实现也不会红，而且漏掉了服务实际支持的
        ``once``（§5.1 第 1 类自指）。这里让每种类型真的走一遍构造。
        """
        service = SchedulerService()

        built = service._build_trigger(trigger_type, trigger_args)

        assert isinstance(built, expected)

    def test_a_trigger_type_the_service_does_not_know_is_rejected(self):
        """未知类型必须报错而不是悄悄回退——否则注册表里写错一个词就静默不跑。"""
        service = SchedulerService()

        with pytest.raises(ValueError, match="Unknown trigger type: weekly"):
            service._build_trigger("weekly", {})

    def test_cron_without_an_expression_is_rejected(self):
        """``cron`` 缺表达式时上游会退化成「每分钟」，这里要求它报错。"""
        service = SchedulerService()

        with pytest.raises(ValueError, match="Missing cron expression"):
            service._build_trigger("cron", {})

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
