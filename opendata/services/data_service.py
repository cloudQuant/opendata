"""
Data service for executing data acquisition scripts.

Provides a unified interface for script execution used by retry and scheduler services.
"""

import asyncio
import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.services.script_service import ScriptService


class DataService:
    """
    Service for executing data scripts with timeout support.

    Wraps ScriptService to provide a simplified interface for
    retry and scheduler services.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._script_service = ScriptService(db)

    async def execute_script(
        self,
        script_id: str,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Execute a data script with optional timeout.

        Args:
            script_id: Script identifier
            params: Execution parameters
            timeout: Timeout in seconds (None = no timeout)

        Returns:
            Execution result dict with keys: success, script_id, result, error,
            rows_processed
        """
        execution_id = str(uuid.uuid4())

        logger.info(
            f"DataService executing script {script_id} "
            f"(timeout={timeout}s, execution_id={execution_id})"
        )

        try:
            if timeout and timeout > 0:
                result = await asyncio.wait_for(
                    self._script_service.execute_script(
                        script_id=script_id,
                        execution_id=execution_id,
                        params=params,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
            else:
                result = await self._script_service.execute_script(
                    script_id=script_id,
                    execution_id=execution_id,
                    params=params,
                    timeout=timeout,
                )

            if result.get("success"):
                logger.info(f"Script {script_id} executed successfully")
            else:
                logger.warning(f"Script {script_id} execution failed: {result.get('error')}")

            return result

        except TimeoutError:
            error_msg = f"Script {script_id} timed out after {timeout}s"
            logger.error(error_msg)
            return {
                "success": False,
                "script_id": script_id,
                "execution_id": execution_id,
                "result": None,
                "error": error_msg,
                "rows_processed": 0,
            }

        except Exception as e:
            error_msg = f"Script {script_id} execution error: {e}"
            logger.error(error_msg)
            return {
                "success": False,
                "script_id": script_id,
                "execution_id": execution_id,
                "result": None,
                "error": str(e),
                "rows_processed": 0,
            }
