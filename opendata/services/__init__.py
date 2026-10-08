"""Service modules with compatibility exports resolved on first access."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opendata.data_fetch.providers.akshare_provider import AkshareProvider as AkshareProvider
    from opendata.services.data_acquisition import (
        DataAcquisitionService as DataAcquisitionService,
    )
    from opendata.services.data_service import DataService as DataService
    from opendata.services.execution_service import ExecutionService as ExecutionService
    from opendata.services.scheduler import task_scheduler as task_scheduler
    from opendata.services.scheduler_service import (
        get_scheduler_service as get_scheduler_service,
    )
    from opendata.services.scheduler_service import (
        init_scheduler_service as init_scheduler_service,
    )
    from opendata.services.script_service import ScriptService as ScriptService

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

_EXPORTS = {
    "AkshareProvider": ("opendata.data_fetch.providers.akshare_provider", "AkshareProvider"),
    "DataAcquisitionService": (
        "opendata.services.data_acquisition",
        "DataAcquisitionService",
    ),
    "DataService": ("opendata.services.data_service", "DataService"),
    "ExecutionService": ("opendata.services.execution_service", "ExecutionService"),
    "ScriptService": ("opendata.services.script_service", "ScriptService"),
    "get_scheduler_service": (
        "opendata.services.scheduler_service",
        "get_scheduler_service",
    ),
    "init_scheduler_service": (
        "opendata.services.scheduler_service",
        "init_scheduler_service",
    ),
    "task_scheduler": ("opendata.services.scheduler", "task_scheduler"),
}


def __getattr__(name: str) -> Any:  # noqa: ANN401 - compatibility export is heterogeneous
    """Import and cache one of the original public service objects."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy compatibility exports in interactive discovery."""
    return sorted((*globals(), *__all__))
