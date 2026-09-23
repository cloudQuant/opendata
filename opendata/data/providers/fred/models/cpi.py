"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

Serves the macro CPI scenario (AI research / asset allocation). The FRED
web service is queried directly (no SDK, design §7.1); the dominant CPI
series are monthly, so the capability declares ``1M``.

Clean-room note (design §1.3): self-written; only FRED's public interface
is referenced.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred._source import SOURCE
from opendata.data.providers.fred.models._client import FredProviderError, fetch_observations


class CpiQuery(QueryParams):
    """Validated query for the macro CPI domain."""

    series_id: str


class FredCpiFetcher(Fetcher[CpiQuery, list[dict[str, object]]]):
    """CPI observations for one FRED series (for example ``CPIAUCSL``).

    ``verified`` stays false until the output has been compared against
    the official series values (R2: implemented first, verified when the
    API key is available), so auto routing skips it for now.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1M",
        market="us",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CpiQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``series_id`` (required, upstream identifier such as
                ``CPIAUCSL``), ``start_date``, ``end_date``.

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

    def transform_data(self, raw: list[dict[str, object]], params: CpiQuery) -> FetchResult:
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
        series_id = params.series_id.strip().upper()
        records = [self._record(observation, series_id) for observation in raw]
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
                "series_id": series_id,
                "date": str(observation["date"]),
                "value": FredCpiFetcher._parse_value(observation.get("value")),
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
