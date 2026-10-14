"""Typed rows for the clean-room Federal Reserve documents and curve slice."""

from __future__ import annotations

from datetime import date as date_type

from opendata.data.models.base import ContractModel


class FederalReserveFomcDocument(ContractModel):
    """A document linked from an official FOMC meeting page.

    ``date`` is the meeting date shown in the heading. ``release_date`` is a
    source-specific extension populated only when the page explicitly states
    the release date.
    """

    date: date_type
    doc_type: str
    doc_format: str
    url: str
    release_date: date_type | None = None


class FederalReserveYieldCurveObservation(ContractModel):
    """A nominal Treasury constant-maturity observation from H.15.

    ``rate`` is an additive source extension normalized to fractions per
    annum; H.15 reports the source value in percent per annum. The public
    YieldCurve output publishes only ``date`` and ``maturity``.
    """

    date: date_type
    maturity: str
    rate: float | None


__all__ = ["FederalReserveFomcDocument", "FederalReserveYieldCurveObservation"]
