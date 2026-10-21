"""Reviewed contract for the EconDB indicator roster.

The 10 fields are exactly the columns
``opendata/data/providers/econdb/specs.py::AVAILABLE_INDICATORS`` declares, which is what
``build_row_model`` checks: a declaration may neither add a column this contract lacks nor drop one
it has. Two of the ten are the declaration's own aliases for the roster's header cells --
``symbol`` reads ``short_ticker``, ``country`` reads ``entity`` -- and the third key the roster
publishes, ``iso``, is on purpose not a field here: the declaration's ``not_equals`` row filter
tests it on the raw record and normalization drops every undeclared key.

``symbol`` is required and non-nullable because upstream indexes ``short_ticker`` unconditionally
(helpers.py:453-457): a roster line without a ticker is a shape failure, not a null ticker. Every
other field is optional with a null default, which is what the roster's own three spellings of "no
value" (``""``, ``N/A``, ``nan`` -- helpers.py:481) become once the declaration's
``columns.null_sentinel`` has rewritten them ahead of the cast.

Two fields a reviewer will be asked about:

* ``multiplier`` is declared and stays null for every row. Upstream computes it from the ``scale``
  text (helpers.py:476), no derive stage can spell that lookup, and the roster publishes no such
  column -- the gap is priced in that package's ``DECLARED_GAPS``, and dropping the field here would
  drop a column the declaration publishes.
* ``last_insert_timestamp`` is a ``str``, not a date. The stamp the roster prints is a timestamp and
  the engine has no datetime value domain, so the contract publishes the source text verbatim.
"""

from __future__ import annotations

from datetime import date

from pydantic import StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class EconDBAvailableIndicators(ContractModel):
    """One ticker of the published ``main_tickers.csv`` roster."""

    symbol: str
    country: str | None = None
    transformation: str | None = None
    currency: str | None = None
    scale: str | None = None
    multiplier: int | None = None
    source: str | None = None
    first_date: date | None = None
    last_date: date | None = None
    last_insert_timestamp: str | None = None


class EconDBEconomicIndicator(ContractModel):
    """One observation from EconDB's documented ``/v1/series/{id}/`` response."""

    date: date
    symbol_root: StrictStr | None = None
    symbol: StrictStr
    country: StrictStr
    value: StrictFloat | None
    unit: StrictStr | None = None
    unit_multiplier: StrictFloat | None = None
    scale: StrictStr | None = None
    order: StrictFloat | None = None
    level: StrictInt | None = None
    title: StrictStr | None = None
    description: StrictStr
    country_code: StrictStr | None = None


class EconDBGdpNominal(ContractModel):
    """Nominal GDP observations, retaining the units published for the selected series."""

    date: date
    country: StrictStr
    value: StrictFloat
    nominal_growth_qoq: StrictFloat | None = None
    nominal_growth_yoy: StrictFloat | None = None
    units: StrictStr | None = None
    symbol: StrictStr
    description: StrictStr


class EconDBGdpReal(ContractModel):
    """Real GDP observations, retaining the units published for the selected series."""

    date: date
    country: StrictStr
    value: StrictFloat
    real_growth_qoq: StrictFloat | None = None
    real_growth_yoy: StrictFloat | None = None
    units: StrictStr | None = None
    symbol: StrictStr
    description: StrictStr


class EconDBPortVolume(ContractModel):
    """Container-traffic and dwell-time observations for one EconDB port."""

    date: date
    port_code: StrictStr
    port_name: StrictStr | None = None
    country: StrictStr | None = None
    country_code: StrictStr | None = None
    export_dwell_time: StrictFloat | None = None
    import_dwell_time: StrictFloat | None = None
    import_teu: StrictInt | None = None
    export_teu: StrictInt | None = None
    transshipment_teu: StrictFloat | None = None


class EconDBYieldCurve(ContractModel):
    """One maturity in a caller-mapped EconDB yield curve snapshot."""

    date: date
    maturity: StrictStr
    value: StrictFloat
    symbol: StrictStr
    country: StrictStr
    units: StrictStr
    description: StrictStr


class EconDBExportDestinations(ContractModel):
    """One merchandise export-destination row from the public EconDB widget."""

    origin_country: str
    destination_country: str
    value: float
    units: str
    title: str
    footnote: str | None = None
