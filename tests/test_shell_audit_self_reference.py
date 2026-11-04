"""AC-17|08 面①（自指）判据自己的反事实：三类真自指还能红，五类误伤不再红。

``docs/evidence/C44/shell_audit.py`` 的 ``self_reference_findings()`` 第一版整条 assert 走一遍，
只看「出现过本地算子」+「出现过生产裸名」。那两层都不分操作数：

* ``assert adapter.parse(_worksheet(...)) == {'code': '...'}`` —— ``_worksheet`` 只是在造输入，
  期望值写的是字面量，却被判成自指（假阳性，本轮 11 处命中全是这一形）；
* ``expected = _ms(day)`` 之后 ``assert result == expected`` —— 期望值确实先被本地重算过，
  但断言句里没有本地算子的调用，旧判据看不见（假阴性）；
* ``fetcher = YieldCurveFetcher(client)`` 之后 ``assert _ms(day) == fetcher.millis_for(...)`` ——
  生产藏在属性链后面，句中没有生产裸名，旧判据同样看不见（假阴性）。

判据面改成了按比较的操作数各判一次，所以这次改动本身要能红能复算：下面每一条都在临时
源码上钉一个读数。命中数为 0 的那几条是**正控制**（黄金向量、造输入的本地 builder、
拼路径读档案的本地函数、把生产输出做差的行为判据）——把它们点红只会把作者推向
§5.2 T1② 明令要求的预计算断言。规则原文与代价见 ``docs/evidence/C44/README.md`` 第三节。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
CENSUS = REPO_ROOT / "docs" / "evidence" / "C44" / "shell_audit.py"

#: 面①用的本地算子：把日期换成上海零点的毫秒戳——与被测实现同一件事，另算一遍。
_MS_HELPER = """
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from opendata_fuyao.endpoints import YieldCurveFetcher, shanghai_midnight_millis


def _ms(day: str) -> int:
    zone = ZoneInfo("Asia/Shanghai")
    midnight = datetime.combine(date.fromisoformat(day), time.min, tzinfo=zone)
    return int(midnight.timestamp() * 1000)
"""


def _load_census() -> ModuleType:
    """Import ``shell_audit.py`` by path (it is evidence, not a package module)."""
    spec = importlib.util.spec_from_file_location("shell_audit", CENSUS)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with the round
        raise RuntimeError(f"cannot load the census at {CENSUS}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def census() -> ModuleType:
    """The census face under test."""
    return _load_census()


def _hits(census: ModuleType, source: str) -> list[str]:
    """Self-reference findings over one synthetic module, as ``line: detail`` strings."""
    tree = ast.parse(source)
    findings = census.self_reference_findings(Path("synthetic/test_shell.py"), tree)
    return [f"{finding.line}: {finding.detail}" for finding in findings]


def test_direct_recomputation_still_fires(census: ModuleType) -> None:
    """C44 当年的那一形（本地重算毫秒戳 vs 生产函数）必须继续命中：收紧不是靠把它关掉."""
    source = (
        _MS_HELPER
        + """

def test_millis_is_recomputed() -> None:
    assert _ms("2024-01-02") == shanghai_midnight_millis(date(2024, 1, 2))
"""
    )
    assert len(_hits(census, source)) == 1


def test_expectation_stored_in_a_local_still_fires(census: ModuleType) -> None:
    """假阴性①：期望值先写进普通局部变量，断言句里没有本地算子——顺着用定义链还是要红."""
    source = (
        _MS_HELPER
        + """

def test_millis_via_local() -> None:
    result = shanghai_midnight_millis(date(2024, 1, 2))
    expected = _ms("2024-01-02")
    assert result == expected
"""
    )
    assert len(_hits(census, source)) == 1


def test_production_behind_an_attribute_chain_still_fires(census: ModuleType) -> None:
    """假阴性②：生产只出现在 ``fetcher.millis_for(...)`` 的属性链后面，句中没有生产裸名."""
    source = (
        _MS_HELPER
        + """

def test_millis_via_attribute() -> None:
    fetcher = YieldCurveFetcher(None)
    assert _ms("2024-01-02") == fetcher.millis_for(date(2024, 1, 2))
"""
    )
    assert len(_hits(census, source)) == 1


def test_input_builder_with_literal_expectation_does_not_fire(census: ModuleType) -> None:
    """假阳性①（本轮 11 处的主形）：本地 builder 只在造输入，期望值是字面量."""
    source = """
from datetime import datetime

from opendata.data.providers.szse import lifecycle_xlsx_adapter as adapter


