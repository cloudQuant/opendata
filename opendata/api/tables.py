"""
Data table API routes.

Provides endpoints for managing data tables created by data acquisition.
"""

import csv
import io
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import get_current_admin_user, get_current_user, get_db
from opendata.api.schemas import (
    APIResponse,
    PaginatedParams,
    TableResponse,
    TableSchemaResponse,
)
from opendata.core.database import get_data_db
from opendata.models.data_table import DataTable
from opendata.utils.constants import CSV_EXPORT_BATCH_SIZE, XLSX_EXPORT_ROW_LIMIT
from opendata.utils.db_result import get_columns_from_result
from opendata.utils.helpers import safe_table_name
from opendata.utils.serialization import serialize_for_csv, serialize_for_json

router = APIRouter()


def _stream_xlsx_export(
    safe_filename: str,
    columns: list[str],
    rows: list[tuple],
) -> StreamingResponse:
    """Build StreamingResponse for Excel export."""
    import pandas as pd

    df = pd.DataFrame(rows, columns=columns)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}.xlsx"'},
    )


async def _csv_export_stream(
    data_db: AsyncSession,
    safe_name: str,
    limit: int,
) -> AsyncIterator[str]:
    """Async generator for CSV export in batches."""
    offset = 0
    header_written = False
    while offset < limit:
        batch_limit = min(CSV_EXPORT_BATCH_SIZE, limit - offset)
        query = text(f"SELECT * FROM {safe_name} LIMIT :limit OFFSET :offset")
        data_result = await data_db.execute(query, {"limit": batch_limit, "offset": offset})
        columns = get_columns_from_result(data_result)
        rows = data_result.fetchall()

        if not rows:
            if not header_written and columns:
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(columns)
                yield buf.getvalue()
            break

        buf = io.StringIO()
        writer = csv.writer(buf)
        if not header_written:
            writer.writerow(columns)
            header_written = True
        for row in rows:
            writer.writerow([serialize_for_csv(v) for v in row])
        yield buf.getvalue()

        offset += len(rows)
        if len(rows) < batch_limit:
            break


def _get_safe_table_name(table_name: str) -> str:
    """
    Get safe table name for SQL, raising HTTP 400 on invalid input.

    Args:
        table_name: Raw table name from metadata

    Returns:
        Backtick-quoted safe table name

    Raises:
        HTTPException: 400 if table name is invalid (e.g. SQL injection attempt)
    """
    try:
        return safe_table_name(table_name)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid table name: {e!s}",
        ) from e


@router.get(
    "/",
)
async def list_tables(
    search: str | None = Query(
        None, description="Search in table name, display name, or description"
    ),
    params: PaginatedParams = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> APIResponse:
    """
    List data tables.

    Returns paginated list of data tables with metadata.
    """
    # Build query
    query = select(DataTable)

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            (DataTable.table_name.ilike(search_pattern))
            | (DataTable.table_comment.ilike(search_pattern))
        )

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = (
        query.order_by(DataTable.updated_at.desc())
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
    )

    result = await db.execute(query)
    tables = result.scalars().all()

    items = [TableResponse.model_validate(table) for table in tables]

    return APIResponse(
        success=True,
        message="success",
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "total": total,
            "page": params.page,
            "page_size": params.page_size,
            "total_pages": (total + params.page_size - 1) // params.page_size,
        },
    )


@router.get(
    "/{table_id}",
)
async def get_table(
    table_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> APIResponse:
    """
    Get data table details.

    Returns detailed information about a specific data table.
    """
    result = await db.execute(select(DataTable).where(DataTable.id == table_id))
    table = result.scalar_one_or_none()

    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data table not found",
        )

    return APIResponse(
        success=True,
        message="success",
        data=TableResponse.model_validate(table).model_dump(mode="json"),
    )


@router.get(
    "/{table_id}/schema",
)
async def get_table_schema(
    table_id: int,
    db: AsyncSession = Depends(get_db),
    data_db: AsyncSession = Depends(get_data_db),
    current_user=Depends(get_current_user),
) -> TableSchemaResponse:
    """
    Get data table schema.

    Returns the database schema for a specific table including
    column names, types, and constraints.
    """
    result = await db.execute(select(DataTable).where(DataTable.id == table_id))
    table = result.scalar_one_or_none()

    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data table not found",
        )

    # Get actual schema from data warehouse
    safe_name = _get_safe_table_name(table.table_name)
    try:
        query = text(f"DESCRIBE {safe_name}")
        schema_result = await data_db.execute(query)
        columns = [
            {
                "name": row[0],
                "type": row[1],
                "nullable": row[2] == "YES",
                "key": row[3] if row[3] else None,
                "default": row[4],
            }
            for row in schema_result.fetchall()
        ]
    except SQLAlchemyError:
        # If table doesn't exist or query fails, return empty
        columns = []

    return TableSchemaResponse(
        table_name=table.table_name,
        columns=columns,
        row_count=table.row_count,
        last_update_time=table.last_update_time,
    )


