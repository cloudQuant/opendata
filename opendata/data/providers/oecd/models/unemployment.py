"""Unemployment fetcher (domain ``economy_unemployment``, contract ``MacroSeries``).

Serves the macro unemployment scenario through the OECD LFS indicators
dataflow (``DF_LFS_INDIC``, annual). The working unemployment-rate key
was read off the flow's own serieskeysonly catalogue after five
exploration sessions: ``USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE`` - the
unit is ``PT_LF_SUB`` (percentage of labour force), not a plain
percentage code.

Clean-room note (design §1.3): self-written; only the OECD SDMX API's
public interface is referenced.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.oecd._source import SOURCE
from opendata.data.providers.oecd.models._client import (
    OecdProviderError,
    fetch_observations,
    normalize_period,
)

#: The LFS indicators dataflow hosting the unemployment-rate series.
LFS_INDIC_FLOW = "OECD.ELS.SAE,DSD_LFS@DF_LFS_INDIC"


class UnemploymentQuery(QueryParams):
    """Validated query for the macro unemployment domain."""

    series_key: str


class OecdUnemploymentFetcher(Fetcher[UnemploymentQuery, list[dict[str, object]]]):
    """Unemployment-rate observations for one LFS_INDIC series key."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_unemployment",
        period="1A",
        market="global",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> UnemploymentQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``series_key`` (required, the full six-dimension key,
                for example ``USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return UnemploymentQuery.model_validate(kwargs)

    def extract_data(self, params: UnemploymentQuery, ctx: FetchContext) -> list[dict[str, object]]:
        """Fetch the upstream observations (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The parsed CSV rows.

        Raises:
            OecdProviderError: Upstream failure or an unreadable body
                (stable codes).
        """
        return fetch_observations(
            params.series_key,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
            flow=LFS_INDIC_FLOW,
        )

    def transform_data(
        self, raw: list[dict[str, object]], params: UnemploymentQuery
    ) -> FetchResult:
        """Normalize the observations (the normalize stage).

        ``series_id`` is the dotted six-dimension LFS key; annual
        ``TIME_PERIOD`` values widen to January 1st and missing values
        become ``None``.

        Args:
            raw: Parsed CSV rows.
            params: The validated query.

        Returns:
            Contract rows sorted by observation date.

        Raises:
            OecdProviderError: An observation cannot be parsed
                (``OECD_BAD_OBSERVATION``).
        """
        records = [self._record(row) for row in raw]
        frame = pd.DataFrame.from_records(records, columns=["series_id", "date", "value"])
        rows = MacroSeries.from_frame(frame)
        return tuple(sorted(rows, key=lambda point: point.date))

    @staticmethod
    def _record(row: dict[str, object]) -> dict[str, object]:
        """Map one CSV row onto a contract record.

        Args:
            row: Parsed CSV row (six LFS key columns plus period/value).

        Returns:
            The record for :meth:`MacroSeries.from_frame`.

        Raises:
            OecdProviderError: The row cannot be parsed
                (``OECD_BAD_OBSERVATION``).
        """
        try:
            value_text = str(row["OBS_VALUE"]).strip()
            return {
                "series_id": OecdUnemploymentFetcher._series_id(row),
                "date": normalize_period(str(row["TIME_PERIOD"]).strip()),
                "value": float(value_text) if value_text else None,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OecdProviderError("OECD_BAD_OBSERVATION") from exc

    @staticmethod
    def _series_id(row: dict[str, object]) -> str:
        """Build the dotted six-dimension LFS series identifier."""
        parts = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "LABOUR_FORCE_STATUS"]
        return ".".join(str(row[part]).strip() for part in parts)
