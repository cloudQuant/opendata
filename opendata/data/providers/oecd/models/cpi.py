"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

Serves the macro CPI scenario (AI research / asset allocation) through
the OECD SDMX API (HICP dataflow, monthly). Series keys are position-
strict 8-dimension dotted strings carried whole by the query; the
contract's ``series_id`` is that dotted key.

Clean-room note (design §1.3): self-written; only the OECD SDMX API's
public interface is referenced.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.oecd._source import SOURCE
from opendata.data.providers.oecd.models._client import OecdProviderError, fetch_observations


class CpiQuery(QueryParams):
    """Validated query for the macro CPI domain."""

    series_key: str


class OecdCpiFetcher(Fetcher[CpiQuery, list[dict[str, object]]]):
    """HICP observations for one OECD series (a full 8-dimension key)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1M",
        market="eu",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> CpiQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``series_key`` (required, the full position-strict
                key, for example ``GBR.M.HICP.CPI.PC.CP09.N.G1``),
                ``start_date``, ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return CpiQuery.model_validate(kwargs)

    def extract_data(self, params: CpiQuery, ctx: FetchContext) -> list[dict[str, object]]:
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
        )

    def transform_data(self, raw: list[dict[str, object]], params: CpiQuery) -> FetchResult:
        """Normalize the observations (the normalize stage).

        The contract's ``series_id`` is the dotted ``REF_AREA``-prefixed
        key; SDMX publishes monthly ``TIME_PERIOD`` values, widened to the
        first of the month (same convention as FRED/ECB), and missing
        observations as an empty ``OBS_VALUE``, which becomes ``None``.

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
            row: Parsed CSV row (``REF_AREA``/``TIME_PERIOD``/``OBS_VALUE``).

        Returns:
            The record for :meth:`MacroSeries.from_frame`.

        Raises:
            OecdProviderError: The row cannot be parsed
                (``OECD_BAD_OBSERVATION``).
        """
        try:
            value_text = str(row["OBS_VALUE"]).strip()
            return {
                "series_id": OecdCpiFetcher._series_id(row),
                "date": OecdCpiFetcher._normalize_period(str(row["TIME_PERIOD"]).strip()),
                "value": float(value_text) if value_text else None,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OecdProviderError("OECD_BAD_OBSERVATION") from exc

    @staticmethod
    def _series_id(row: dict[str, object]) -> str:
        """Build the dotted series identifier from the row's dimensions.

        Args:
            row: Parsed CSV row carrying the eight key columns.

        Returns:
            The dotted key, ``REF_AREA`` first, as published.
        """
        parts = [
            "REF_AREA",
            "FREQ",
            "METHODOLOGY",
            "MEASURE",
            "UNIT_MEASURE",
            "EXPENDITURE",
            "ADJUSTMENT",
            "TRANSFORMATION",
        ]
        return ".".join(str(row[part]).strip() for part in parts)

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
