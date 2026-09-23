"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

Serves the macro CPI scenario (AI research / asset allocation) for the
euro area (``REF_AREA`` U2). The ECB Data Portal is queried directly (no
SDK, no key, design §7.1); HICP is monthly, so the capability declares
``1M``.

Clean-room note (design §1.3): self-written; only the ECB Data Portal's
public interface is referenced.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.ecb._source import SOURCE
from opendata.data.providers.ecb.models._client import EcbProviderError, fetch_observations


class CpiQuery(QueryParams):
    """Validated query for the macro CPI domain."""

    series_id: str


class EcbCpiFetcher(Fetcher[CpiQuery, list[dict[str, object]]]):
    """HICP observations for one ECB series (for example the euro-area ANR)."""

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
            **kwargs: ``series_id`` (required, upstream series key such as
                ``ICP/M.U2.N.000000.4.ANR``), ``start_date``, ``end_date``.

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
            EcbProviderError: Upstream failure or an unreadable body
                (stable codes).
        """
        return fetch_observations(
            params.series_id,
            start=params.start_date,
            end=params.end_date,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, object]], params: CpiQuery) -> FetchResult:
        """Normalize the observations (the normalize stage).

        The dotted ``KEY`` published in the CSV becomes the contract's
        ``series_id``; SDMX publishes missing observations as an empty
        ``OBS_VALUE``, which becomes ``None`` on the nullable contract
        instead of a fabricated zero.

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
            The record for :meth:`MacroSeries.from_records`.

        Raises:
            EcbProviderError: The row cannot be parsed
                (``ECB_BAD_OBSERVATION``).
        """
        try:
            value_text = str(row["OBS_VALUE"]).strip()
            return {
                "series_id": str(row["KEY"]).strip(),
                "date": EcbCpiFetcher._normalize_period(str(row["TIME_PERIOD"]).strip()),
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
