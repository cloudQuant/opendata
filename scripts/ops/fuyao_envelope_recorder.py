"""C44: record *real* fuyao envelopes so the T1 tests stop inventing them.

``代码质量规范.md`` §5.2 T1 档要求「错误翻译：真实响应信封样例」「normalize：真实报文」，
而 §5.2 末尾把来源钉死了：*"fuyao 用官方文档示例 + 联调录制 …… 禁止手造理想报文替代真实样本"*。
仓库里从来没有一份扶摇报文落过盘（``tests/fixtures/upstream/`` 只有 providers 侧的录制件），
所以 T1 的错误翻译用例全部建立在测试自己写的 ``{"code": 2001}`` 上——
上游换个字段名、把 ``message`` 改成 ``msg``、或返回一个映射以外的 ``data``，
这些用例一个字都不会变红。

这个脚本做联调录制那一半，read-only：

1. **成功报文**：日线与标的检索各取一份小窗口，``normalize_*`` 的逐字段断言以此输入。
2. **业务错误**：空数据窗口、超过十年的窗口、空标的、鉴权用一份显然无效的凭据。
   这四条是上游自己发回来的 ``code``，不是测试猜的。
3. **HTTP 层错误**：一个映射表里不存在的端点（预期 404），用于 ``http``/``envelope_invalid`` 翻译。

落盘的是 ``status`` + 原始响应体 + 请求参数（**去掉凭据**），外加 ``sha256`` 与录制时刻。
脚本从不打印响应正文，也从不调用 ``repr`` 于任何含凭据的对象；写盘前逐字段确认凭据字符串
没有出现在待写记录里，出现即中止（``REDACTION_LEAK``）——落盘档案是要进 git 的。

Usage (py313 env; needs network and the fuyao credential for ``.env``):

    python scripts/ops/fuyao_envelope_recorder.py [--out tests/fixtures/upstream]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 秒。上游对连发直接断连（C15 实测），两次请求之间必须留开。
PACE_SECONDS = 12.0
#: 断连后的重试间隔与次数：录制跑不完就没有档案，一断就废不可接受。
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 4
#: 单条响应体的落盘上限（字节）：窗口本来就小，超了说明参数写错。
MAX_RECORDED_BYTES = 240_000
#: 落盘时保留的响应头（其余可能含凭据或网关信息，一律不记）。
KEPT_HEADERS: tuple[str, ...] = ("content-type", "content-length")
#: 一份显然无效的凭据：只用于取回上游真实的鉴权错误信封。
BOGUS_CREDENTIAL_SENTINEL = "c44-recorder-not-a-real-credential"

#: 日线端点的取样窗口（10 个交易日，够 normalize 逐字段断言，又留在跨度上限内）。
PRICES_SYMBOL = "600519.SH"
PRICES_START = date(2024, 1, 2)
PRICES_END = date(2024, 1, 12)


class RecordingError(RuntimeError):
    """录制面自身的失败：凭据缺失、脱敏不通过、或响应体超限."""


def _cases() -> list[dict[str, Any]]:
    """Return the envelope shapes worth recording.

    Parameters come from the same production builders the client uses, so the recording
    cannot drift from the request shape the adapters actually send.

    Returns:
        One entry per request: fixture name, endpoint, query parameters, and whether the
        real credential or the bogus one should be presented.
    """
    from opendata_fuyao.endpoints import (
        CALENDAR_ENDPOINT,
        build_prices_request,
        build_tickers_search_request,
        shanghai_midnight_millis,
    )

    prices = build_prices_request(
        symbol=PRICES_SYMBOL,
        start=PRICES_START,
        end=PRICES_END,
        adjust="unadjusted",
    )
    return [
        {
            "name": "success_prices",
            "endpoint": "/api/a-share/prices/historical",
            "params": dict(prices),
            "bogus": False,
            "expect": "code=0 with at least one bar",
        },
        {
            "name": "success_tickers_search",
            "endpoint": "/api/meta/tickers/search",
            "params": build_tickers_search_request(query="600519", limit=5),
            "bogus": False,
            "expect": "code=0 with at least one instrument",
        },
        {
            "name": "success_calendar",
            "endpoint": CALENDAR_ENDPOINT,
            "params": {},
            "bogus": False,
            "expect": "code=0 with trading days",
        },
        {
            "name": "probe_prices_before_listing",
            "endpoint": "/api/a-share/prices/historical",
            "params": {
                **prices,
                "start": shanghai_midnight_millis(date(1990, 1, 1)),
                "end": shanghai_midnight_millis(date(1990, 1, 6)) - 1,
            },
            "bogus": False,
            "expect": "empty: is it code=0 with no rows, or 3001/3002 (only live answers this)",
        },
        {
            "name": "error_window_too_long",
            "endpoint": "/api/a-share/prices/historical",
            "params": {**prices, "start": shanghai_midnight_millis(date(2000, 1, 1))},
            "bogus": False,
            "expect": "1003 (window beyond the 10-year cap)",
        },
        {
            "name": "error_blank_ticker",
            "endpoint": "/api/a-share/prices/historical",
            "params": {**prices, "thscode": ""},
            "bogus": False,
            "expect": "1001/1002 (parameter rejected upstream, not locally)",
        },
        {
            "name": "error_unknown_ticker",
            "endpoint": "/api/a-share/prices/historical",
            "params": {**prices, "thscode": "999999.ZZ"},
            "bogus": False,
            "expect": "3001/1001 (no such instrument)",
        },
        {
            "name": "error_bad_credential",
            "endpoint": "/api/a-share/prices/historical",
            "params": dict(prices),
            "bogus": True,
            "expect": "2001 or an HTTP 401/403 body",
        },
        {
            "name": "error_unknown_endpoint",
            "endpoint": "/api/meta/this-endpoint-does-not-exist",
            "params": {},
            "bogus": False,
            "expect": "HTTP 404, probably not the JSON envelope",
        },
    ]


def _present(case: dict[str, Any]) -> tuple[str, str]:
    """Resolve ``(base_url, credential)`` for one case, failing closed.

    Args:
        case: The case being recorded.

    Returns:
        The API root and the header value to present.

    Raises:
        RecordingError: No credential is configured and the case needs one.
    """
    from opendata.data.providers.ths.models._client import credentials as resolve

    if case["bogus"]:
        from opendata_fuyao.credentials import DEFAULT_FUYAO_API_BASE_URL

        return DEFAULT_FUYAO_API_BASE_URL, BOGUS_CREDENTIAL_SENTINEL
    active = resolve()
    return active.base_url, active.api_key


def _record_one(case: dict[str, Any]) -> dict[str, Any]:
    """Issue one read-only request and describe the response without printing it.

    Args:
        case: The case to record.

    Returns:
        A JSON-safe record: request metadata, status, raw body text, and a digest.

    Raises:
        RecordingError: The credential appears anywhere in the record about to be written.
    """
    import httpx

    from opendata_fuyao.http_client import API_KEY_HEADER

    base_url, presented = _present(case)
    url = f"{base_url}{case['endpoint']}"
    last: str | None = None
    for _attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=20.0) as session:
                response = session.get(
                    url, params=dict(case["params"]), headers={API_KEY_HEADER: presented}
                )
        except httpx.HTTPError as exc:
            last = exc.__class__.__name__
            print(f"[retry] {case['name']}: {last} after {RETRY_SECONDS}s")
            time.sleep(RETRY_SECONDS)
            continue
        body = response.content[:MAX_RECORDED_BYTES]
        record: dict[str, Any] = {
            "name": case["name"],
            "expectation": case["expect"],
            "endpoint": case["endpoint"],
            "params": dict(case["params"]),
            "status_code": response.status_code,
            "headers": {
                key: response.headers[key] for key in KEPT_HEADERS if key in response.headers
            },
            "body_text": body.decode("utf-8", errors="replace"),
            "body_bytes": len(response.content),
            "body_sha256": hashlib.sha256(response.content).hexdigest(),
            "truncated": len(response.content) > MAX_RECORDED_BYTES,
            "recorded_at": date.today().isoformat(),
        }
        leak = presented != BOGUS_CREDENTIAL_SENTINEL and presented in json.dumps(
            record, ensure_ascii=False
        )
        if leak:
            raise RecordingError(f"REDACTION_LEAK: {case['name']}")
        code: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = payload.get("code")
        print(
            f"[ok] {case['name']}: http={response.status_code} code={code} "
            f"bytes={len(response.content)} sha={record['body_sha256'][:12]}"
        )
        return record
    raise RecordingError(f"NO_RESPONSE: {case['name']} after {last}")


def record(out_dir: Path) -> int:
    """Record every case into ``<out_dir>/fuyao_t1_envelopes/``.

    Args:
        out_dir: The fixture root, normally ``tests/fixtures/upstream``.

    Returns:
        ``0`` when all cases answered, ``1`` when at least one did not.
    """
    case_dir = out_dir / "fuyao_t1_envelopes"
    case_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    failures = 0
    for index, case in enumerate(_cases()):
        if index:
            time.sleep(PACE_SECONDS)
        try:
            records.append(_record_one(case))
        except RecordingError as exc:
            failures += 1
            print(f"[fail] {exc}")
    payload = gzip.compress(
        (json.dumps(records, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )
    (case_dir / "responses.json.gz").write_bytes(payload)
    meta = {
        "source": "fuyao REST, 联调录制（read-only GET）",
        "recorder": "scripts/ops/fuyao_envelope_recorder.py",
        "recorded_at": date.today().isoformat(),
        "cases": [entry["name"] for entry in records],
        "credential": "不落盘；请求头里的值在写盘前逐条比对过，出现即中止",
        "note": "truncated 为 true 表示只落了前 240000 字节，sha 仍是全体的",
    }
    (case_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"RECORDED cases={len(records)} failures={failures} -> {case_dir}")
    return 0 if failures == 0 and records else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command line arguments after the program name.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "upstream"))
    arguments = parser.parse_args(argv)
    try:
        return record(Path(arguments.out))
    except RecordingError as exc:
        print(f"ABORT: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
