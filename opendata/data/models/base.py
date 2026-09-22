"""Base class for the standardized contract models (design §4.1).

Every contract model serializes to both a pydantic object and a
``pandas.DataFrame`` (FR-2); :meth:`ContractModel.to_frame` and
:meth:`ContractModel.from_frame` are the two directions of that contract.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import TYPE_CHECKING, get_args

import pandas as pd
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from pydantic.fields import FieldInfo
    from typing_extensions import Self


def _is_missing(value: object) -> bool:
    """Detect pandas/float missing markers (NaN, NaT, NA) and None."""
    if value is None or value is pd.NaT or value is pd.NA:
        return True
    return isinstance(value, float) and math.isnan(value)


def _is_nullable(field: FieldInfo) -> bool:
    """Whether the field's annotation accepts ``None``."""
    return type(None) in get_args(field.annotation)


def _is_date_field(field: FieldInfo) -> bool:
    """Whether the field stores a ``datetime.date`` (excluding datetime)."""
    return date in get_args(field.annotation) or field.annotation is date


class ContractModel(BaseModel):
    """Base class for all standardized data contract models.

    Models are strict by design: unknown fields are rejected at
    construction so mapping mistakes in ``normalize()`` fail loudly
    instead of being silently dropped, and NaN/inf are rejected in
    float fields because a contract row with a NaN price is bad data,
    not a missing value (fail-closed principle, design §8.2).
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @classmethod
    def to_frame(cls, rows: Sequence[Self]) -> pd.DataFrame:
        """Serialize contract rows into a DataFrame.

        Args:
            rows: Contract model instances of this class.

        Returns:
            A DataFrame whose columns are exactly the model fields, in
            declaration order. ``date`` fields stay ``datetime.date``
            objects, which round-trips losslessly with MySQL DATE columns.
        """
        if not rows:
            return pd.DataFrame(columns=list(cls.model_fields))
        return pd.DataFrame([row.model_dump() for row in rows])

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> list[Self]:
        """Build contract rows from a DataFrame.

        Only columns declared by the model are read; extra columns (for
        example dwd lineage columns such as ``source`` or ``_merged_at``)
        are ignored. pandas timestamps in date fields are coerced to
        ``datetime.date``; NaN/NaT in nullable fields becomes ``None``.
        A missing *required* column raises so that field-name mismatches
        fail closed instead of silently passing (design §8.2).

        Args:
            df: Frame with already-normalized column names (normalize()
                is the single owner of field mapping, design §4.2).

        Returns:
            One model instance per input row, in frame order.

        Raises:
            ValueError: If a required column is missing.
        """
        if df.empty:
            return []
        missing = sorted(
            name
            for name, field in cls.model_fields.items()
            if field.is_required() and name not in df.columns
        )
        if missing:
            raise ValueError(f"{cls.__name__}: missing required columns: {missing}")
        records = (cls._coerce_record(row) for row in df.to_dict("records"))
        return [cls(**record) for record in records]

    @classmethod
    def _coerce_record(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Project one DataFrame record onto the model's fields.

        Args:
            row: A record from ``DataFrame.to_dict("records")``.

        Returns:
            The record restricted to model fields, with missing markers
            normalized to ``None`` in nullable fields and pandas
            timestamps coerced to ``datetime.date`` in date fields.
        """
        record: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            if name not in row:
                continue
            value = row[name]
            if _is_missing(value):
                if not _is_nullable(field):
                    # Required non-nullable fields fall through to pydantic,
                    # which fails loudly on the missing marker.
                    record[name] = value
                    continue
                record[name] = None
                continue
            if _is_date_field(field) and isinstance(value, datetime):
                value = value.date()
            record[name] = value
        return record
