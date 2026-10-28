"""Authenticated, bounded REST reads from the financial observation archive."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from contextlib import suppress
from datetime import date, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from opendata.api.dependencies import CurrentPrincipal, require_domain_access
from opendata.api.published_data import (
    _ERROR_RESPONSES,
    _ErrorEnvelopeRoute,
    _reject_unknown_query,
    _success_response,
)
from opendata.core.config import settings
from opendata.data.financial_observation_archive_reader import (
    FinancialObservationArchivePage,
    FinancialObservationArchiveReadError,
    FinancialObservationArchiveRow,
    read_financial_observation_page,
)

router = APIRouter(route_class=_ErrorEnvelopeRoute)

_MAX_PAGE_SIZE = 199
_MAX_REPORT_PERIOD_SPAN_DAYS = 3_660
# The reader fetches page_size + 1 to detect continuation, so 199 keeps the
# internal SELECT at or below the 200-row single-page ceiling.
_MAX_RESPONSE_BYTES = 256 * 1024
_SOURCE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_SOURCE_ID_FULL_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}", re.ASCII)
_INSTRUMENT_ID_PATTERN = r"^[0-9]{6}\.(?:SH|SZ|BJ)$"
# Starlette renamed 422 to "Unprocessable Content". Keep compatibility with
# older FastAPI-supported Starlette releases without touching the deprecated alias.
_HTTP_422_STATUS = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)
# Starlette renamed 413 to "Content Too Large"; 413 is stable across both names.
_HTTP_413_STATUS = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)


class FinancialArchiveScopeResponse(BaseModel):
    """One explicit, single-security archive query scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: StrictStr
    instrument_id: StrictStr
    statement_type: Literal["income", "balance", "cashflow"]
    periodicity: Literal["annual", "quarterly"]
    start_report_period: StrictStr
    end_report_period: StrictStr


class FinancialArchiveRowResponse(BaseModel):
    """Allowlisted source-native observation fields; decimal values stay exact text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: StrictStr
    instrument_id: StrictStr
    statement_type: Literal["income", "balance", "cashflow"]
    periodicity: Literal["annual", "quarterly"]
    report_period: StrictStr
    source_disclosure_date: StrictStr
    observed_at_utc: StrictStr
    item_code: StrictStr
    value_decimal_text: StrictStr | None
    native_unit: StrictStr


class FinancialArchiveResponse(BaseModel):
    """Bounded archive read with explicitly unknown coverage and PIT semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: FinancialArchiveScopeResponse
    records: list[FinancialArchiveRowResponse]
    completeness: Literal["unknown"]
    point_in_time_reconstruction: Literal["unavailable"]
    period_amount_semantics: Literal["unknown"]


class FinancialArchiveUnavailableError(RuntimeError):
    """Raised when the separately configured archive cannot be read."""


class FinancialArchivePageTooLargeError(RuntimeError):
    """Raised when a complete page cannot fit the endpoint's hard limits."""


def _archive_engine_from_settings() -> Engine:
    """Create an uncached engine from the explicit archive URL only.

    The URL is never logged or included in exception text. A fresh ``NullPool``
    engine makes each request own a single short-lived database connection.
    URL comparison rejects known literal endpoint/schema collisions but does not
    resolve DNS aliases or prove database isolation. The DBA must verify the
    archive target and grant its account SELECT only; this API does not run
    schema migrations or write statements.
    """
    configured_url = settings.financial_archive_database_url
    raw_url = configured_url.get_secret_value() if configured_url is not None else None
    if not raw_url or raw_url != raw_url.strip():
        raise FinancialArchiveUnavailableError("financial archive URL is not configured")
    try:
        url: URL = make_url(raw_url)
    except (SQLAlchemyError, TypeError, ValueError):
        raise FinancialArchiveUnavailableError("financial archive URL is invalid") from None
    if (
        url.drivername != "mysql+pymysql"
        or not url.host
        or not url.database
        or _uses_unix_socket(url)
    ):
        raise FinancialArchiveUnavailableError("financial archive requires an explicit MySQL URL")
    try:
        matches_application_database = _matches_application_database(url)
    except Exception:
        raise FinancialArchiveUnavailableError(
            "financial archive configuration is invalid"
        ) from None
    if matches_application_database:
        raise FinancialArchiveUnavailableError("financial archive must use a dedicated database")
    try:
        return create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 5},
        )
    except Exception:
        raise FinancialArchiveUnavailableError("financial archive engine is unavailable") from None


