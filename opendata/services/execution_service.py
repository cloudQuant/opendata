"""Execution service for monitoring task executions"""

import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.task import TaskExecution, TaskStatus, TriggeredBy
from opendata.utils.db_result import get_rowcount


class ExecutionService:
    """执行监控服务"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_execution_id(self, execution_id: str) -> TaskExecution | None:
        """根据执行ID获取执行记录"""
        result = await self.db.execute(
            select(TaskExecution).where(TaskExecution.execution_id == execution_id)
        )
        return result.scalar_one_or_none()

    # Alias for backward compatibility
    async def get_execution(self, execution_id: str) -> TaskExecution | None:
        """根据执行ID获取执行记录（别名方法）"""
        return await self.get_by_execution_id(execution_id)

    async def create_execution(
        self,
        task_id: int,
        script_id: str,
        params: dict | None = None,
        triggered_by: TriggeredBy = TriggeredBy.SCHEDULER,
        operator_id: int | None = None,
    ) -> TaskExecution:
        """
        创建执行记录

        Args:
            task_id: 任务ID
            script_id: 脚本ID
            params: 执行参数
            triggered_by: 触发方式
            operator_id: 操作人ID

        Returns:
            执行记录对象
        """
        execution_id = f"exec_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        execution = TaskExecution(
            execution_id=execution_id,
            task_id=task_id,
            script_id=script_id,
            params=params,
            status=TaskStatus.PENDING,
            triggered_by=triggered_by,
            operator_id=operator_id,
        )
        self.db.add(execution)
        await self.db.commit()
        await self.db.refresh(execution)
        return execution

    def _apply_execution_updates(
        self,
        execution: TaskExecution,
        status: TaskStatus | None,
        start_time: datetime | None,
        end_time: datetime | None,
        result: dict | None,
        error_message: str | None,
        error_trace: str | None,
        rows_before: int | None,
        rows_after: int | None,
    ) -> None:
        """Apply update fields to execution model."""
        if status:
            execution.status = status
        if start_time:
            execution.start_time = start_time
        if end_time:
            execution.end_time = end_time
            effective_start = start_time or execution.start_time
            if effective_start:
                execution.duration = (end_time - effective_start).total_seconds()
        if result is not None:
            execution.result = result
        if error_message:
            execution.error_message = error_message
        if error_trace:
            execution.error_trace = error_trace
        if rows_before is not None:
            execution.rows_before = rows_before
        if rows_after is not None:
            execution.rows_after = rows_after

    async def update_execution(
        self,
        execution_id: str,
        status: TaskStatus | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        result: dict | None = None,
        error_message: str | None = None,
        error_trace: str | None = None,
        rows_before: int | None = None,
        rows_after: int | None = None,
    ) -> bool:
        """
        更新执行记录

        Args:
            execution_id: 执行ID
            status: 执行状态
            start_time: 开始时间
            end_time: 结束时间
            result: 执行结果
            error_message: 错误信息
            error_trace: 错误堆栈
            rows_before: 执行前行数
            rows_after: 执行后行数

        Returns:
            是否更新成功
        """
        execution = await self.get_by_execution_id(execution_id)
        if not execution:
            logger.warning(f"Execution not found for update: {execution_id}")
            return False

        self._apply_execution_updates(
            execution,
            status,
            start_time,
            end_time,
            result,
            error_message,
            error_trace,
            rows_before,
            rows_after,
        )

        try:
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to commit execution update for {execution_id}: {e}")
            await self.db.rollback()
            return False

    async def get_executions(
        self,
        task_id: int | None = None,
        script_id: str | None = None,
        status: TaskStatus | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[TaskExecution], int]:
        """
        获取执行记录列表

        Args:
            task_id: 任务ID筛选
            script_id: 脚本ID筛选
            status: 状态筛选
            start_date: 开始日期
            end_date: 结束日期
            skip: 跳过数量
            limit: 返回数量

        Returns:
            (执行记录列表, 总数)
        """
        query = select(TaskExecution)

        # 筛选条件
        if task_id:
            query = query.where(TaskExecution.task_id == task_id)
        if script_id:
            query = query.where(TaskExecution.script_id == script_id)
        if status:
            query = query.where(TaskExecution.status == status)
        if start_date:
            query = query.where(TaskExecution.start_time >= start_date)
        if end_date:
            query = query.where(TaskExecution.start_time <= end_date)

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.order_by(TaskExecution.start_time.desc()).offset(skip).limit(limit)
        result = await self.db.execute(query)
        executions = result.scalars().all()

        return list(executions), total

    async def get_execution_stats(
        self, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> dict:
        """
        获取执行统计

        Args:
            start_date: 统计开始日期
            end_date: 统计结束日期

        Returns:
            统计信息字典
        """
        if not start_date:
            start_date = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            end_date = datetime.now(UTC)

        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        # Single query for all stats to reduce database round-trips
        stats_query = select(
            func.count(TaskExecution.id).label("total_count"),
            func.count(case((TaskExecution.status == TaskStatus.COMPLETED, 1))).label(
                "success_count"
            ),
            func.avg(
                case(
                    (TaskExecution.status == TaskStatus.COMPLETED, TaskExecution.duration),
                )
            ).label("avg_duration"),
            func.count(case((TaskExecution.start_time >= today_start, 1))).label("today_count"),
        ).where(
            and_(
                TaskExecution.start_time >= start_date,
                TaskExecution.start_time <= end_date,
            )
        )

        result = await self.db.execute(stats_query)
        row = result.one()

        total_count = row.total_count or 0
        success_count = row.success_count or 0
        avg_duration = row.avg_duration or 0
        today_count = row.today_count or 0
        failed_count = total_count - success_count

        return {
            "total_count": total_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": ((success_count / total_count * 100) if total_count > 0 else 0),
            "avg_duration": float(avg_duration) if avg_duration else 0,
            "today_executions": today_count,
        }

    async def get_recent_executions(self, limit: int = 50) -> list[TaskExecution]:
        """获取最近的执行记录"""
        query = select(TaskExecution).order_by(TaskExecution.start_time.desc()).limit(limit)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_running_executions(self) -> list[TaskExecution]:
        """获取正在执行的任务"""
        query = (
            select(TaskExecution)
            .where(TaskExecution.status == TaskStatus.RUNNING)
            .order_by(TaskExecution.start_time.desc())
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def delete_executions_by_ids(self, execution_ids: list[str]) -> int:
        """按ID批量删除执行记录"""
        if not execution_ids:
            return 0
        result = await self.db.execute(
            delete(TaskExecution).where(TaskExecution.execution_id.in_(execution_ids))
        )
        await self.db.commit()
        return get_rowcount(result)

    async def delete_executions_by_status(self, status: TaskStatus) -> int:
        """按状态删除执行记录"""
        result = await self.db.execute(delete(TaskExecution).where(TaskExecution.status == status))
        await self.db.commit()
        return get_rowcount(result)

    async def get_failed_executions(self, limit: int = 20) -> list[TaskExecution]:
        """获取失败的执行记录"""
        query = (
            select(TaskExecution)
            .where(TaskExecution.status == TaskStatus.FAILED)
            .order_by(TaskExecution.start_time.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def handle_execution_complete(
        self,
        execution_id: str,
        status: TaskStatus,
    ) -> bool:
        """
        Handle execution completion callback.

        Note: Retry logic is handled exclusively by TaskScheduler._execute_with_retry().
        This method is kept for status logging / future hooks (e.g. notifications),
        but does NOT trigger retries to avoid double-retry issues.

        Args:
            execution_id: Execution ID
            status: Final execution status

        Returns:
            True if handled successfully
        """
        if status == TaskStatus.FAILED:
            logger.info(f"Execution {execution_id} failed (retry handled by scheduler)")
        return True
