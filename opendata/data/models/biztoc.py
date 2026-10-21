"""Biztoc World News contract: the article fields the public RapidAPI answer publishes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from opendata.data.models.base import ContractModel


class BiztocWorldNews(ContractModel):
    """One Biztoc world-news article."""

    date: datetime
    title: str
    author: str | None = None
    excerpt: str | None = None
    body: str | None = None
    images: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    score: float | None = None
    url: str | None = None


__all__ = ["BiztocWorldNews"]
