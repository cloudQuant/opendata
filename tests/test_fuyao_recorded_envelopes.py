"""T1 档基线（``代码质量规范.md`` §5.2）：断言输入是**录制回来的真实信封**。

§5.2 对 T1（``opendata_fuyao/``、``opendata/data/http_client.py``、契约层核心 Fetcher）
要求三件事：①错误翻译用真实响应信封样例，≥3 用例且含「成功不抛」；②认证头/参数构造
断言**预计算值**；③normalize 用真实报文逐字段断言，≥3 用例。同一节末尾钉死了来源：
*fuyao 用官方文档示例 + 联调录制，禁止手造「理想报文」替代真实样本*。

在 C44 之前这一档一件都不成立：``tests/`` 里没有任何一份扶摇报文落过盘，错误翻译用例
的输入全是测试自己写的 ``{"code": 2001}``。真实录制的第一个读数就把这套手造样本证伪了
——无效/吊销的 Key 上游回的是 **``code=2003``**（``Invalid or revoked API key``），
而分类表把 2001 记作 ``auth``、2003 记作 ``permission``，于是文案会让人去谈采购权限，
而不是去换 Key。这一条写在 :meth:`TestErrorTranslation.bad_credential_is_the_recorded_shape`。

夹具由 ``scripts/ops/fuyao_envelope_recorder.py`` 联调录制（read-only GET）产生，
每个用例都重算 ``body_sha256`` ⇒ 档案被手改过就红。夹具缺失是用例报错而不是跳过：
一个不存在的真实样本面不能被读成「已通过」。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from opendata_fuyao.credentials import FuyaoCredentials
from opendata_fuyao.endpoints import (
    PRICES_ENDPOINT,
    build_prices_request,
    normalize_bars,
    normalize_calendar,
    normalize_instruments,
    shanghai_midnight_millis,
)
from opendata_fuyao.envelope import parse_envelope
from opendata_fuyao.errors import FuyaoError
from opendata_fuyao.http_client import API_KEY_HEADER, FuyaoHttpClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "upstream" / "fuyao_t1_envelopes"
#: 录制件里的标的与交易所（断言逐字段时作为期望值的一部分，必须由测试显式给出）。
RECORDED_SYMBOL = "600519.SH"
RECORDED_CALENDAR_EXCHANGE = "CN-SSE"
#: 录制用的黄金向量：上海零点毫秒戳（2024-01-02 / 2024-01-12 的前一日末）。
PRICES_START_MILLIS = 1_704_124_800_000
PRICES_END_MILLIS = 1_704_988_799_999


def _records() -> dict[str, dict[str, Any]]:
    """Load the recorded transcript keyed by case name.

    Returns:
        Every recorded case as a mapping.

    Raises:
        FileNotFoundError: The recording plane is missing entirely.
    """
    archive = FIXTURE_DIR / "responses.json.gz"
    with gzip.open(archive) as handle:
        payload = json.loads(handle.read())
    indexed = {str(entry["name"]): entry for entry in payload}
    if not indexed:
        raise FileNotFoundError("the fuyao transcript holds no case")
    return indexed


def _body(case: str) -> bytes:
    """Return the recorded response body for one case.

    Args:
        case: The recorded case name.

    Returns:
        The bytes exactly as they came off the wire.
    """
    return str(_records()[case]["body_text"]).encode("utf-8")


def _payload(case: str) -> dict[str, Any]:
    """Return the recorded JSON body as a mapping.

    Args:
        case: The recorded case name.

    Returns:
        The parsed response body.
    """
    parsed: dict[str, Any] = json.loads(_body(case))
    return parsed


def _client_for(case: str) -> FuyaoHttpClient:
    """Build the real client with the recorded response wired in.

    Args:
        case: The recorded case to serve.

    Returns:
        A client whose transport answers with the recorded body and status.
    """
    record = _records()[case]
    status = int(record["status_code"])
    body = _body(case)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    return FuyaoHttpClient(
        credentials=FuyaoCredentials("c44-replay-key", base_url="https://fuyao.recorded"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
    )


class TestRecordingIsReal:
    """The fixture plane must be evidence, not a hand-written expectation."""

    def test_every_body_still_hashes_to_what_was_recorded(self) -> None:
        """重算 sha：档案被手改过（哪怕只改一个 ``code``）立刻红。"""
        for name, record in _records().items():
            assert record["truncated"] is False, name
            digest = hashlib.sha256(_body(name)).hexdigest()
            assert digest == record["body_sha256"], name

    def test_the_archive_keeps_only_response_headers_that_cannot_hold_a_key(self) -> None:
        """档案只留 content-type/content-length：凭据既不进请求头记录，也不留回显头。"""
        kept = {"content-type", "content-length"}
        blob = gzip.decompress((FIXTURE_DIR / "responses.json.gz").read_bytes()).decode("utf-8")

        assert API_KEY_HEADER.lower() not in blob.lower()
        for name, record in _records().items():
            assert set(record["headers"]) <= kept, name

    def test_the_credential_the_recorder_presented_is_not_in_the_archive(self) -> None:
        """录制时递出去的那份假凭据也不在档案里——落盘前那道脱敏检查有反事实可复算。"""
        blob = gzip.decompress((FIXTURE_DIR / "responses.json.gz").read_bytes()).decode("utf-8")

        assert "c44-recorder-not-a-real-credential" not in blob
        assert "c44-replay-key" not in blob


class TestErrorTranslation:
    """T1① —— 真实信封 → 稳定分类（≥3 用例，含成功不抛）。"""

    @pytest.mark.parametrize(
        ("case", "endpoint", "upstream_code", "category"),
        [
            ("error_window_too_long", PRICES_ENDPOINT, 1003, "request"),
            ("error_blank_ticker", PRICES_ENDPOINT, 1002, "request"),
            ("error_unknown_ticker", PRICES_ENDPOINT, 1002, "request"),
        ],
    )
    def test_recorded_business_code_translates_as_measured(
        self, case: str, endpoint: str, upstream_code: int, category: str
    ) -> None:
        """参数被上游拒绝时，客户端给出的分类与录制读数一致且不可重试。"""
        with pytest.raises(FuyaoError) as caught:
            _client_for(case).get(endpoint, params=_records()[case]["params"])

        assert caught.value.upstream_code == upstream_code
        assert caught.value.category == category
        assert caught.value.retryable is False

    def test_bad_credential_is_the_recorded_shape(self) -> None:
        """无效 Key 的真实信封是 ``2003``，而分类表把 2001 当作鉴权码。

        这条断言写的是**观测到的**行为：不可重试、分类为 ``permission``。
        分类不能凭一次观测就改（2003 也覆盖真的权限不足），能改的是文案：
        最后一行钉住「第一步去换 Key」，否则这段人话退回「去谈采购」就没人发现了。
        """
        with pytest.raises(FuyaoError) as caught:
            _client_for("error_bad_credential").get(PRICES_ENDPOINT)

        assert caught.value.upstream_code == 2003
        assert caught.value.category == "permission"
        assert caught.value.retryable is False
        assert "Invalid or revoked API key" in _payload("error_bad_credential")["message"]
        assert "FUYAO_API_KEY" in caught.value.advice

    def test_recorded_request_id_survives_the_translation(self) -> None:
        """对账用的 ``request_id`` 必须穿过翻译层，而不是只活在原始报文里。"""
        recorded = _payload("error_window_too_long")["request_id"]

        with pytest.raises(FuyaoError) as caught:
            _client_for("error_window_too_long").get(PRICES_ENDPOINT)

        assert caught.value.request_id == recorded

    def test_http_404_body_is_translated_as_http(self) -> None:
        """非 2xx 走 ``http`` 分类，且 detail 带上真实状态码。"""
        with pytest.raises(FuyaoError) as caught:
            _client_for("error_unknown_endpoint").get("/api/meta/this-endpoint-does-not-exist")

        assert caught.value.category == "http"
        assert caught.value.retryable is False
        assert "404" in str(caught.value)

    def test_success_envelope_does_not_raise(self) -> None:
        """§5.2 里那句「含成功不抛」：同一条通路收到 code=0 时返回结果。"""
        result = _client_for("success_prices").get(PRICES_ENDPOINT)

        assert result.status_code == 200
        assert len(result.envelope.items) == 8


class TestGoldenVectors:
    """T1② —— 认证头与请求参数断言预计算值（不复用被测实现算期望）。"""

    def test_the_key_travels_in_the_recorded_header_and_nowhere_else(self) -> None:
        """凭据头名与取值来自录制档案里的同一形态，期望值写成字面量。"""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_body("success_prices"))

        client = FuyaoHttpClient(
            credentials=FuyaoCredentials("c44-golden-key", base_url="https://fuyao.recorded"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda seconds: None,
        )
        client.get(PRICES_ENDPOINT, params={"thscode": RECORDED_SYMBOL})

        assert seen[0].headers["X-api-key"] == "c44-golden-key"
        assert "authorization" not in seen[0].headers

    def test_prices_request_equals_precomputed_millis(self) -> None:
        """整个参数字典与字面量比对：毫秒戳、``interval``、``adjust`` 一个都不放过。"""
        params = build_prices_request(
            symbol=RECORDED_SYMBOL,
            start=date(2024, 1, 2),
            end=date(2024, 1, 12),
            adjust="unadjusted",
        )

        assert params == {
            "thscode": "600519.SH",
            "interval": "1d",
            "start": PRICES_START_MILLIS,
            "end": PRICES_END_MILLIS,
            "adjust": "none",
        }

    def test_midnight_millis_golden_vector_matches_the_recording(self) -> None:
        """窗口两端各一条预计算值，``end`` 的「次日零点减一毫秒」口径同样钉住。"""
        assert shanghai_midnight_millis(date(2024, 1, 2)) == PRICES_START_MILLIS
        assert shanghai_midnight_millis(date(2024, 1, 12)) - 1 == PRICES_END_MILLIS
        assert shanghai_midnight_millis(date(1990, 1, 1)) == 631_123_200_000


class TestNormalizeRecordedPayloads:
    """T1③ —— 真实报文 → 标准化模型逐字段断言（≥3 用例）。"""

    def test_recorded_bars_map_field_by_field(self) -> None:
        """第一根与最后一根的八个字段逐个等于录制读数。"""
        bars = normalize_bars(parse_envelope(_payload("success_prices")), symbol=RECORDED_SYMBOL)

        assert len(bars) == 8
        assert bars[0].symbol == "600519.SH"
        assert bars[0].trade_date == date(2024, 1, 2)
        assert bars[0].open == 1715.0
        assert bars[0].high == 1718.19
        assert bars[0].low == 1678.1
        assert bars[0].close == 1685.01
        assert bars[0].volume == 3215644.0
        assert bars[0].amount == 5440082548.08
        assert bars[-1].trade_date == date(2024, 1, 11)
        assert bars[-1].close == 1646.57

    def test_recorded_instrument_maps_field_by_field(self) -> None:
        """标的检索的真实报文里 ``list_date`` 是字符串、三个日期字段为 null。"""
        instruments = normalize_instruments(parse_envelope(_payload("success_tickers_search")))

        assert len(instruments) == 1
        found = instruments[0]
        assert found.symbol == "600519.SH"
        assert found.exchange == "SH"
        assert found.name == "贵州茅台"
        assert found.status == "active"
        assert found.currency == "CNY"
        assert found.list_date == date(2001, 8, 27)
        assert found.delist_date is None
        assert found.board is None

    def test_recorded_calendar_maps_field_by_field(self) -> None:
        """日历真实报文 240 行、``date`` 是 ``YYYYMMDD`` 字符串、上游不含休市行。"""
        days = normalize_calendar(
            parse_envelope(_payload("success_calendar")), exchange=RECORDED_CALENDAR_EXCHANGE
        )

        assert len(days) == 240
        assert days[0].exchange == "CN-SSE"
        assert days[0].date == date(2025, 9, 29)
        assert days[0].is_open is True
        assert days[1].date == date(2025, 9, 30)
        assert days[-1].date == date(2026, 9, 24)

    def test_a_pre_listing_window_normalizes_to_nothing_without_raising(self) -> None:
        """真实读数：上市前的窗口不报 3001，而是 ``code=0`` + 空行 + ``timestamp: null``。"""
        envelope = parse_envelope(_payload("probe_prices_before_listing"))

        assert envelope.data_timestamp_ms is None
        assert normalize_bars(envelope, symbol=RECORDED_SYMBOL) == ()


class TestEveryEndpointPlaneIsCovered:
    """三条 T1 通路（行情 / 目录 / 日历）都要有真实样本，缺一即红。"""

    def test_the_three_recorded_paths_cover_the_three_t1_shapes(self) -> None:
        """行情、标的、日历各一份成功报文，另加四类错误信封。"""
        covered = set(_records())

        assert {
            "success_prices",
            "success_tickers_search",
            "success_calendar",
        } <= covered
        assert {
            "error_window_too_long",
            "error_blank_ticker",
            "error_unknown_ticker",
            "error_bad_credential",
            "error_unknown_endpoint",
        } <= covered

    def test_the_recording_declares_its_provenance(self) -> None:
        """``meta.json`` 必须说清是谁、什么时候、怎么录的——否则档案等于手造样本。"""
        meta = json.loads((FIXTURE_DIR / "meta.json").read_text(encoding="utf-8"))

        assert meta["recorder"] == "scripts/ops/fuyao_envelope_recorder.py"
        assert "联调录制" in meta["source"]
        assert len(meta["cases"]) >= 8
