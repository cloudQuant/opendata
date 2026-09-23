"""Shared fetcher skeleton for ECB Data Portal series (C1 P1).

The Data Portal serves every series the same way - a position-strict
dotted key, CSV output, granularity-shaped ``TIME_PERIOD`` - so the
CPI/rate fetchers differ only in their capability. The CPI fetcher
migrated onto this base with its tests unchanged (the fred/imf
skeletons set the pattern; this is the same rule of three arriving at
the third ECB-shaped consumer, written at two because the shape was
already proven twice elsewhere).
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.ecb.models._client import EcbProviderError, fetch_observations


class SeriesQuery(QueryParams):
    """Validated query for one ECB series."""

    series_id: str


class EcbSeriesFetcher(Fetcher[SeriesQuery, list[dict[str, object]]]):
    """ECB observations for one series; subclasses declare the domain.

    The upstream ignores the lowercase ``startperiod`` variant (verified
    live) - the client sends the camelCase form. Subclasses with a key
    key are verified live via the self-checking e2e pattern.
    """

    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> SeriesQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``series_id`` (required, the full position-strict
                key, for example ``ICP/M.U2.N.000000.4.ANR``), ``start_date``,
                ``end_date``.

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
            The parsed CSV rows.

        Raises:
            EcbProviderError: Upstream failure or an unreadable body
                (stable codes).
        """
        return fetch_observations(
            params.series_id,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, object]], params: SeriesQuery) -> FetchResult:
        """Normalize the observations (the normalize stage).

        The dotted ``KEY`` published in the CSV becomes the contract's
        ``series_id``; SDMX publishes ``YYYY``/``YYYY-MM``/``YYYY-MM-DD``
        periods, widened to the first of the period, and missing
        observations as an empty ``OBS_VALUE``, which becomes ``None``.

        Args:
            raw: Parsed CSV rows.
            params: The validated query.

        Returns:
            Contract rows sorted by observation date.

        Raises:
            EcbProviderError: An observation cannot be parsed
                (``ECB_BAD_OBSERVATION``).
        """
        records = [self._record(row) for row in raw]
        frame = pd.DataFrame.from_records(records, columns=["series_id", "date", "value"])
        rows = MacroSeries.from_frame(frame)
        return tuple(sorted(rows, key=lambda point: point.date))

    @staticmethod
    def _record(row: dict[str, object]) -> dict[str, object]:
        """Map one CSV row onto a contract record.

        Args:
            row: Parsed CSV row (``KEY``/``TIME_PERIOD``/``OBS_VALUE``).

        Returns:
            The record for :meth:`MacroSeries.from_frame`.

        Raises:
            EcbProviderError: The row cannot be parsed
                (``ECB_BAD_OBSERVATION``).
        """
        try:
            value_text = str(row["OBS_VALUE"]).strip()
            return {
                "series_id": str(row["KEY"]).strip(),
                "date": EcbSeriesFetcher._normalize_period(str(row["TIME_PERIOD"]).strip()),
                "value": float(value_text) if value_text else None,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise EcbProviderError("ECB_BAD_OBSERVATION") from exc

    @staticmethod
    def _normalize_period(period: str) -> str:
        """Widen an SDMX period to a full date.

        SDMX publishes ``YYYY`` for annual and ``YYYY-MM`` for monthly
        frequencies; monthly observations land on the first of the month,
        the same convention FRED's own dates use.

        Args:
            period: The published ``TIME_PERIOD`` value.

        Returns:
            The value widened to ``YYYY-MM-DD``.

        Raises:
            ValueError: The period does not match a known granularity.
        """
        if len(period) == 10:
            return period
        if len(period) == 7:
            return f"{period}-01"
        if len(period) == 4:
            return f"{period}-01-01"
        raise ValueError(period)
