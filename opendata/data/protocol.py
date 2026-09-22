"""Three-stage data source protocol (design §4.2, provider template §7.1).

``QueryParams`` -> ``Fetcher`` -> standardized output. The stage names
follow the concrete template of design §7.1 and map onto the §4.2
pipeline semantics::

    transform_query  ==  validate   (build + validate query params)
    extract_data     ==  fetch_raw  (transport, source-native shape)
    transform_data   ==  normalize   (the single owner of field
                                      mapping, unit conversion and
                                      key normalization, §4.2)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Generic, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from opendata.data.capability import Capability
    from opendata.data.models import ContractModel

#: Query parameter type; concrete fetchers narrow it (bound: QueryParams).
QueryT = TypeVar("QueryT", bound="QueryParams")
#: Raw extraction type; an implementation detail of each fetcher.
RawT = TypeVar("RawT")
# Quoted: pandas is untyped, so the unquoted form would not be a valid
# type alias for mypy (Sequence[ContractModel] | Any collapses to Any).
FetchResult: TypeAlias = "Sequence[ContractModel] | pd.DataFrame"


class QueryParams(BaseModel):
    """Common query parameters shared by every domain (design §4.2).

    Domain-specific queries subclass this and add their own fields
    (and may narrow ``symbol`` to a required field, as in the §7.1
    template). Unknown fields are rejected so typos fail closed.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = "auto"
    market: str | None = None
    symbol: str | None = None
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class FetchContext:
    """Per-call execution context passed to the extraction stage.

    Currently carries only a timeout override; it will gain the shared
    HTTP session handle once the unified client lands (design §5.3,
    milestone A1.4) so fetchers never build their own transports.
    """

    timeout: float | None = None


class Fetcher(ABC, Generic[QueryT, RawT]):
    """Base class for every data source fetcher.

    Concrete fetchers declare their :class:`~opendata.data.capability.Capability`
    as the ``capability`` class attribute (design §7.1) and implement
    the three stages. ``fetch`` is the template method wiring the
    stages together; callers (service layer, pipeline) only use
    ``fetch``.
    """

    capability: ClassVar[Capability]

    def fetch(self, *, ctx: FetchContext | None = None, **kwargs: object) -> FetchResult:
        """Run the validate -> extract -> normalize pipeline.

        Args:
            ctx: Optional per-call execution context (timeouts, later
                the shared HTTP session).
            **kwargs: Domain-specific query fields, forwarded to
                ``transform_query`` and validated there.

        Returns:
            Standardized contract models or a DataFrame.

        Raises:
            pydantic.ValidationError: If the query parameters are
                invalid or unknown (fail closed in the first stage).
        """
        params = self.transform_query(**kwargs)
        context = ctx if ctx is not None else FetchContext()
        raw = self.extract_data(params, context)
        return self.transform_data(raw, params)

    @abstractmethod
    def transform_query(self, **kwargs: object) -> QueryT:
        """Build and validate the domain query (the validate stage).

        Args:
            **kwargs: Domain-specific query fields.

        Returns:
            The validated query params instance.
        """

    @abstractmethod
    def extract_data(self, params: QueryT, ctx: FetchContext) -> RawT:
        """Fetch raw source data (the fetch_raw stage).

        Args:
            params: Validated query parameters.
            ctx: Per-call execution context.

        Returns:
            Source-native raw data, opaque to the protocol.
        """

    @abstractmethod
    def transform_data(self, raw: RawT, params: QueryT) -> FetchResult:
        """Normalize raw data into contract models (the normalize stage).

        This is the single owner of field mapping, unit conversion
        (手 -> 股), adjust semantics and key normalization
        (``600519`` <-> ``600519.SH``), design §4.2.

        Args:
            raw: Output of ``extract_data``.
            params: The validated query parameters.

        Returns:
            Standardized contract models or a DataFrame.
        """
