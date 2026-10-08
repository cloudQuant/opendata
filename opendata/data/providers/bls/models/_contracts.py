"""Compatibility re-exports for the BLS period-series contracts."""

from __future__ import annotations

from opendata.data.models.period_series import (
    BlsCatalogItem,
    BlsCatalogPage,
    BlsFootnote,
    BlsObservation,
)

__all__ = [
    "BlsCatalogItem",
    "BlsCatalogPage",
    "BlsFootnote",
    "BlsObservation",
]