def _worksheet(day: str) -> bytes:
    stamp = int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
    return f'<worksheet rows="{stamp}"/>'.encode()


def test_header_contract_mismatch() -> None:
    assert adapter.parse_worksheet(_worksheet("2024-01-02"), ()) == {
        "code": "header_contract_mismatch"
    }
"""
    assert _hits(census, source) == []


def test_empty_expectation_after_a_built_context_does_not_fire(census: ModuleType) -> None:
    """假阳性②：``_context(...)`` 是给 fetch 喂的请求上下文，期望值是空列表字面量."""
    source = """
from datetime import datetime, timedelta, timezone

from opendata_fuyao.budget import YieldCurveFetcher


def _context(model: str) -> object:
    return {"model": model, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}


def test_empty_date_list_returns_without_io() -> None:
    assert YieldCurveFetcher(None).fetch(ctx=_context("YieldCurve"), date=[]) == []
"""
    assert _hits(census, source) == []


def test_precomputed_golden_vector_does_not_fire(census: ModuleType) -> None:
    """§5.2 T1② 要求的写法（把期望值预先算好写成字面量）不能被判成自指."""
    source = (
        _MS_HELPER
        + """

def test_golden_millis() -> None:
    assert shanghai_midnight_millis(date(2024, 1, 2)) == 1704124800000
"""
    )
    assert _hits(census, source) == []


def test_ledger_reader_is_not_a_recomputation(census: ModuleType) -> None:
    """拼路径读档案的本地函数是「查表」，不是「算数量」：``/`` 在这里不重算任何东西."""
    source = """
from pathlib import Path

from opendata.data.providers.congress import specs

REPO_ROOT = Path(".")
LEDGER_RELATIVE_PATH = "docs/ledger.json"


def load_ledger(path: Path) -> list[dict[str, str]]:
    return []


def ledger_rows() -> list[str]:
    return [row["upstream_model"] for row in load_ledger(REPO_ROOT / LEDGER_RELATIVE_PATH)]


def test_package_rows_are_the_ledger_rows() -> None:
    assert sorted(specs.UPSTREAM_ROWS) == sorted(ledger_rows())
"""
    assert _hits(census, source) == []


def test_differential_over_production_wrapper_does_not_fire(census: ModuleType) -> None:
    """两侧都是生产输出、断的是「不相等」：这是行为判据，不是将期望值另算一遍."""
    source = """
from opendata.data.cache import RawResponseCache

cache = RawResponseCache(source="alpha_vantage", endpoint="https://example.invalid")


def cache_key(params: dict[str, str]) -> str:
    return cache.key_for(params)


def test_adjusted_and_raw_are_different_keys() -> None:
    assert cache_key({"adjusted": "true"}) != cache_key({"adjusted": "false"})
"""
    assert _hits(census, source) == []


def test_date_spelling_helper_is_not_a_recomputation(census: ModuleType) -> None:
    """``date.fromisoformat("2024-01-03")`` 是把测试自己写的字面量换个写法，单步换算不算重算."""
    source = """
from datetime import date

from opendata.data.history_coverage import evaluate_history_coverage


def d(value: str) -> date:
    return date.fromisoformat(value)


def test_requested_end_is_the_written_date() -> None:
    report = evaluate_history_coverage(request=None)
    assert report.coverage.requested_end == d("2024-01-03")
"""
    assert _hits(census, source) == []


def test_the_four_face_names_are_unchanged(census: ModuleType) -> None:
    """验收棘轮按「格 id + 面名」打哈希：四类面名一字不能改，改了旧钉就作废."""
    assert tuple(census._RULES) == (
        "self-reference",
        "vacuous-assert",
        "constant-shell",
        "source-form-check",
    )


@pytest.mark.parametrize(
    "relative",
    [
        "tests/test_ecb_series_parser.py",
        "tests/test_source_grant_provisioning.py",
        "tests/test_federal_reserve_clean_documents.py",
        "tests/test_sec_iter2.py",
        "tests/test_szse_lifecycle_xlsx_adapter.py",
        "tests/test_source_attempt_scope_v3.py",
        "tests/test_temporal_instrument_identity.py",
    ],
)
def test_repaired_corpus_files_read_clean(census: ModuleType, relative: str) -> None:
    """改掉的那批用例文件（含 11 处误伤的原件）在面①上读数为 0，且没有靠跳过用例做到."""
    path = REPO_ROOT / relative
    findings = census.self_reference_findings(path, ast.parse(path.read_text(encoding="utf-8")))
    assert findings == [], "\n".join(str(finding) for finding in findings)
