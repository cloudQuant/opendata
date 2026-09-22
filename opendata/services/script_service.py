"""Script service for managing data acquisition scripts"""

import asyncio
import importlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.data_fetch.configs import SCRIPT_CATEGORIES
from opendata.models.data_script import DataScript, ScriptFrequency


class ScriptService:
    """Data acquisition script management service."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.script_base_path = Path("app/data_fetch/scripts")

    async def get_scripts(
        self,
        category: str | None = None,
        frequency: ScriptFrequency | None = None,
        is_active: bool | None = None,
        keyword: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[DataScript], int]:
        """
        Get list of scripts with optional filters.

        Args:
            category: Filter by category
            frequency: Filter by frequency
            is_active: Filter by active status
            keyword: Search in name, id, description
            skip: Number of records to skip
            limit: Maximum records to return

        Returns:
            Tuple of (script list, total count)
        """
        query = select(DataScript)

        # 筛选条件
        if category:
            query = query.where(DataScript.category == category)
        if frequency:
            query = query.where(DataScript.frequency == frequency)
        if is_active is not None:
            query = query.where(DataScript.is_active == is_active)
        if keyword:
            filters = [
                DataScript.script_name.contains(keyword),
                DataScript.script_id.contains(keyword),
                DataScript.description.contains(keyword),
            ]
            keyword_filter = filters[0]
            for f in filters[1:]:
                keyword_filter = keyword_filter | f
            query = query.where(keyword_filter)

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.offset(skip).limit(limit).order_by(DataScript.category, DataScript.script_id)
        result = await self.db.execute(query)
        scripts = result.scalars().all()

        return list(scripts), total

    async def get_script(self, script_id: str) -> DataScript | None:
        """Get script details by ID."""
        result = await self.db.execute(select(DataScript).where(DataScript.script_id == script_id))
        return result.scalar_one_or_none()

    async def create_script(
        self,
        script_id: str,
        script_name: str,
        category: str,
        module_path: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> DataScript:
        """Create a new script."""
        script = DataScript(
            script_id=script_id,
            script_name=script_name,
            category=category,
            module_path=module_path,
            **kwargs,
        )
        self.db.add(script)
        await self.db.commit()
        await self.db.refresh(script)
        return script

    async def update_script(self, script_id: str, **kwargs: Any) -> DataScript | None:  # noqa: ANN401
        """Update an existing script."""
        script = await self.get_script(script_id)
        if not script:
            return None

        for key, value in kwargs.items():
            if hasattr(script, key):
                setattr(script, key, value)

        script.updated_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(script)
        return script

    async def delete_script(self, script_id: str) -> bool:
        """Delete a script by ID."""
        script = await self.get_script(script_id)
        if not script:
            return False

        await self.db.delete(script)
        await self.db.commit()
        return True

    async def toggle_script(self, script_id: str, active: bool | None = None) -> bool:
        """
        Toggle script active status.

        Args:
            script_id: Script ID
            active: Target state; if None, toggles current state

        Returns:
            True if toggle succeeded
        """
        script = await self.get_script(script_id)
        if not script:
            return False

        if active is None:
            script.is_active = not script.is_active
        else:
            script.is_active = active

        await self.db.commit()
        return True

    def _resolve_entrypoint(self, module: Any, func_name: str) -> Any:  # noqa: ANN401
        """Resolve callable entrypoint from module (function or class instance)."""
        entrypoint = getattr(module, func_name, None)

        if not callable(entrypoint):
            for obj in module.__dict__.values():
                if (
                    isinstance(obj, type)
                    and obj.__module__ == module.__name__
                    and (hasattr(obj, "fetch_data") or hasattr(obj, "run"))
                ):
                    entrypoint = obj()
                    func_name = "fetch_data" if hasattr(entrypoint, "fetch_data") else "run"
                    break

            if not entrypoint or not callable(getattr(entrypoint, func_name, None)):
                raise AttributeError(f"No callable entrypoint found for module={module.__name__}")

        func = getattr(entrypoint, func_name, None) if not callable(entrypoint) else entrypoint
        if func is None:
            raise ValueError(f"Script function '{func_name}' not found")
        return func

    async def execute_script(
        self,
        script_id: str,
        execution_id: str,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Execute a script.

        Args:
            script_id: Script ID
            execution_id: Execution ID for tracking
            params: Execution parameters
            timeout: Timeout in seconds

        Returns:
            Execution result dict with success, result, error
        """
        script = await self.get_script(script_id)
        if not script:
            raise ValueError(f"Script not found: {script_id}")

        if not script.is_active:
            raise ValueError(f"Script is not active: {script_id}")

        if not script.module_path:
            raise ValueError(f"Script has no module_path: {script_id}")

        try:
            module = importlib.import_module(script.module_path)
            func_name = script.function_name or "main"
            func = self._resolve_entrypoint(module, func_name)

            if asyncio.iscoroutinefunction(func):
                coro = func(**(params or {}))
            else:
                coro = asyncio.to_thread(func, **(params or {}))

            if timeout and timeout > 0:
                try:
                    result = await asyncio.wait_for(coro, timeout=timeout)
                except TimeoutError as e:
                    raise TimeoutError(
                        f"Script {script_id} execution timed out after {timeout}s"
                    ) from e
            else:
                result = await coro

            return {
                "success": True,
                "script_id": script_id,
                "execution_id": execution_id,
                "result": self._serialize_result(result),
                "error": None,
            }

        except Exception as e:
            logger.error(f"Script execution failed: {script_id}, error: {e!s}")
            return {
                "success": False,
                "script_id": script_id,
                "execution_id": execution_id,
                "result": None,
                "error": str(e),
            }

    def _serialize_result(self, result: Any) -> Any:  # noqa: ANN401
        """Serialize execution result for JSON response."""
        import pandas as pd

        if isinstance(result, pd.DataFrame):
            return {
                "type": "DataFrame",
                "rows": len(result),
                "columns": list(result.columns),
                "data": result.to_dict(orient="records")[:100],  # Limit to first 100 rows
            }
        if hasattr(result, "__dict__"):
            return str(result)
        return result

    async def get_categories(self) -> list[str]:
        """Get all script category names."""
        result = await self.db.execute(select(DataScript.category).distinct())
        return [row[0] for row in result.all()]

    async def scan_and_register_scripts(self) -> dict[str, Any]:
        """
        Scan filesystem and register all scripts.

        Returns:
            Dict with registered_count, updated_count, errors
        """
        registered_count = 0
        updated_count = 0
        errors = []

        script_base_path = Path("opendata") / "data_fetch" / "scripts"

        for category, config in SCRIPT_CATEGORIES.items():
            category_path = script_base_path / category
            if not await asyncio.to_thread(category_path.exists):
                continue

            for sub_category in config.get("sub_categories", []):
                sub_path = category_path / sub_category
                if not await asyncio.to_thread(sub_path.exists):
                    continue

                entries = await asyncio.to_thread(list, sub_path.iterdir())
                for entry in entries:
                    if not entry.name.endswith(".py") or entry.name.startswith("__"):
                        continue

                    try:
                        result = await self._register_script_from_file(
                            str(entry), str(script_base_path)
                        )
                        if result == "created":
                            registered_count += 1
                        elif result == "updated":
                            updated_count += 1
                    except Exception as e:
                        errors.append(f"{entry.name}: {e!s}")

        return {
            "registered_count": registered_count,
            "updated_count": updated_count,
            "errors": errors,
        }

    async def _register_script_from_file(self, file_path: str, base_path: str) -> str:
        """
        Register a script from a file.

        Returns:
            'created', 'updated', or 'skipped'
        """
        p = Path(file_path)
        base = Path(base_path)
        rel_path = p.relative_to(base)
        module_path = "opendata.data_fetch.scripts." + str(rel_path).replace(".py", "").replace(
            "/", "."
        ).replace("\\", ".")

        # 解析脚本信息
        rel_parts = rel_path.parts
        category = rel_parts[0] if len(rel_parts) >= 1 else "common"
        sub_category = rel_parts[1] if len(rel_parts) >= 2 else None

        # 判断频率
        parent_dir = p.parent.name
        frequency_map = {
            "hourly": ScriptFrequency.HOURLY,
            "daily": ScriptFrequency.DAILY,
            "weekly": ScriptFrequency.WEEKLY,
            "monthly": ScriptFrequency.MONTHLY,
        }
        frequency = frequency_map.get(parent_dir, ScriptFrequency.DAILY)

        script_id = p.stem
        target_table = self._extract_target_table(file_path)

        # 检查是否已存在
        existing = await self.get_script(script_id)

        if existing:
            # 检查是否需要更新
            needs_update = any(
                [
                    existing.module_path != module_path,
                    existing.category != category,
                    not existing.target_table and bool(target_table),
                ]
            )
            if needs_update:
                await self.update_script(
                    script_id,
                    module_path=module_path,
                    category=category,
                    sub_category=sub_category,
                    frequency=frequency,
                    target_table=target_table,
                )
                return "updated"
            return "skipped"

        # 创建新脚本
        await self.create_script(
            script_id=script_id,
            script_name=script_id.replace("_", " ").title(),
            category=category,
            sub_category=sub_category,
            frequency=frequency,
            module_path=module_path,
            target_table=target_table,
            is_active=True,
        )
        return "created"

    def _extract_target_table(self, file_path: str) -> str | None:
        """Extract target table name from script file."""
        try:
            with Path(file_path).open(encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            logger.debug("Could not read script file %s: %s", file_path, e)
            return None

        m = re.search(r"""self\.table_name\s*=\s*["']([A-Za-z0-9_]+)["']""", content)
        if m:
            return m.group(1)
        return None

    async def get_script_stats(self) -> dict[str, Any]:
        """Get script statistics (total, active, by category, by frequency)."""
        total_query = select(func.count(DataScript.id))
        active_query = select(func.count(DataScript.id)).where(DataScript.is_active.is_(True))

        total_result = await self.db.execute(total_query)
        active_result = await self.db.execute(active_query)

        total = total_result.scalar() or 0
        active = active_result.scalar() or 0

        # 按分类统计
        category_query = select(DataScript.category, func.count(DataScript.id)).group_by(
            DataScript.category
        )
        category_result = await self.db.execute(category_query)
        by_category = {row[0]: row[1] for row in category_result.all()}

        # 按频率统计
        frequency_query = select(DataScript.frequency, func.count(DataScript.id)).group_by(
            DataScript.frequency
        )
        frequency_result = await self.db.execute(frequency_query)
        by_frequency = {row[0].value: row[1] for row in frequency_result.all()}

        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "by_category": by_category,
            "by_frequency": by_frequency,
        }
