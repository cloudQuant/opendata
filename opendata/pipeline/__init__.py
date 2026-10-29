"""Data domain pipelines and the warehouse schema layer (design §8/§9).

A4 builds the vertical slice here: warehouse DDL generation
(``ddl.py``), the ods/dwd writers, the cross-check and merge
services, and the six-step pipeline orchestration.
"""

from typing import TYPE_CHECKING

from opendata.pipeline.ddl import (
    DWD_TRACE_COLUMNS,
    ODS_METADATA_COLUMNS,
    Column,
    contract_columns,
    dwd_table_ddl,
    ods_table_ddl,
    year_partitions,
)

if TYPE_CHECKING:
    from opendata.pipeline.runner import DataPipeline, PipelineOutcome, PipelineSpec, Window

__all__ = [
    "DWD_TRACE_COLUMNS",
    "DataPipeline",
    "PipelineOutcome",
    "PipelineSpec",
    "Window",
    "ODS_METADATA_COLUMNS",
    "Column",
    "contract_columns",
    "dwd_table_ddl",
    "ods_table_ddl",
    "year_partitions",
]

_LAZY_RUNNER_EXPORTS = frozenset({"DataPipeline", "PipelineOutcome", "PipelineSpec", "Window"})


def __getattr__(name: str) -> object:
    """Load a runner export only when a caller asks for it.

    Importing the DDL helpers is safe for schema tooling and does not need to
    initialize the runner's database model dependencies. The orchestration
    types remain available through this package for backwards compatibility.
    """
    if name not in _LAZY_RUNNER_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    if name == "DataPipeline":
        from opendata.pipeline.runner import DataPipeline

        value: object = DataPipeline
    elif name == "PipelineOutcome":
        from opendata.pipeline.runner import PipelineOutcome

        value = PipelineOutcome
    elif name == "PipelineSpec":
        from opendata.pipeline.runner import PipelineSpec

        value = PipelineSpec
    else:
        from opendata.pipeline.runner import Window

        value = Window

    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return package globals and its lazy public exports for introspection."""
    return sorted(set(globals()).union(__all__))
