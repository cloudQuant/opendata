"""fuyao market-dumps 通道测试（A3.3）。

覆盖：dump 目录失败关闭、预签名解析（含过期）、流式下载的大小上限与哈希、
Parquet 解析为契约模型（列漂移/口径缺失/空结果 fail-closed）；真机层（``e2e``）
拉取最近 10 交易日全市场 dump 并解析。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import pytest

from opendata.data.models import Bar, CorporateAction
from opendata_fuyao import FuyaoCredentials, FuyaoError, FuyaoHttpClient
from opendata_fuyao.dumps import (
    ADJUSTMENT_FACTOR_COLUMNS,
    DAILY_K_COLUMNS,
    DEFAULT_MAX_DUMP_BYTES,
    DUMP_SPECS,
    PresignedDownload,
    download_dump,
    dump_spec,
    parse_download_url,
    read_adjustment_factors_dump,
    read_daily_k_dump,
    request_download_url,
)

if TYPE_CHECKING:
    from pathlib import Path

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc

try:  # Parquet 解析依赖 pyarrow（未安装时相关用例跳过）
    import pyarrow as pa
    import pyarrow.parquet as pq

    PYARROW_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于环境
    PYARROW_AVAILABLE = False

requires_pyarrow = pytest.mark.skipif(not PYARROW_AVAILABLE, reason="pyarrow is not installed")


def _ms(day: str) -> int:
    parsed = date.fromisoformat(day)
    return int(datetime.combine(parsed, time.min, tzinfo=SHANGHAI).timestamp() * 1000)


def _url_envelope(url: str, *, expires_in: int = 300, expires_at: str | None = None) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-dump",
            "data": {
                "presigned_url": url,
                "presigned_url_expires_at": expires_at
                or (datetime.now(UTC_TZ) + timedelta(seconds=expires_in)).isoformat(),
                "expires_in_seconds": expires_in,
            },
        }
    ).encode()


class TestDumpSpecs:
    def test_registered_dumps_are_exact(self):
        assert set(DUMP_SPECS) == {
            "a_share_daily_k_1d_none_10y",
            "a_share_daily_k_1d_none_10d",
            "a_share_adjustment_factors_event_none_all",
        }
        assert DUMP_SPECS["a_share_daily_k_1d_none_10d"].parquet_columns == DAILY_K_COLUMNS
        assert (
            DUMP_SPECS["a_share_adjustment_factors_event_none_all"].parquet_columns
            == ADJUSTMENT_FACTOR_COLUMNS
        )

    def test_unknown_dump_fails_closed(self):
        with pytest.raises(FuyaoError, match="dump_unregistered"):
            dump_spec("a_share_not_a_dump")
        with pytest.raises(FuyaoError, match="dump_id"):
            dump_spec("   ")


class TestPresignedUrl:
    def test_request_download_url_hits_the_dump_path(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, content=_url_envelope("https://o.thsi.cn/x.parquet"))

        client = FuyaoHttpClient(
            credentials=FuyaoCredentials("k", base_url="https://fuyao.test"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda seconds: None,
        )
        with client:
            presigned = request_download_url(client, dump_id="a_share_daily_k_1d_none_10d")

        assert seen["path"] == "/api/dump/market-dumps/daily-k-10d/download-url"
        assert presigned.url == "https://o.thsi.cn/x.parquet"
        assert presigned.expires_in_seconds == 300
        assert presigned.expires_at is not None
        assert not presigned.is_expired()

    def test_missing_url_is_refused(self):
        from opendata_fuyao import parse_envelope

        payload = {"code": 0, "message": "ok", "request_id": "r", "data": {}}

        with pytest.raises(FuyaoError, match="presigned_url"):
            parse_download_url(parse_envelope(payload))

    def test_expired_link_is_detected(self):
        presigned = PresignedDownload(
            url="https://o.thsi.cn/x.parquet",
            expires_at=datetime.now(UTC_TZ) - timedelta(seconds=1),
        )
        naive = PresignedDownload(
            url="https://o.thsi.cn/y.parquet", expires_at=datetime(2020, 1, 1)
        )

        assert presigned.is_expired()
        assert naive.is_expired()  # MySQL/上游返回的 naive 时间按 UTC 处理


class TestDownload:
    def test_streams_to_disk_with_hash(self, tmp_path: Path):
        payload = b"parquet-bytes" * 10

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        result = download_dump(
            presigned=PresignedDownload(url="https://o.thsi.cn/x.parquet"),
            dump_id="a_share_daily_k_1d_none_10d",
            dest_dir=tmp_path,
            transport=httpx.MockTransport(handler),
        )

        assert result.path == tmp_path / "a_share_daily_k_1d_none_10d.parquet"
        assert result.size_bytes == len(payload)
        assert result.sha256 == hashlib.sha256(payload).hexdigest()
        assert result.path.read_bytes() == payload

    def test_size_cap_aborts_and_cleans_up(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 4096)

        with pytest.raises(FuyaoError, match="RESPONSE_TOO_LARGE"):
            download_dump(
                presigned=PresignedDownload(url="https://o.thsi.cn/x.parquet"),
                dump_id="a_share_daily_k_1d_none_10d",
                dest_dir=tmp_path,
                max_bytes=64,
                transport=httpx.MockTransport(handler),
            )

        assert list(tmp_path.iterdir()) == []  # 半成品已清理

    def test_expired_link_is_refused_before_download(self, tmp_path: Path):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=b"x")

        with pytest.raises(FuyaoError, match="presigned_expired"):
            download_dump(
                presigned=PresignedDownload(
                    url="https://o.thsi.cn/x.parquet",
                    expires_at=datetime.now(UTC_TZ) - timedelta(seconds=5),
                ),
                dump_id="a_share_daily_k_1d_none_10d",
                dest_dir=tmp_path,
                transport=httpx.MockTransport(handler),
            )

        assert calls == []

    def test_http_error_is_classified(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"forbidden")

        with pytest.raises(FuyaoError) as exc:
            download_dump(
                presigned=PresignedDownload(url="https://o.thsi.cn/x.parquet"),
                dump_id="a_share_daily_k_1d_none_10d",
                dest_dir=tmp_path,
                transport=httpx.MockTransport(handler),
            )

        assert exc.value.category == "http"

    def test_default_size_cap_is_512mb(self):
        assert DEFAULT_MAX_DUMP_BYTES == 512 * 1024 * 1024


@requires_pyarrow
class TestParquetReaders:
    def _daily_k(self, tmp_path: Path, *, adjusted: str = "none") -> Path:
        table = pa.table(
            {
                "thscode": ["600519.SH", "000001.SZ"],
                "currency": ["CNY", "CNY"],
                "interval": ["1d", "1d"],
                "adjusted": [adjusted, adjusted],
                "date_ms": [_ms("2024-01-02"), _ms("2024-01-03")],
                "open_price": [1.0, 2.0],
                "high_price": [1.5, 2.5],
                "low_price": [0.5, 1.5],
                "close_price": [1.2, 2.2],
                "volume": [100.0, 200.0],
                "turnover": [120.0, 440.0],
            }
        )
        path = tmp_path / "daily_k.parquet"
        pq.write_table(table, path)
        return path

    def test_daily_k_maps_to_bars(self, tmp_path: Path):
        bars = read_daily_k_dump(self._daily_k(tmp_path))

        assert all(isinstance(bar, Bar) for bar in bars)
        assert bars[0].symbol == "000001.SZ"  # 按 (symbol, trade_date) 排序
        assert bars[0].trade_date == date(2024, 1, 3)
        assert bars[1].symbol == "600519.SH"
        assert bars[1].amount == 120.0  # turnover → amount
        assert bars[1].close == 1.2

    def test_daily_k_requires_the_requested_basis(self, tmp_path: Path):
        with pytest.raises(FuyaoError, match="daily_k_empty"):
            read_daily_k_dump(self._daily_k(tmp_path, adjusted="forward"))

    def test_daily_k_column_drift_fails_closed(self, tmp_path: Path):
        table = pa.table({"thscode": ["600519.SH"], "date_ms": [_ms("2024-01-02")]})
        path = tmp_path / "drifted.parquet"
        pq.write_table(table, path)

        with pytest.raises(FuyaoError, match="daily_k_columns"):
            read_daily_k_dump(path)

    def test_adjustment_dump_maps_to_corporate_actions(self, tmp_path: Path):
        table = pa.table(
            {
                "thscode": ["600519.SH"],
                "ticker": ["600519"],
                "ex_date_ms": [_ms("2024-06-19")],
                "dividend_per_share": [30.876],
                "per_share_bonus": [1.0],
                "allotment_ratio": [0.2],
                "allotment_price": [5.0],
                "currency": ["CNY"],
            }
        )
        path = tmp_path / "adjustment.parquet"
        pq.write_table(table, path)

        events = read_adjustment_factors_dump(path)

        assert isinstance(events[0], CorporateAction)
        assert events[0].cash_dividend == 30.876
        assert events[0].stock_dividend == 1.0
        assert events[0].rights_shares == 0.2
        assert events[0].rights_price == 5.0
        assert events[0].ex_date == date(2024, 6, 19)

    def test_adjustment_column_drift_fails_closed(self, tmp_path: Path):
        table = pa.table({"thscode": ["600519.SH"], "ex_date_ms": [_ms("2024-06-19")]})
        path = tmp_path / "drifted-adj.parquet"
        pq.write_table(table, path)

        with pytest.raises(FuyaoError, match="adjustment_columns"):
            read_adjustment_factors_dump(path)

    def test_missing_pyarrow_is_reported_clearly(self, tmp_path: Path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("pyarrow"):
                raise ImportError("no pyarrow")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        path = self._daily_k(tmp_path)
        with pytest.raises(FuyaoError, match="parquet_missing"):
            read_daily_k_dump(path)


@pytest.mark.e2e
class TestAgainstTheLiveDumps:
    """真机：拉取最近 10 交易日全市场 dump 并解析（需要 FUYAO_API_KEY）。"""

    @pytest.fixture
    def client(self):
        credentials = FuyaoCredentials.from_environment()
        if credentials is None:
            pytest.skip("FUYAO_API_KEY is not configured")
        with FuyaoHttpClient(credentials=credentials) as live:
            yield live

    def test_download_and_parse_the_recent_daily_k_dump(self, client, tmp_path: Path):
        presigned = request_download_url(client, dump_id="a_share_daily_k_1d_none_10d")

        assert presigned.url.startswith("https://")
        assert presigned.expires_in_seconds is not None and presigned.expires_in_seconds <= 600

        result = download_dump(
            presigned=presigned,
            dump_id="a_share_daily_k_1d_none_10d",
            dest_dir=tmp_path,
        )
        bars = read_daily_k_dump(result.path)

        assert result.size_bytes > 0
        assert len(result.sha256) == 64
        assert len(bars) > 1000  # 全市场 10 个交易日
        assert {bar.symbol[-3:] for bar in bars} >= {".SH", ".SZ"}

    def test_download_and_parse_the_adjustment_factor_dump(self, client, tmp_path: Path):
        presigned = request_download_url(
            client, dump_id="a_share_adjustment_factors_event_none_all"
        )
        result = download_dump(
            presigned=presigned,
            dump_id="a_share_adjustment_factors_event_none_all",
            dest_dir=tmp_path,
        )
        events = read_adjustment_factors_dump(result.path)

        assert events
        # 上游含已公告但尚未除权的未来除权日，故只要求存在历史事件与日期合法。
        assert any(event.ex_date <= date.today() for event in events)
        assert all(event.ex_date.year >= 1990 for event in events)
