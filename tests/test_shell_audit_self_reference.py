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
        "tests/test_c85_runner.py",
        "tests/test_run_ledger_database_clock.py",
        "tests/test_sina_fzb_fanout_journal.py",
        "tests/test_ths_csi500_sample_composition.py",
        "tests/test_fred_sloos.py",
        "tests/test_platform_contracts.py",
        "tests/test_migration_worklist.py",
    ],
)
def test_repaired_corpus_files_read_clean(census: ModuleType, relative: str) -> None:
    """改掉的那批用例文件（含 11 处误伤的原件）在面①上读数为 0，且没有靠跳过用例做到."""
    path = REPO_ROOT / relative
    findings = census.self_reference_findings(path, ast.parse(path.read_text(encoding="utf-8")))
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_private_production_method_as_oracle_fires(census: ModuleType) -> None:
    """本轮加进来的那一形（c85_runner 原件）：期望值是叫被测对象自己的私有方法算的.

    旧的身份判定把「碰到生产的一侧」整个当被测值放过，这条两侧都碰生产，于是读成 0——
    可 ``_pipeline_id`` 就是生产实现：算法一改，等号两边一起改，断言永远不会红。
    """
    source = """
from datetime import date

from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window

WINDOW = Window(start=date(2026, 9, 22), end=date(2026, 9, 24))
SPEC = PipelineSpec(domain="stock_daily", source="ods-failure")


class RecordingPipeline(DataPipeline):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.invalidated = []


def test_ods_write_failure_invalidates_the_run() -> None:
    pipeline = RecordingPipeline(SPEC, window=WINDOW)
    assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)] * 2
"""
    hits = _hits(census, source)
    assert len(hits) == 1
    assert "_pipeline_id" in hits[0]


def test_public_production_method_as_oracle_fires(census: ModuleType) -> None:
    """同一个生产对象「读出来 vs 叫它算」：公开方法也一样红.

    左侧 ``fetcher.normalized_rows`` 是在读被测对象的观测值，右侧 ``fetcher.normalize(rows)``
    是叫同一个对象把期望算出来——分工清楚，不需要方法是私有的才算自指。
    """
    source = """
from opendata_fuyao.endpoints import YieldCurveFetcher


def test_normalized_rows_match_the_fetchers_own_normalization(client) -> None:
    fetcher = YieldCurveFetcher(client)
    rows = [("600519", "2024-01-02", 1.0)]
    assert fetcher.normalized_rows == fetcher.normalize(rows)
"""
    hits = _hits(census, source)
    assert len(hits) == 1
    assert "normalize" in hits[0]


def test_private_function_of_an_imported_production_module_as_oracle_fires(
    census: ModuleType,
) -> None:
    """run_ledger_database_clock 那一形：期望值是问**导入的生产模块**的私有换算函数要的.

    被测侧 ``timestamp_us`` 是 ``run_ledger_module._database_clock_us(...)`` 的观测值，
    期望侧再问同一个模块要 ``_datetime_to_epoch_us(...)``：换算与读表一起改，断言不红。
    """
    source = """
from datetime import datetime, timezone

import opendata.pipeline.run_ledger as run_ledger_module


def test_database_clock_matches_the_module_own_conversion(connection) -> None:
    database_value = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
    timestamp_us = run_ledger_module._database_clock_us(connection)
    assert timestamp_us == run_ledger_module._datetime_to_epoch_us(database_value)
"""
    hits = _hits(census, source)
    assert len(hits) == 1
    assert "_datetime_to_epoch_us" in hits[0]


def test_locally_overridden_member_is_not_a_production_oracle(census: ModuleType) -> None:
    """本地替身自己覆写了那个成员：算期望的是测试代码，不是生产实现，不红."""
    source = """
from datetime import date

from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window

WINDOW = Window(start=date(2026, 9, 22), end=date(2026, 9, 24))
SPEC = PipelineSpec(domain="stock_daily", source="recorded")


class RecordingPipeline(DataPipeline):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.invalidated = []

    def _pipeline_id(self, window: Window) -> str:
        return "stock_daily:recorded"


def test_invalidation_uses_the_recorded_id() -> None:
    pipeline = RecordingPipeline(SPEC, window=WINDOW)
    assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)]
"""
    assert _hits(census, source) == []


def test_relative_fencing_token_delta_does_not_fire(census: ModuleType) -> None:
    """必须绿的相对期望：在**读出来**的观测值上加一个 ``+ 1``，没有调用任何生产成员."""
    source = """
from datetime import timedelta

from opendata.pipeline.run_ledger import RunLedger, claim_token

NOW = claim_token(0)


def test_reclaim_bumps_the_fencing_token(ledger_engine) -> None:
    ledger = RunLedger(ledger_engine)
    old_claim = ledger.claim("worker-old", NOW, timedelta(seconds=10))
    reclaimed = ledger.claim("worker-new", NOW, timedelta(seconds=20))
    assert reclaimed.fencing_token == old_claim.fencing_token + 1
"""
    assert _hits(census, source) == []


