"""APScheduler wrapper used by the data module.

Notes:
- We treat `scheduled_tasks` in database as the source of truth.
- APScheduler jobs are kept in-memory and should be re-registered on app startup.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


class SchedulerService:
    """定时任务调度服务"""

    def __init__(self) -> None:
        """初始化调度器"""
        self.scheduler: AsyncIOScheduler | None = None

    def get_scheduler(self) -> AsyncIOScheduler:
        """获取调度器实例（单例模式）"""
        if self.scheduler is None:
            self._initialize_scheduler()
        return self.scheduler

    def _initialize_scheduler(self) -> None:
        """初始化APScheduler"""
        executors = {"default": AsyncIOExecutor()}
        job_defaults = {
            "coalesce": True,  # 合并错过的执行
            "max_instances": 3,  # 最大并发实例数
            "misfire_grace_time": 3600,  # 错过执行宽限时间
        }

        self.scheduler = AsyncIOScheduler(
            executors=executors, job_defaults=job_defaults, timezone="Asia/Shanghai"
        )

    async def start(self) -> None:
        """启动调度器"""
        scheduler = self.get_scheduler()
        if not scheduler.running:
            # 添加事件监听器
            scheduler.add_listener(
                self._job_executed_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
            )
            scheduler.start()
            logger.info(
                "Scheduler started (in-memory job store – jobs re-loaded from DB on each startup)"
            )
            logger.warning(
                "APScheduler is using an in-memory job store. "
                "If the process restarts, all jobs are re-registered from the database. "
                "For persistent job queues across workers, consider migrating to "
                "APScheduler 4.x with a SQLAlchemy/Redis data store, or arq/celery."
            )

    async def shutdown(self, wait: bool = True) -> None:
        """关闭调度器"""
        scheduler = self.get_scheduler()
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=wait)
            logger.info("Scheduler shutdown")

    async def add_job(
        self,
        job_id: str,
        func: Callable,
        trigger_type: str,
        trigger_args: dict,
        job_name: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> dict | None:
        """
        添加定时任务

        Args:
            job_id: 任务唯一标识
            func: 执行函数
            trigger_type: 触发器类型 (cron/interval/date)
            trigger_args: 触发器参数
            job_name: 任务名称（可选）
            **kwargs: 其他参数

        Returns:
            任务信息字典
        """
        scheduler = self.get_scheduler()
        if scheduler is None:
            raise RuntimeError("Scheduler not initialized")

        # 构建触发器
        trigger = self._build_trigger(trigger_type, trigger_args)

        # 添加任务
        job = scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            name=job_name,
            replace_existing=True,
            **kwargs,
        )

        return {
            "job_id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time,
            "trigger": str(job.trigger),
        }

    async def remove_job(self, job_id: str) -> bool:
        """移除任务"""
        scheduler = self.get_scheduler()
        if scheduler and scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            return True
        return False

    async def pause_job(self, job_id: str) -> bool:
        """暂停任务"""
        scheduler = self.get_scheduler()
        if scheduler and scheduler.get_job(job_id):
            scheduler.pause_job(job_id)
            return True
        return False

    async def resume_job(self, job_id: str) -> bool:
        """恢复任务"""
        scheduler = self.get_scheduler()
        if scheduler and scheduler.get_job(job_id):
            scheduler.resume_job(job_id)
            return True
        return False

    async def run_job_now(self, job_id: str) -> bool:
        """立即执行任务"""
        scheduler = self.get_scheduler()
        job = scheduler.get_job(job_id) if scheduler else None
        if job:
            job.modify(next_run_time=datetime.now())
            return True
        return False

    def get_jobs(self, jobstore: str | None = None) -> list[dict]:
        """获取任务列表（来自 APScheduler 内存调度表）"""
        scheduler = self.get_scheduler()
        if scheduler is None:
            return []

        # APScheduler's `get_jobs()` optionally accepts a jobstore alias.
        jobs = scheduler.get_jobs(jobstore)
        return [self._job_to_dict(job) for job in jobs]

    def get_job(self, job_id: str) -> Any:
        """获取单个任务"""
        scheduler = self.get_scheduler()
        if scheduler:
            return scheduler.get_job(job_id)
        return None

    def _build_trigger(self, trigger_type: str, trigger_args: dict[str, Any]) -> Any:  # noqa: ANN401
        """构建触发器"""
        if trigger_type == "cron":
            cron_expression = trigger_args.get("cron_expression") or trigger_args.get("cron")
            if not cron_expression:
                raise ValueError("Missing cron expression in trigger_args (cron_expression/cron)")
            timezone_str = trigger_args.get("timezone", "Asia/Shanghai")
            return CronTrigger.from_crontab(cron_expression, timezone=timezone_str)
        if trigger_type == "interval":
            return IntervalTrigger(
                **{
                    k: v
                    for k, v in trigger_args.items()
                    if v is not None and k in ["weeks", "days", "hours", "minutes", "seconds"]
                }
            )
        if trigger_type == "date":
            run_date = trigger_args.get("run_date")
            if isinstance(run_date, str):
                run_date = datetime.fromisoformat(run_date)
            return DateTrigger(run_date=run_date)
        if trigger_type == "once":
            return DateTrigger(run_date=datetime.now())
        raise ValueError(f"Unknown trigger type: {trigger_type}")

    def _job_to_dict(self, job) -> dict:
        """任务对象转字典"""
        return {
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time,
            "trigger": str(job.trigger),
            "executor": job.executor,
        }

    def _job_executed_listener(self, event: Any) -> None:  # noqa: ANN401
        """任务执行监听器"""
        if event.exception:
            logger.error(f"Job {event.job_id} failed: {event.exception}")
            # 这里可以添加告警逻辑
        else:
            logger.info(f"Job {event.job_id} executed successfully")


# 全局调度器实例
_scheduler_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService | None:
    """获取调度器服务实例"""
    return _scheduler_service


def init_scheduler_service() -> SchedulerService:
    """初始化调度器服务"""
    global _scheduler_service
    _scheduler_service = SchedulerService()
    return _scheduler_service
