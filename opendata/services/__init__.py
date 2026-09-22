"""Service modules."""

from opendata.data_fetch.providers.akshare_provider import AkshareProvider
from opendata.services.data_acquisition import DataAcquisitionService
from opendata.services.data_service import DataService
from opendata.services.execution_service import ExecutionService
from opendata.services.scheduler import task_scheduler
from opendata.services.scheduler_service import get_scheduler_service, init_scheduler_service
from opendata.services.script_service import ScriptService

__all__ = [
    "AkshareProvider",
    "DataAcquisitionService",
    "DataService",
    "ExecutionService",
    "ScriptService",
    "get_scheduler_service",
    "init_scheduler_service",
    "task_scheduler",
]
