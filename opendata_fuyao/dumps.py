"""fuyao market-dumps 通道：目录契约、受控下载与 Parquet 解析（A3.3）.

全市场导出走预签名 S3 链接（``data.presigned_url``，通常 300 秒有效），
**不可缓存链接**：解析后立即下载消费。dump 目录是静态契约（上游没有裸目录
端点，实测 ``/api/dump/market-dumps`` 返回 404），未知 dump_id 一律失败关闭。

Parquet 解析惰性依赖 ``pyarrow``；未安装时抛出稳定的 ``FUYAO_PARQUET_MISSING``，
不影响其它 provider 与端点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from opendata.data.models import Bar, CorporateAction
from opendata_fuyao.errors import FuyaoError, error_for_transport

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from opendata_fuyao.envelope import FuyaoEnvelope
    from opendata_fuyao.http_client import FuyaoHttpClient

#: 日 K dump 的 Parquet 列（上游契约）。
DAILY_K_COLUMNS: tuple[str, ...] = (
    "thscode",
    "currency",
    "interval",
    "adjusted",
    "date_ms",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)

#: 复权因子 dump 的 Parquet 列（上游契约）。
ADJUSTMENT_FACTOR_COLUMNS: tuple[str, ...] = (
    "thscode",
    "ticker",
    "ex_date_ms",
    "dividend_per_share",
    "per_share_bonus",
    "allotment_ratio",
    "allotment_price",
    "currency",
)

#: 中台不复权口径对应的上游 ``adjusted`` 取值（D10：入库只存不复权）。
UNADJUSTED = "none"

#: 单个 dump 的下载大小上限（超出即中断，避免磁盘被写满）。
DEFAULT_MAX_DUMP_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DumpSpec:
    """一个全市场导出的静态契约.

    Attributes:
        dump_id: 稳定标识。
        download_path: 预签名链接端点。
        description: 中文说明。
        parquet_columns: 该 dump 的 Parquet 列。
    """

    dump_id: str
    download_path: str
    description: str
    parquet_columns: tuple[str, ...]


#: 已审查的 dump 目录（未知 dump_id 失败关闭）。
DUMP_SPECS: Mapping[str, DumpSpec] = MappingProxyType(
    {
        "a_share_daily_k_1d_none_10y": DumpSpec(
            dump_id="a_share_daily_k_1d_none_10y",
            download_path="/api/dump/market-dumps/daily-k/download-url",
            description="10 年全量日 K",
            parquet_columns=DAILY_K_COLUMNS,
        ),
        "a_share_daily_k_1d_none_10d": DumpSpec(
            dump_id="a_share_daily_k_1d_none_10d",
            download_path="/api/dump/market-dumps/daily-k-10d/download-url",
            description="最近 10 交易日日 K",
            parquet_columns=DAILY_K_COLUMNS,
        ),
        "a_share_adjustment_factors_event_none_all": DumpSpec(
            dump_id="a_share_adjustment_factors_event_none_all",
            download_path="/api/dump/market-dumps/adjustment-factors/download-url",
            description="复权因子全量",
            parquet_columns=ADJUSTMENT_FACTOR_COLUMNS,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class PresignedDownload:
    """一次预签名下载（链接短期有效，不可持久化）.

    Attributes:
        url: 预签名 URL。
        expires_at: 过期时刻（上游时区感知 ISO 字符串；可缺省）。
        expires_in_seconds: 剩余有效秒数（可缺省）。
    """

    url: str
    expires_at: datetime | None = None
    expires_in_seconds: int | None = None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """判断链接是否已过期.

        Args:
            now: 覆盖"当前时刻"（测试用）。

        Returns:
            无过期信息时不判过期；否则按 ``expires_at`` 判断。
        """
        if self.expires_at is None:
            return False
        moment = now or datetime.now(timezone.utc)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= moment


@dataclass(frozen=True, slots=True)
class DumpFile:
    """已下载的 dump 文件.

    Attributes:
        path: 落地路径。
        size_bytes: 字节数。
        sha256: 内容哈希（用于导入留痕与去重）。
    """

    path: Path
    size_bytes: int
    sha256: str


def dump_spec(dump_id: str) -> DumpSpec:
    """按 dump_id 精确解析契约，绝不使用同形后备.

    Args:
        dump_id: dump 标识。

    Returns:
        契约描述。

    Raises:
        FuyaoError: 标识为空或未登记。
    """
    if not isinstance(dump_id, str) or not dump_id.strip():
        raise error_for_transport("envelope_invalid", detail="dump_id")
    spec = DUMP_SPECS.get(dump_id.strip())
    if spec is None:
        raise error_for_transport("envelope_invalid", detail="dump_unregistered")
    return spec


def parse_download_url(envelope: FuyaoEnvelope) -> PresignedDownload:
    """从 download-url 响应解析预签名链接与有效期.

    Args:
        envelope: 成功信封（``data.presigned_url`` 等）。

    Returns:
        预签名下载描述。

    Raises:
        FuyaoError: 缺少 URL 或字段类型非法。
    """
    data = envelope.data
    url = data.get("presigned_url")
    if not isinstance(url, str) or not url.strip():
        raise error_for_transport("envelope_invalid", detail="presigned_url")
    raw_expires = data.get("presigned_url_expires_at")
    expires_at: datetime | None = None
    if isinstance(raw_expires, str) and raw_expires.strip():
        try:
            expires_at = datetime.fromisoformat(raw_expires.strip())
        except ValueError as exc:
            raise error_for_transport("envelope_invalid", detail="expires_at") from exc
    raw_seconds = data.get("expires_in_seconds")
    expires_in = (
        raw_seconds if isinstance(raw_seconds, int) and not isinstance(raw_seconds, bool) else None
    )
    return PresignedDownload(url=url.strip(), expires_at=expires_at, expires_in_seconds=expires_in)


def request_download_url(client: FuyaoHttpClient, *, dump_id: str) -> PresignedDownload:
    """请求某个 dump 的预签名下载链接（不缓存）.

    Args:
        client: 传输层客户端。
        dump_id: dump 标识。

    Returns:
        预签名下载描述。
    """
    spec = dump_spec(dump_id)
    response = client.get(spec.download_path, params={})
    return parse_download_url(response.envelope)


def download_dump(
    *,
    presigned: PresignedDownload,
    dump_id: str,
    dest_dir: Path,
    max_bytes: int = DEFAULT_MAX_DUMP_BYTES,
    now: datetime | None = None,
    transport: Any = None,  # noqa: ANN401  # httpx.BaseTransport，测试注入
) -> DumpFile:
    """流式下载 dump 到本地文件（带大小上限与哈希）.

    Args:
        presigned: 预签名下载（过期即拒绝）。
        dump_id: dump 标识（决定文件名）。
        dest_dir: 落地目录（自动创建）。
        max_bytes: 大小上限，超出即中断并清理半成品。
        now: 覆盖"当前时刻"（测试用）。
        transport: 可选 httpx transport（测试注入 MockTransport）。

    Returns:
        已下载文件（路径/大小/哈希）。

    Raises:
        FuyaoError: 链接过期、下载失败或超出大小上限。
    """
    import httpx

    if presigned.is_expired(now=now):
        raise error_for_transport("timeout", detail="presigned_expired")
    spec = dump_spec(dump_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{spec.dump_id}.parquet"
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            httpx.Client(trust_env=False, timeout=60.0, transport=transport) as client,
            client.stream("GET", presigned.url) as response,
        ):
            if response.status_code >= 400:
                raise error_for_transport("http", detail=str(response.status_code))
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise error_for_transport("response_too_large", detail="dump")
                    digest.update(chunk)
                    handle.write(chunk)
    except FuyaoError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise error_for_transport("network", detail=exc.__class__.__name__) from exc
    return DumpFile(path=target, size_bytes=size, sha256=digest.hexdigest())


def _parquet_table(path: Path) -> Any:  # noqa: ANN401  # pyarrow Table
    """读取 Parquet 表（惰性依赖 pyarrow）.

    Args:
        path: Parquet 文件路径。

    Returns:
        pyarrow Table。

    Raises:
        FuyaoError: pyarrow 未安装或文件不可读。
    """
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise error_for_transport("envelope_invalid", detail="parquet_missing") from exc
    try:
        return parquet.read_table(path)
    except Exception as exc:
        raise error_for_transport("envelope_invalid", detail="parquet_unreadable") from exc


def _require_columns(columns: Sequence[str], expected: Sequence[str], *, context: str) -> None:
    """校验 Parquet 列集合与已审查契约一致（防 schema 漂移）."""
    missing = [name for name in expected if name not in columns]
    if missing:
        raise error_for_transport("envelope_invalid", detail=f"{context}_columns")


def _daily_k_bar(row: Mapping[str, Any]) -> Bar:
    """把一行日 K dump 记录转成 ``Bar``（失败关闭）.

    Args:
        row: Parquet 行.

    Returns:
        未复权日线行.

    Raises:
        FuyaoError: 行缺字段或数值非法.
    """
    from opendata_fuyao.endpoints import millis_to_trading_date

    try:
        return Bar(
            symbol=str(row["thscode"]),
            trade_date=millis_to_trading_date(row["date_ms"]),
            open=float(row["open_price"]),
            high=float(row["high_price"]),
            low=float(row["low_price"]),
            close=float(row["close_price"]),
            volume=float(row["volume"]),
            amount=float(row["turnover"]),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise error_for_transport("envelope_invalid", detail="daily_k_row") from exc


def read_daily_k_dump(path: Path, *, adjusted: str = UNADJUSTED) -> tuple[Bar, ...]:
    """解析日 K dump 为 ``Bar``（默认只取不复权，D10）.

    Args:
        path: Parquet 文件路径。
        adjusted: 目标复权口径（中台入库固定 ``none``）。

    Returns:
        未复权日线行（按标的与交易日排序）。

    Raises:
        FuyaoError: 列缺失、口径不存在或数值非法。
    """
    table = _parquet_table(path)
    columns = list(table.column_names)
    _require_columns(columns, DAILY_K_COLUMNS, context="daily_k")
    rows = table.to_pylist()
    bars = [
        _daily_k_bar(row)
        for row in rows
        if str(row.get("adjusted", "")).strip().lower() == adjusted
    ]
    if not bars:
        raise error_for_transport("envelope_invalid", detail="daily_k_empty")
    bars.sort(key=lambda bar: (bar.symbol, bar.trade_date))
    return tuple(bars)


def _adjustment_event(row: Mapping[str, Any]) -> CorporateAction:
    """把一行复权因子 dump 记录转成 ``CorporateAction``（失败关闭）.

    Args:
        row: Parquet 行.

    Returns:
        除权除息事件.

    Raises:
        FuyaoError: 行缺字段或数值非法.
    """
    from opendata_fuyao.endpoints import millis_to_trading_date

    try:
        return CorporateAction(
            symbol=str(row["thscode"]),
            ex_date=millis_to_trading_date(row["ex_date_ms"]),
            cash_dividend=float(row.get("dividend_per_share") or 0.0),
            stock_dividend=float(row.get("per_share_bonus") or 0.0),
            rights_shares=float(row.get("allotment_ratio") or 0.0),
            rights_price=float(row.get("allotment_price") or 0.0),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise error_for_transport("envelope_invalid", detail="adjustment_row") from exc


def read_adjustment_factors_dump(path: Path) -> tuple[CorporateAction, ...]:
    """解析复权因子 dump 为 ``CorporateAction``.

    字段映射（上游事实）：``dividend_per_share`` → 现金分红、
    ``per_share_bonus`` → 送股、``allotment_ratio`` → 配股比例、
    ``allotment_price`` → 配股价。

    Args:
        path: Parquet 文件路径。

    Returns:
        除权除息事件（按标的与除权日排序）。

    Raises:
        FuyaoError: 列缺失、行数值非法或结果为空。
    """
    table = _parquet_table(path)
    _require_columns(list(table.column_names), ADJUSTMENT_FACTOR_COLUMNS, context="adjustment")
    events = [_adjustment_event(row) for row in table.to_pylist()]
    if not events:
        raise error_for_transport("envelope_invalid", detail="adjustment_empty")
    events.sort(key=lambda event: (event.symbol, event.ex_date))
    return tuple(events)


__all__ = [
    "ADJUSTMENT_FACTOR_COLUMNS",
    "DAILY_K_COLUMNS",
    "DEFAULT_MAX_DUMP_BYTES",
    "DUMP_SPECS",
    "UNADJUSTED",
    "DumpFile",
    "DumpSpec",
    "PresignedDownload",
    "download_dump",
    "dump_spec",
    "parse_download_url",
    "read_adjustment_factors_dump",
    "read_daily_k_dump",
    "request_download_url",
]
