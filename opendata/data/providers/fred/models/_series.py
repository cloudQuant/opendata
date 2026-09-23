"""Shared fetcher skeleton for FRED indicator series (C1 P1).

FRED serves every macro series through the same ``series/observations``
endpoint, so the CPI/GDP/unemployment fetchers differ only in their
capability: the query shape (``series_id``), the three pipeline stages
and the stable failure codes are common. Rule of three applied once the
third series landed - the CPI fetcher migrated onto this base with its
tests unchanged.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models._client import FredProviderError, fetch_observations


class SeriesQuery(QueryParams):
    """Validated query for one FRED indicator series."""

    series_id: str


class FredSeriesFetcher(Fetcher[SeriesQuery, list[dict[str, object]]]):
    """FRED observations for one series; subclasses declare the domain.

    ``verified`` stays false on subclasses until the output has been
    compared against the official series values (R2: implemented first,
    verified when the API key is available).
    """

    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> SeriesQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``series_id`` (required, upstream identifier such as
                ``CPIAUCSL``), ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return SeriesQuery.model_validate(kwargs)

    def extract_data(self, params: SeriesQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the upstream observations (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The raw observation dicts.

        Raises:
            FredProviderError: Key missing, upstream failure, or an
                unreadable response (stable codes).
        """
        return fetch_observations(
            params.series_id,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, object]], params: SeriesQuery) -> FetchResult:
        """Normalize the observations (the normalize stage).

        FRED publishes missing observations as its ``.`` sentinel; those
        become ``None`` on the nullable contract instead of fabricated
        zeros.

        Args:
            raw: Raw observation dicts.
            params: The validated query.

        Returns:
            Contract rows sorted by observation date.

        Raises:
            FredProviderError: An observation cannot be parsed
                (``FRED_BAD_OBSERVATION``).
        """
        records = [self._record(observation, params.series_id) for observation in raw]
        frame = pd.DataFrame.from_records(records, columns=["series_id", "date", "value"])
        rows = MacroSeries.from_frame(frame)
        return tuple(sorted(rows, key=lambda point: point.date))

    @staticmethod
    def _record(observation: dict[str, object], series_id: str) -> dict[str, object]:
        """Map one upstream observation onto a contract record.

        Args:
            observation: Raw upstream dict (``date``/``value``).
            series_id: Normalized series identifier.

        Returns:
            The record for :meth:`MacroSeries.from_frame`.

        Raises:
            FredProviderError: The observation cannot be parsed
                (``FRED_BAD_OBSERVATION``).
        """
        try:
            return {
                "series_id": series_id.strip().upper(),
                "date": str(observation["date"]),
                "value": FredSeriesFetcher._parse_value(observation.get("value")),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc

    @staticmethod
    def _parse_value(raw: object) -> float | None:
        """Map FRED's missing sentinel to ``None``, parse the rest."""
        if raw is None:
            return None
        text = str(raw).strip()
        if text in {"", "."}:
            return None
        return float(text)