def _matches_application_database(archive_url: URL) -> bool:
    """Reject known application targets; this comparison cannot resolve DNS aliases."""
    archive_target = (
        _canonical_database_host(archive_url.host or ""),
        archive_url.port or 3306,
        (archive_url.database or "").casefold(),
    )
    for application_url in (settings.database_url_sync, settings.data_database_url):
        try:
            parsed = make_url(application_url)
        except (SQLAlchemyError, TypeError, ValueError):
            raise ValueError("application database URL cannot be compared") from None
        if parsed.get_backend_name() != "mysql":
            continue
        if not parsed.host or not parsed.database or _uses_unix_socket(parsed):
            raise ValueError("application MySQL target cannot be compared safely")
        application_target = (
            _canonical_database_host(parsed.host),
            parsed.port or 3306,
            parsed.database.casefold(),
        )
        if archive_target == application_target:
            return True
    return False


def _canonical_database_host(host: str) -> str:
    """Normalize literal loopback aliases without making network DNS requests."""
    normalized = host.encode("idna").decode("ascii").casefold().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return "<loopback>"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return "<loopback>" if address.is_loopback else address.compressed


def _uses_unix_socket(url: URL) -> bool:
    """Identify MySQL URLs whose effective endpoint may bypass their host field."""
    return any(key.casefold() in {"unix_socket", "socket"} for key in url.query)


def _row_payload(row: FinancialObservationArchiveRow) -> dict[str, str | None]:
    """Return only the public, source-native fields for one observation."""
    return {
        "source_id": row.source_id,
        "instrument_id": row.instrument_id,
        "statement_type": row.statement_type,
        "periodicity": row.periodicity,
        "report_period": row.report_period.isoformat(),
        "source_disclosure_date": row.source_disclosure_date.isoformat(),
        "observed_at_utc": row.observed_at_utc.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "item_code": row.item_code,
        "value_decimal_text": row.value_decimal_text,
        "native_unit": row.native_unit,
    }


def _response_payload(
    page: FinancialObservationArchivePage,
    *,
    source_id: str,
    instrument_id: str,
    statement_type: str,
    periodicity: str,
    start_report_period: date,
    end_report_period: date,
) -> dict[str, object]:
    """Build an allowlisted payload and reject incomplete or oversized pages."""
    if page.has_more:
        raise FinancialArchivePageTooLargeError

    data: dict[str, object] = {
        "scope": {
            "source_id": source_id,
            "instrument_id": instrument_id,
            "statement_type": statement_type,
            "periodicity": periodicity,
            "start_report_period": start_report_period.isoformat(),
            "end_report_period": end_report_period.isoformat(),
        },
        "records": [_row_payload(row) for row in page.rows],
        "completeness": "unknown",
        "point_in_time_reconstruction": "unavailable",
        "period_amount_semantics": "unknown",
    }
    response = JSONResponse(content=data)
    if len(response.body) > _MAX_RESPONSE_BYTES:
        raise FinancialArchivePageTooLargeError
    return data


