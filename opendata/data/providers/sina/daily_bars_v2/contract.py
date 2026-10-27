"""Source-native contract and recorded schema profiles for Sina daily bars v2.

This module describes the raw Sina-shaped product only. It does not register a
provider, perform I/O, or claim that the source's native units have been
verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from opendata.data.models.base import ContractModel


@dataclass(frozen=True)
class SinaSourceProfile:
    """Explicit source-column contract for one recorded Sina endpoint shape.

    ``amount_column=None`` is an affirmative declaration that this endpoint
    shape has no turnover column. It is not inferred from a missing field at
    runtime. ``excluded_source_columns`` records source fields omitted by the
    OHLCV projection; the complete upstream frame must be retained separately
    whenever full source fidelity is required. Source unit labels intentionally
    remain unverified.
    """

    name: str
    source: Literal["sina"]
    asset_class: Literal["futures", "option", "bond"]
    domain: Literal["futures_daily_v2", "option_daily_v2", "bond_daily_v2"]
    function: str
    upstream_commit: str
    expected_columns: tuple[str, ...]
    date_column: str
    open_column: str
    high_column: str
    low_column: str
    close_column: str
    volume_column: str
    amount_column: str | None
    amount_absence_reason: str | None
    excluded_source_columns: tuple[str, ...] = ()
    price_unit_status: Literal["unverified_source_native"] = "unverified_source_native"
    volume_unit_status: Literal["unverified_source_native"] = "unverified_source_native"
    amount_unit_status: Literal["unverified_source_native", "not_provided"] = "not_provided"

    def __post_init__(self) -> None:
        """Reject internally inconsistent profiles at definition time."""
        if len(set(self.expected_columns)) != len(self.expected_columns):
            raise ValueError(f"{self.name}: expected_columns contains duplicates")
        if len(set(self.excluded_source_columns)) != len(self.excluded_source_columns):
            raise ValueError(f"{self.name}: excluded_source_columns contains duplicates")
        mapped_columns = {
            self.date_column,
            self.open_column,
            self.high_column,
            self.low_column,
            self.close_column,
            self.volume_column,
        }
        if not mapped_columns.issubset(self.expected_columns):
            raise ValueError(f"{self.name}: mapped columns must be declared in expected_columns")
        if self.amount_column is None:
            if not self.amount_absence_reason:
                raise ValueError(
                    f"{self.name}: an absent amount column needs an explicit source declaration"
                )
            if self.amount_unit_status != "not_provided":
                raise ValueError(f"{self.name}: absent amount cannot have a unit status")
        elif self.amount_column not in self.expected_columns:
            raise ValueError(f"{self.name}: amount_column must be declared in expected_columns")
        elif self.amount_absence_reason is not None:
            raise ValueError(
                f"{self.name}: amount_absence_reason conflicts with a declared amount_column"
            )
        elif self.amount_unit_status != "unverified_source_native":
            raise ValueError(
                f"{self.name}: a present source-native amount unit must be marked unverified"
            )
        retained_columns = set(mapped_columns)
        if self.amount_column is not None:
            retained_columns.add(self.amount_column)
        actual_excluded = set(self.expected_columns) - retained_columns
        if set(self.excluded_source_columns) != actual_excluded:
            raise ValueError(
                f"{self.name}: every unprojected source column must be explicitly listed"
            )
        if not set(self.excluded_source_columns).issubset(self.expected_columns):
            raise ValueError(
                f"{self.name}: excluded_source_columns must be declared in expected_columns"
            )


class SourceNativeBarV2(ContractModel):
    """Offline OHLCV projection, not a persistence or API contract.

    ``amount`` has no default, so every serialized row contains the key; it
    may be a real source value (including zero) or ``None``. ``volume`` and
    prices retain the source's native numeric scale, which remains unverified.
    Source, profile, domain and unit-status metadata travel with each row so
    source-native data cannot be mixed without its interpretation context.
    This object is only an OHLCV projection, not the complete source payload.
    ``excluded_source_columns`` names fields discarded by that projection;
    retain the upstream frame separately for full-fidelity use.
    """

    source: Literal["sina"]
    source_profile: str
    asset_class: Literal["futures", "option", "bond"]
    domain: Literal["futures_daily_v2", "option_daily_v2", "bond_daily_v2"]
    projection: Literal["ohlcv_projection"]
    excluded_source_columns: tuple[str, ...]
    source_frame_retention_required: Literal[True]
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None
    price_unit_status: Literal["unverified_source_native"]
    volume_unit_status: Literal["unverified_source_native"]
    amount_unit_status: Literal["unverified_source_native", "not_provided"]


RECORDED_UPSTREAM_COMMIT = "c4f6a631c259783dbc2507b6b27d179b3e88079d"

FUTURES_DAILY_SINA_PROFILE = SinaSourceProfile(
    name="futures_daily_sina",
    source="sina",
    asset_class="futures",
    domain="futures_daily_v2",
    function="futures_zh_daily_sina",
    upstream_commit=RECORDED_UPSTREAM_COMMIT,
    expected_columns=("date", "open", "high", "low", "close", "volume", "hold", "settle"),
    date_column="date",
    open_column="open",
    high_column="high",
    low_column="low",
    close_column="close",
    volume_column="volume",
    amount_column=None,
    amount_absence_reason=(
        "Recorded futures_zh_daily_sina response has no turnover column "
        "(tests/fixtures/upstream/futures_daily_sina/meta.json)."
    ),
    excluded_source_columns=("hold", "settle"),
)

OPTION_DAILY_SINA_PROFILE = SinaSourceProfile(
    name="option_daily_sina",
    source="sina",
    asset_class="option",
    domain="option_daily_v2",
    function="option_sse_daily_sina",
    upstream_commit=RECORDED_UPSTREAM_COMMIT,
    expected_columns=("日期", "开盘", "最高", "最低", "收盘", "成交量"),
    date_column="日期",
    open_column="开盘",
    high_column="最高",
    low_column="最低",
    close_column="收盘",
    volume_column="成交量",
    amount_column=None,
    amount_absence_reason=(
        "Recorded option_sse_daily_sina response has no turnover column "
        "(tests/fixtures/upstream/option_daily_sina/meta.json)."
    ),
)

BOND_DAILY_SINA_PROFILE = SinaSourceProfile(
    name="bond_daily_sina",
    source="sina",
    asset_class="bond",
    domain="bond_daily_v2",
    function="bond_zh_hs_cov_daily",
    upstream_commit=RECORDED_UPSTREAM_COMMIT,
    expected_columns=("date", "open", "high", "low", "close", "volume"),
    date_column="date",
    open_column="open",
    high_column="high",
    low_column="low",
    close_column="close",
    volume_column="volume",
    amount_column=None,
    amount_absence_reason=(
        "Recorded bond_zh_hs_cov_daily response has no turnover column "
        "(tests/fixtures/upstream/bond_daily_sina/meta.json)."
    ),
)
