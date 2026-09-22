"""Data domain pipelines and the warehouse schema layer (design §8/§9).

A4 builds the vertical slice here: warehouse DDL generation
(``ddl.py``), the ods/dwd writers, the cross-check and merge
services, and the six-step pipeline orchestration.
"""

from opendata.pipeline.ddl import (
    DWD_TRACE_COLUMNS,
    ODS_METADATA_COLUMNS,
    Column,
    contract_columns,
    dwd_table_ddl,
    ods_table_ddl,
    year_partitions,
)

__all__ = [
    "DWD_TRACE_COLUMNS",
    "ODS_METADATA_COLUMNS",
    "Column",
    "contract_columns",
    "dwd_table_ddl",
    "ods_table_ddl",
    "year_partitions",
]
