"""Pure offline preview of one fixed CSI 500 Fuyao request shape.

This module reports a deterministic caller-side query digest. It does not
verify a trading calendar, establish source rights, read configuration, touch a
target database, create gate state, or contact Fuyao.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from opendata.data.providers.ths.endpoints import (
    INDEX_PRICES_ENDPOINT,
    build_index_prices_request,
)
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.request_summary import request_summary_sha256

CSI500_SYMBOL = "000905.SH"
CSI500_DATASET_ID = "cn.csi500.index.daily"
_REQUEST_SUMMARY_FIELDS = ("end", "interval", "start", "thscode")
_BUSINESS_DATE_PATTERN = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class Csi500PreviewInputError(ValueError):
    """Invalid date input for a safe offline request preview."""


@dataclass(frozen=True, slots=True)
class Csi500RequestPreview:
    """A request-shape summary that cannot be used as an execution permit."""

    status: Literal["preview_only"]
    authorization_status: Literal["not_established"]
    calendar_evidence_status: Literal["not_verified"]
    target_status: Literal["not_required_not_accessed"]
    gate_status: Literal["not_accessed"]
    execution_enabled: Literal[False]
    dataset_id: str
    symbol: str
    business_date: date
    method: Literal["GET"]
    host: str
    path: str
    query_summary_sha256: str


def parse_business_date(value: object) -> date:
    """Parse one exact ISO date; this does not establish an exchange session."""
    if not isinstance(value, str) or _BUSINESS_DATE_PATTERN.fullmatch(value) is None:
        raise Csi500PreviewInputError("business_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise Csi500PreviewInputError("business_date_invalid") from None
    if parsed.isoformat() != value:
        raise Csi500PreviewInputError("business_date_invalid")
    return parsed


def preview_csi500_request(*, business_date: date) -> Csi500RequestPreview:
    """Summarize the fixed one-day request without authorization or external I/O.

    Args:
        business_date: Caller-selected date. Its exchange-calendar status is
            intentionally not checked by this preview.

    Returns:
        Fixed route metadata and a digest of the endpoint builder's exact
        one-day query parameters. Raw parameters, credentials, and response
        data are not returned.

    Raises:
        Csi500PreviewInputError: If the date is not an exact ``date`` value or
            the repository's default Fuyao route differs from this fixed path.
    """
    if type(business_date) is not date:
        raise Csi500PreviewInputError("business_date_invalid")
    if business_date == date.max:
        # The fixed one-day request needs the following date as its exclusive
        # endpoint; there is no representable endpoint after 9999-12-31.
        raise Csi500PreviewInputError("business_date_invalid")

    endpoint = urlsplit(DEFAULT_FUYAO_API_BASE_URL)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "fuyao.aicubes.cn"
        or endpoint.port is not None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise Csi500PreviewInputError("fixed_route_invalid")

    parameters = build_index_prices_request(
        symbol=CSI500_SYMBOL,
        start=business_date,
        end=date.fromordinal(business_date.toordinal() + 1),
    )
    query_summary = request_summary_sha256(
        {key: str(value) for key, value in parameters.items()},
        allowed_fields=_REQUEST_SUMMARY_FIELDS,
    )
    return Csi500RequestPreview(
        status="preview_only",
        authorization_status="not_established",
        calendar_evidence_status="not_verified",
        target_status="not_required_not_accessed",
        gate_status="not_accessed",
        execution_enabled=False,
        dataset_id=CSI500_DATASET_ID,
        symbol=CSI500_SYMBOL,
        business_date=business_date,
        method="GET",
        host=endpoint.hostname,
        path=INDEX_PRICES_ENDPOINT,
        query_summary_sha256=query_summary,
    )


__all__ = [
    "CSI500_DATASET_ID",
    "CSI500_SYMBOL",
    "Csi500PreviewInputError",
    "Csi500RequestPreview",
    "parse_business_date",
    "preview_csi500_request",
]