def test_relative_scaled_expectation_does_not_fire(census: ModuleType) -> None:
    """必须绿的相对期望：``pytest.approx(legacy_bar.close * 0.8)`` 是「相对输入陈述期望」.

    ``pytest`` 不是生产包，``legacy_bar.close`` 是成员读取——两个都不构成「叫生产算」。
    """
    source = """
import pytest

from opendata.data.adjust import unadjust_bars
from opendata.data.models import Bar


def test_adjusted_close_is_a_ratio_of_the_raw_close(bars: list[Bar]) -> None:
    legacy_bar = bars[0]
    adjusted = unadjust_bars(bars, [0.8])[0]
    assert adjusted.close == pytest.approx(legacy_bar.close * 0.8)
"""
    assert _hits(census, source) == []


def test_production_call_on_the_observed_side_does_not_fire(census: ModuleType) -> None:
    """必须绿：生产调用只在**被测值**那一侧（最左操作数），期望值是本地写的 tuple(bars)."""
    source = """
from opendata.data.adjust import unadjust_bars


def test_unadjust_over_an_empty_event_list_is_the_input(bars) -> None:
    assert unadjust_bars(bars, []) == tuple(bars)
"""
    assert _hits(census, source) == []


def test_production_call_on_the_observed_side_with_a_read_expectation_does_not_fire(
    census: ModuleType,
) -> None:
    """必须绿：``assert gate.reserve(scope).status is ReservationStatus.RESERVED_NEW``.

    生产成员调用在被测侧（最左），期望值那一侧只是**读**生产枚举成员、没有调用——
    判据不认它是「叫生产算期望」。
    """
    source = """
from opendata.data.source_attempt_scope_v3 import ReservationStatus, SourceAttemptGate


def test_matching_grant_pins_the_reserve(gate_engine, scope) -> None:
    gate = SourceAttemptGate(gate_engine)
    reservation = gate.reserve_attempt_v3(scope)
    assert reservation.status is ReservationStatus.RESERVED_NEW
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.DUPLICATE
"""
    assert _hits(census, source) == []


def test_literal_pinned_chain_does_not_fire(census: ModuleType) -> None:
    """必须绿：比较链里写着字面量期望（``== 32``）时，同对象的公开方法读法不算自指.

    ``assert budget.attempts_used == budget.attempts_used_for("fred") == 32`` 已经把期望
    钉死在 32：实现改掉这一条就红，不是跟着实现一起变的空壳。
    """
    source = """
from opendata.data.request_budget import RequestBudget


def test_budget_counts_every_series() -> None:
    budget = RequestBudget(profile=None)
    assert budget.attempts_used == budget.attempts_used_for("fred") == 32
"""
    assert _hits(census, source) == []


def test_round_trip_behaviour_over_the_same_object_does_not_fire(census: ModuleType) -> None:
    """必须绿：两侧都是生产函数的输出、比的是行为（round-trip 后哈希不变）.

    ``canonical_sha256(contract) == canonical_sha256(model_validate(contract.model_dump()))``
    里期望值那一侧并没有「挂着同一个对象读成员」的另一侧配合——被测侧是把对象整个传给别人。
    """
    source = """
from opendata.data.contracts import DatasetContract, canonical_sha256


def test_canonical_hash_survives_a_round_trip(contract: DatasetContract) -> None:
    assert canonical_sha256(contract) == canonical_sha256(
        DatasetContract.model_validate(contract.model_dump())
    )
"""
    assert _hits(census, source) == []


def test_value_returned_by_production_is_not_a_production_object(census: ModuleType) -> None:
    """必须绿：生产**函数**返回的是数据，不是生产机器——``first.items()`` 是 dict 自带读取.

    ``assert first["worklist_sha256"] == canonical_json_sha256({... for ... in first.items()})``
    的期望值来自一个本地 dict 推导 + 一个公开生产函数的输出，接收者只是碰巧由生产函数造出来。
    """
    source = """
from opendata.migration.worklist import build_migration_worklist, canonical_json_sha256


def test_worklist_hash_covers_everything_but_itself(plan, runtime, catalog) -> None:
    first = build_migration_worklist(plan, runtime, catalog)
    assert first["worklist_sha256"] == canonical_json_sha256(
        {key: value for key, value in first.items() if key != "worklist_sha256"}
    )
"""
    assert _hits(census, source) == []