def _read_archive_page(
    *,
    source_id: str,
    instrument_id: str,
    statement_type: str,
    periodicity: str,
    start_report_period: date,
    end_report_period: date,
    page_size: int,
) -> dict[str, object]:
    """Read one repeatable-read page and return a checked response payload."""
    engine = _archive_engine_from_settings()
    try:
        with engine.connect() as connection:
            rr_connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with rr_connection.begin():
                page = read_financial_observation_page(
                    rr_connection,
                    source_id=source_id,
                    instrument_id=instrument_id,
                    statement_type=statement_type,
                    periodicity=periodicity,
                    start_report_period=start_report_period,
                    end_report_period=end_report_period,
                    page_size=page_size,
                )
                return _response_payload(
                    page,
                    source_id=source_id,
                    instrument_id=instrument_id,
                    statement_type=statement_type,
                    periodicity=periodicity,
                    start_report_period=start_report_period,
                    end_report_period=end_report_period,
                )
    except FinancialArchivePageTooLargeError:
        raise
    except (FinancialObservationArchiveReadError, TypeError, ValueError):
        # Query arguments are validated by FastAPI before this worker is called.
        # A reader rejection here therefore means the configured archive failed
        # to satisfy its published storage contract.
        raise FinancialArchiveUnavailableError("financial archive data is unavailable") from None
    except Exception:
        raise FinancialArchiveUnavailableError("financial archive is unavailable") from None
    finally:
        with suppress(Exception):
            engine.dispose()


@router.get(
    "/financial-statements/archive",
    response_model=FinancialArchiveResponse,
    responses=_ERROR_RESPONSES,
)
async def query_financial_observation_archive(
    request: Request,
    principal: CurrentPrincipal,
    source_id: Annotated[
        str,
        Query(min_length=1, max_length=64, pattern=_SOURCE_ID_PATTERN),
    ],
    instrument_id: Annotated[
        str,
        Query(min_length=9, max_length=9, pattern=_INSTRUMENT_ID_PATTERN),
    ],
    statement_type: Literal["income", "balance", "cashflow"],
    periodicity: Literal["annual", "quarterly"],
    start_report_period: date,
    end_report_period: date,
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = 100,
) -> Response:
    """Return one complete, bounded page of source-native financial rows.

    Authentication and the existing ``financial_statement`` scope check run
    before the archive engine is created or an archive connection is opened.
    The configured archive account must be provisioned with SELECT-only access.
    This endpoint exposes no cursor, request-wide ``as_of`` value, completeness
    assertion, or reconstructed point-in-time result.
    """
    require_domain_access(principal, "financial_statement")
    _reject_unknown_query(
        request,
        {
            "source_id",
            "instrument_id",
            "statement_type",
            "periodicity",
            "start_report_period",
            "end_report_period",
            "page_size",
        },
    )
    if _SOURCE_ID_FULL_PATTERN.fullmatch(source_id) is None:
        raise HTTPException(
            status_code=_HTTP_422_STATUS,
            detail={"code": "invalid_request", "message": "source_id is invalid"},
        )
    if start_report_period > end_report_period:
        raise HTTPException(
            status_code=_HTTP_422_STATUS,
            detail={
                "code": "invalid_request",
                "message": "start_report_period cannot be after end_report_period",
            },
        )
    if (end_report_period - start_report_period).days > _MAX_REPORT_PERIOD_SPAN_DAYS:
        raise HTTPException(
            status_code=_HTTP_422_STATUS,
            detail={
                "code": "invalid_request",
                "message": "report-period range cannot exceed 3,660 days",
            },
        )

    try:
        payload = await asyncio.to_thread(
            _read_archive_page,
            source_id=source_id,
            instrument_id=instrument_id,
            statement_type=statement_type,
            periodicity=periodicity,
            start_report_period=start_report_period,
            end_report_period=end_report_period,
            page_size=page_size,
        )
    except FinancialArchivePageTooLargeError:
        raise HTTPException(
            status_code=_HTTP_413_STATUS,
            detail={
                "code": "response_too_large",
                "message": "Financial archive result exceeds the 256 KiB response limit",
            },
        ) from None
    except FinancialArchiveUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "published_data_unavailable",
                "message": "Financial archive service is unavailable",
            },
        ) from None
    return _success_response(payload)


__all__ = ["query_financial_observation_archive", "router"]
