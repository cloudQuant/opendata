"""Aggregate the ported AFI data-management routers under one namespace.

Mounted at ``/api/v1/afi-data`` so the ported plane is fully reachable in
OpenData without changing any existing OpenData contract. Each sub-router
keeps its AFI path suffix (``/scripts``, ``/tasks`` ...), and the governance
router carries its own ``/data-governance`` prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from opendata.afi_port.api.data_governance import router as data_governance_router
from opendata.afi_port.api.executions import router as executions_router
from opendata.afi_port.api.interfaces import router as interfaces_router
from opendata.afi_port.api.scripts import router as scripts_router
from opendata.afi_port.api.tables import router as tables_router
from opendata.afi_port.api.tasks import router as tasks_router

afi_data_router = APIRouter()
afi_data_router.include_router(scripts_router, tags=["AFI Data Scripts"])
afi_data_router.include_router(tasks_router, tags=["AFI Data Tasks"])
afi_data_router.include_router(executions_router, tags=["AFI Data Executions"])
afi_data_router.include_router(tables_router, tags=["AFI Data Tables"])
afi_data_router.include_router(interfaces_router, tags=["AFI Data Interfaces"])
afi_data_router.include_router(data_governance_router, tags=["AFI Data Governance"])

__all__ = ["afi_data_router"]