@router.get("/{table_id}/data")
async def get_table_data(
    table_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    data_db: AsyncSession = Depends(get_data_db),
    current_user=Depends(get_current_user),
) -> APIResponse:
    """
    Get data from table.

    Returns actual data rows from the specified table.
    """
    result = await db.execute(select(DataTable).where(DataTable.id == table_id))
    table = result.scalar_one_or_none()

    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data table not found",
        )

    offset = (page - 1) * page_size
    limit = page_size

    # Query actual data from data warehouse
    safe_name = _get_safe_table_name(table.table_name)
    try:
        query = text(f"SELECT * FROM {safe_name} ORDER BY id LIMIT :limit OFFSET :offset")
        data_result = await data_db.execute(query, {"limit": limit, "offset": offset})

        # Get column names from cursor description
        columns = get_columns_from_result(data_result)
        rows = data_result.fetchall()

        serialized_rows = [
            {col: serialize_for_json(row[i]) for i, col in enumerate(columns)} for row in rows
        ]

        return APIResponse(
            success=True,
            message="success",
            data={
                "table_name": table.table_name,
                "columns": columns,
                "rows": serialized_rows,
                "row_count": len(rows),
                "offset": offset,
                "limit": limit,
            },
        )
    except SQLAlchemyError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query table data: {e!s}",
        ) from e


@router.delete(
    "/{table_id}",
)
async def delete_table(
    table_id: int,
    db: AsyncSession = Depends(get_db),
    data_db: AsyncSession = Depends(get_data_db),
    current_user=Depends(get_current_admin_user),
) -> APIResponse:
    """
    Delete data table.

    Admin only endpoint for dropping tables and metadata.
    """
    result = await db.execute(select(DataTable).where(DataTable.id == table_id))
    table = result.scalar_one_or_none()

    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data table not found",
        )

    # Drop the actual table from data warehouse
    safe_name = _get_safe_table_name(table.table_name)
    try:
        query = text(f"DROP TABLE IF EXISTS {safe_name}")
        await data_db.execute(query)
        await data_db.commit()
    except SQLAlchemyError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to drop table: {e!s}",
        ) from e

    # Delete metadata
    await db.delete(table)
    await db.commit()

    return APIResponse(
        success=True,
        message=f"Table {table.table_name} deleted successfully",
    )


@router.get("/{table_id}/export")
async def export_table_data(
    table_id: int,
    format: str = Query("csv", pattern="^(csv|xlsx)$", description="Export format: csv or xlsx"),
    limit: int = Query(100000, ge=1, le=1000000, description="Maximum rows to export"),
    db: AsyncSession = Depends(get_db),
    data_db: AsyncSession = Depends(get_data_db),
    current_user=Depends(get_current_user),
) -> StreamingResponse:
    """
    Export table data as CSV or Excel file.

    CSV uses streaming to keep memory bounded for large tables.
    Excel is limited to 50,000 rows due to in-memory requirement.
    """
    result = await db.execute(select(DataTable).where(DataTable.id == table_id))
    table = result.scalar_one_or_none()

    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data table not found",
        )

    safe_name = _get_safe_table_name(table.table_name)
    safe_filename = re.sub(r"[^\w]", "_", table.table_name)
    xlsx_limit = min(limit, XLSX_EXPORT_ROW_LIMIT)

    if format == "xlsx":
        try:
            query = text(f"SELECT * FROM {safe_name} LIMIT :limit")
            data_result = await data_db.execute(query, {"limit": xlsx_limit})
            columns = get_columns_from_result(data_result)
            rows = data_result.fetchall()
        except SQLAlchemyError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to query table data: {e!s}",
            ) from e
        return _stream_xlsx_export(safe_filename, columns, rows)

    return StreamingResponse(
        _csv_export_stream(data_db, safe_name, limit),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}.csv"'},
    )


@router.post(
    "/refresh",
)
async def refresh_table_metadata(
    db: AsyncSession = Depends(get_db),
    data_db: AsyncSession = Depends(get_data_db),
    current_user=Depends(get_current_admin_user),
) -> APIResponse:
    """
    Refresh table metadata.

    Admin only endpoint to update row counts and schema info
    from actual database tables.
    """
    # Get all tables
    result = await db.execute(select(DataTable))
    tables = result.scalars().all()

    updated_count = 0

    for table in tables:
        try:
            safe_name = _get_safe_table_name(table.table_name)
            count_query = text(f"SELECT COUNT(*) FROM {safe_name}")
            count_result = await data_db.execute(count_query)
            table.row_count = count_result.scalar() or 0

            updated_count += 1
        except SQLAlchemyError as e:
            logger.debug("Table %s not in warehouse, skipping: %s", table.table_name, e)

    await db.commit()

    return APIResponse(
        success=True,
        message=f"Refreshed metadata for {updated_count} tables",
    )
