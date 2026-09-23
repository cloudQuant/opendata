"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

Serves the macro CPI scenario (AI research / asset allocation) through
the IMF DataMapper (indicator ``PCPIPCH``, annual CPI change). The API
is queried directly (no SDK, no key, design §7.1).

Clean-room note (design §1.3): self-written; only the DataMapper's
public interface is referenced.
"""

import datetime
from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import MacroSeries
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models._client import ImfProviderError, fetch_indicator


class CpiQuery(QueryParams):
    """Validated query for the macro CPI domain."""

    indicator: str
    country: str


class ImfCpiFetcher(Fetcher[CpiQuery, dict[str, object]]):
    """Annual CPI observations for one country (for example ``PCPIPCH/USA``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1A",
        market="global",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> CpiQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``indicator`` (required, for example ``PCPIPCH``),
                ``country`` (required, ISO3 such as ``USA``), ``start_date``,
                ``end_date``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
        """
        return CpiQuery.model_validate(kwargs)

    def extract_data(self, params: CpiQuery, ctx: FetchContext) -> dict[str, object]:
        """Fetch the upstream series (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            The ``{year: value}`` mapping as published.

        Raises:
            ImfProviderError: Upstream failure or an unreadable body
                (stable codes).
        """
        return fetch_indicator(
            params.indicator.strip().upper(), params.country.strip().upper(), timeout=ctx.timeout
        )

    def transform_data(self, raw: dict[str, object], params: CpiQuery) -> FetchResult:
        """Normalize the series (the normalize stage).

        The upstream ignores window parameters, so ``start_date``/
        ``end_date`` filter here. Annual observations land on the first
        of the year; ``series_id`` is the dotted ``indicator.country``
        form.

        Args:
            raw: The ``{year: value}`` mapping.
            params: The validated query.

        Returns:
            Contract rows sorted by observation date.

        Raises:
            ImfProviderError: An observation cannot be parsed
                (``IMF_BAD_OBSERVATION``).
        """
        series_id = f"{params.indicator.strip().upper()}.{params.country.strip().upper()}"
        rows = [self._point(year_text, value, series_id) for year_text, value in raw.items()]
        if params.start_date is not None:
            rows = [row for row in rows if row.date >= params.start_date]
        if params.end_date is not None:
            rows = [row for row in rows if row.date <= params.end_date]
        return tuple(sorted(rows, key=lambda row: row.date))

    @staticmethod
    def _point(year_text: object, value: object, series_id: str) -> MacroSeries:
        """Map one ``{year: value}`` entry onto a contract row.

        Args:
            year_text: The published year key (a string of digits).
            value: The published value (``None`` when absent).
            series_id: The dotted ``indicator.country`` identifier.

        Returns:
            The contract row (annual observations land on January 1st).

        Raises:
            ImfProviderError: The entry cannot be parsed
                (``IMF_BAD_OBSERVATION``).
        """
        try:
            return MacroSeries(
                series_id=series_id,
                date=datetime.date(int(str(year_text)), 1, 1),
                value=ImfCpiFetcher._parse_value(value),
            )
        except (TypeError, ValueError) as exc:
            raise ImfProviderError("IMF_BAD_OBSERVATION") from exc

    @staticmethod
    def _parse_value(value: object) -> float | None:
        """Parse a published value; absent values stay ``None``."""
        if value is None:
            return None
        return float(str(value))
