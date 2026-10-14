"""Declared public documentation and unresolved machine-response contracts."""

from __future__ import annotations

from typing import Final

DECLARED_MODELS: Final[tuple[str, ...]] = ()
NOT_DECLARED: Final[dict[str, str]] = {
    "CalendarEarnings": (
        "The official earnings-calendar page renders client-side data, while its public help page "
        "documents user-visible columns but no stable machine-response schema."
    ),
    "ForwardEpsEstimates": (
        "Official help confirms past and future EPS estimates in the UI, but the public estimates "
        "page returned HTTP 403 without credentials and no official machine protocol is documented."
    ),
    "ForwardSalesEstimates": (
        "Official help confirms past and future revenue estimates in the UI, but the public "
        "estimates page returned HTTP 403 without credentials and no official machine protocol is "
        "documented."
    ),
}
