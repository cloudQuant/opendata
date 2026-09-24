"""Fail-closed guards for the ported tree's silent-empty divergences (C11a).

Two ported index functions swallow transport failures and return an empty
frame, which incremental scheduling cannot tell apart from a legitimately
empty window (holiday / weekend-only ranges). These tests pin the registered
manual edits in ``opendata_http/index/*``, so a re-port that replays pristine
upstream code turns red instead of silently under-fetching.

The distinction each test protects: *refusal* (no candidate answered) raises,
*answered but empty* stays an empty frame.
"""

import io
import json
from pathlib import Path

import pandas as pd
import pytest
import requests

from opendata.data.providers.akshare.models.index_constituent import (
    AkshareIndexConstituentFetcher,
)
from opendata.data.providers.akshare.models.index_daily import AkshareIndexDailyFetcher
from opendata_http.index import index_cons, index_zh_em
from opendata_http.utils import request as em_request

#: Columns the ported index kline frame publishes (upstream Chinese names).
KLINE_COLUMNS = (
    "日期",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
)


def _raw_response(content: bytes, status_code: int = 200) -> requests.Response:
    """Build a ``requests`` response with a raw body."""
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    return response


def _json_response(payload: object, status_code: int = 200) -> requests.Response:
    """Build a ``requests`` response carrying ``payload`` as JSON body."""
    response = _raw_response(json.dumps(payload).encode("utf-8"), status_code)
    response.headers["Content-Type"] = "application/json"
    return response


def _xlsx(frame: pd.DataFrame) -> bytes:
    """Serialize a frame as the in-memory workbook csindex serves."""
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()


@pytest.fixture
def pure_requests_channel(monkeypatch):
    """Keep the eastmoney helper on ``requests`` only (never spawn curl)."""
    monkeypatch.setenv("AKSHARE_EASTMONEY_CURL_INTERFACE", "0")
    monkeypatch.setenv("AKSHARE_EASTMONEY_AUTO_CURL_INTERFACE", "0")
    em_request._reset_eastmoney_fallback_cache()
    yield
    em_request._reset_eastmoney_fallback_cache()


class TestIndexDailyHistFailClosed:
    """``index_zh_a_hist`` (domain ``index_daily``)."""

    @pytest.fixture(autouse=True)
    def _stub_secid_lookup(self, monkeypatch):
        """Skip the code->market probe; the candidate list is what fails closed."""
        monkeypatch.setattr(index_zh_em, "index_code_id_map_em", lambda: {})

    def test_raises_when_no_candidate_answers(self, monkeypatch, pure_requests_channel):
        def refuse(*args, **kwargs):
            raise requests.ConnectionError("Empty reply from server")

        monkeypatch.setattr(requests, "get", refuse)

        with pytest.raises(RuntimeError, match="index kline"):
            index_zh_em.index_zh_a_hist(symbol="000300")

    def test_clean_empty_answer_stays_an_empty_frame(self, monkeypatch, pure_requests_channel):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _json_response({"data": None}))

        frame = index_zh_em.index_zh_a_hist(symbol="000300")

        assert frame.empty
        assert list(frame.columns) == list(KLINE_COLUMNS)

    def test_parses_klines_when_a_candidate_answers(self, monkeypatch, pure_requests_channel):
        row = "2024-01-02,3000.1,3010.2,3020.3,2990.4,123456,789012,1.0,0.5,10.1,0.2"
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: _json_response({"data": {"klines": [row]}})
        )

        frame = index_zh_em.index_zh_a_hist(symbol="000300")

        assert len(frame) == 1
        assert frame["收盘"].iloc[0] == pytest.approx(3010.2)

    def test_fetcher_propagates_refusal_instead_of_returning_empty(
        self, monkeypatch, pure_requests_channel
    ):
        def refuse(*args, **kwargs):
            raise requests.ConnectionError("RemoteDisconnected")

        monkeypatch.setattr(requests, "get", refuse)

        with pytest.raises(RuntimeError, match="index kline"):
            AkshareIndexDailyFetcher().fetch(symbol="000300")


class TestIndexConstituentWeightFailClosed:
    """``index_stock_cons_weight_csindex`` (domain ``index_constituent``)."""

    def test_transport_refusal_raises(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise requests.ConnectionError("Connection reset by peer")

        monkeypatch.setattr(index_cons.requests, "get", refuse)

        with pytest.raises(RuntimeError, match="closeweight"):
            index_cons.index_stock_cons_weight_csindex(symbol="000300")

    def test_http_error_status_raises(self, monkeypatch):
        monkeypatch.setattr(
            index_cons.requests,
            "get",
            lambda *a, **k: _raw_response(b"<html>gateway</html>", status_code=503),
        )

        with pytest.raises(RuntimeError, match="503"):
            index_cons.index_stock_cons_weight_csindex(symbol="000300")

    def test_unreadable_body_raises(self, monkeypatch):
        monkeypatch.setattr(
            index_cons.requests, "get", lambda *a, **k: _raw_response(b"<html>not an xls</html>")
        )

        with pytest.raises(RuntimeError, match="not readable"):
            index_cons.index_stock_cons_weight_csindex(symbol="000300")

    def test_parseable_but_rowless_stays_empty(self, monkeypatch):
        monkeypatch.setattr(
            index_cons.requests, "get", lambda *a, **k: _raw_response(_xlsx(pd.DataFrame()))
        )

        frame = index_cons.index_stock_cons_weight_csindex(symbol="000300")

        assert frame.empty

    def test_fetcher_propagates_refusal_instead_of_returning_empty(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise requests.ConnectionError("Connection reset by peer")

        monkeypatch.setattr(index_cons.requests, "get", refuse)

        with pytest.raises(RuntimeError, match="closeweight"):
            AkshareIndexConstituentFetcher().fetch(symbol="000300")


def test_fail_closed_patch_survives_in_the_ported_tree():
    """A re-port that replays pristine upstream code must trip this."""
    zh_em = Path(index_zh_em.__file__).read_text(encoding="utf-8")
    assert "request_eastmoney(url, params=params" in zh_em
    assert "except (requests.RequestException, ValueError):\n            continue" not in zh_em

    cons = Path(index_cons.__file__).read_text(encoding="utf-8")
    assert "is not readable as XLS" in cons
    assert cons.count("return _empty_index_stock_cons_weight_csindex()") == 2
