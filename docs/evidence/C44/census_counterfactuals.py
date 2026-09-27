"""C44 空壳普查的反事实：每条判据面都要能红，且不能误伤合法的黄金向量.

普查面（``shell_audit.py``）本身是 AC-17|08 的判据，所以它自己必须可证伪：
这里在临时目录里造出**最小**的命中样本，逐条问「这一面着了吗」——
不碰工作树，跑完即弃，任何一条不如预期就非零退出。

七条反事实（前四条对应 §5.1 的三类空壳 + 恒真断言）：

1. 自指：测试里本地重算毫秒戳，又拿它断言生产函数 ⇒ ``self-reference`` 命中；
2. 恒真：``assert True`` ⇒ ``vacuous-assert`` 命中；
3. 抄定义：生产侧绑的是字面量，测试写 ``CONST == 字面量`` ⇒ ``constant-shell`` 命中；
4. 形式检查：``inspect.getsource`` ⇒ ``source-form-check`` 命中；
5. **正控制**：生产侧 ``LABEL = Path(__file__)...`` 是派生值，``LABEL == 字面量`` 是黄金向量，
   必须**不**命中（规则分不清就会把作者推向删掉预计算断言）；
6. T1 档没有录制夹具 ⇒ ``t1_*`` 读数为 0、判定 gap；
7. 录制夹具被手改一字节 ⇒ ``fixture_sha_ok = no``（档案不等于「存在过」）。

用法::

    python docs/evidence/C44/census_counterfactuals.py
"""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from types import ModuleType

CENSUS = Path(__file__).resolve().parent / "shell_audit.py"
REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``(case, 造出来的 tests/ 相对路径, 该文件源码)``——每条只写一个测试文件。
_SHELL_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "self-reference",
        "test_self_ref.py",
        """
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from opendata_fuyao.endpoints import shanghai_midnight_millis


def _ms(day: str) -> int:
    zone = ZoneInfo("Asia/Shanghai")
    midnight = datetime.combine(date.fromisoformat(day), time.min, tzinfo=zone)
    return int(midnight.timestamp() * 1000)


def test_recomputed_millis_equals_production() -> None:
    assert _ms("2024-01-02") == shanghai_midnight_millis(date(2024, 1, 2))
""",
    ),
    (
        "vacuous-assert",
        "test_vacuous.py",
        """
def test_shutdown_does_not_raise() -> None:
    assert True
""",
    ),
    (
        "constant-shell",
        "test_const_shell.py",
        """
from opendata.pipeline.fake import FAKE_SOURCE


def test_source_label_is_fake() -> None:
    assert FAKE_SOURCE == "fake"
""",
    ),
    (
        "source-form-check",
        "test_form_check.py",
        """
import inspect

from opendata.pipeline.fake import helper


def test_helper_writes_no_magic() -> None:
    assert "42" not in inspect.getsource(helper)
""",
    ),
    (
        "derived-label-is-a-golden-vector",
        "test_derived_label.py",
        """
from opendata.pipeline.derived import LABEL


def test_label_equals_the_directory_name() -> None:
    assert LABEL == "pipeline"
""",
    ),
)

#: 反事实 5 之外的生产侧桩件：一个字面量常量 + 一个派生标签 + 一个可调用对象。
_PRODUCTION_STUBS: Final = {
    "opendata/pipeline/fake.py": (
        '"""Stub."""\n\nFAKE_SOURCE = "fake"\n\n\ndef helper() -> int:\n    return 1\n'
    ),
    "opendata/pipeline/derived.py": (
        '"""Stub."""\n\nfrom pathlib import Path\n\nLABEL = Path(__file__).resolve().parent.name\n'
    ),
}


def _load_census() -> ModuleType:
    """Import ``shell_audit.py`` by path (it is evidence, not a package module)."""
    spec = importlib.util.spec_from_file_location("shell_audit", CENSUS)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with this round
        raise RuntimeError(f"cannot load the census at {CENSUS}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_tree(base: Path, test_source: str, test_name: str) -> Path:
    """A minimal tree the census can walk: ``tests/`` plus the stubbed ``opendata/``."""
    tests_dir = base / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / test_name).write_text(test_source.lstrip("\n"), encoding="utf-8")
    for relative, source in _PRODUCTION_STUBS.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tests_dir


def _shell_counts(census: ModuleType, tests_dir: Path) -> dict[str, int]:
    """Rule -> hit count over one synthetic tree."""
    findings: list[Any] = []  # census Finding objects, typed by attribute access
    for path in sorted(tests_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        findings += census.self_reference_findings(path, tree)
        findings += census.constant_shell_findings(path, tree, tests_dir.parent, {})
        findings += census.form_check_findings(path, tree)
    counts = dict.fromkeys(census._RULES, 0)
    for finding in findings:
        counts[finding.rule] += 1
    return counts


def check_shell_faces(census: ModuleType, results: list[str]) -> None:
    """反事实 1-5：三类空壳各自能红，派生标签的黄金向量不能红."""
    for rule, test_name, source in _SHELL_CASES:
        with tempfile.TemporaryDirectory(prefix="c44-census-") as raw:
            tests_dir = _build_tree(Path(raw), source, test_name)
            counts = _shell_counts(census, tests_dir)
        if rule == "derived-label-is-a-golden-vector":
            ok = counts["constant-shell"] == 0
            detail = f"constant-shell={counts['constant-shell']} (期望 0)"
        else:
            ok = counts[rule] == 1 and sum(counts.values()) == 1
            detail = f"{rule}={counts[rule]} 总命中={sum(counts.values())} (期望 1/1)"
        results.append(f"{'ok  ' if ok else 'FAIL'} break[{rule}] {detail}")


def check_t1_faces(census: ModuleType, results: list[str]) -> None:
    """反事实 6-7：夹具缺失 ⇒ T1 读数为 0；夹具被手改 ⇒ sha 复核为 no."""
    with tempfile.TemporaryDirectory(prefix="c44-t1-") as raw:
        bare = Path(raw) / "tests"
        bare.mkdir()
        (bare / "test_anything.py").write_text(
            "def test_one() -> None:\n    assert 1 == 1\n", encoding="utf-8"
        )
        readings = census._t1_readings(bare)
        missing_ok = (
            readings["fixture_cases"] == "0"
            and readings["fixture_sha_ok"] == "absent"
            and readings["t1_error_cases"] == "0"
            and readings["t1_normalize_cases"] == "0"
            and census._t1_met(readings) is False
        )
        results.append(
            f"{'ok  ' if missing_ok else 'FAIL'} break[t1-fixture-absent] "
            f"cases={readings['fixture_cases']} sha={readings['fixture_sha_ok']} t1_met=False"
        )

        copied = Path(raw) / "tampered" / "tests"
        copied.mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "tests" / census.T1_FIXTURE, copied / census.T1_FIXTURE)
        for plane in (census.T1_TEST, census.GOLDEN_MODULE):
            shutil.copy2(REPO_ROOT / "tests" / plane, copied / plane)
        archive = copied / census.T1_FIXTURE / "responses.json.gz"
        with gzip.open(archive) as handle:
            records = json.loads(handle.read())
        for record in records:
            if record["name"] == "success_prices":
                record["body_text"] = record["body_text"].replace('"code":0', '"code":1', 1)
        archive.write_bytes(gzip.compress(json.dumps(records).encode("utf-8")))
        readings = census._t1_readings(copied)
        tamper_ok = readings["fixture_sha_ok"] == "no" and census._t1_met(readings) is False
        results.append(
            f"{'ok  ' if tamper_ok else 'FAIL'} break[t1-fixture-tampered] "
            f"cases={readings['fixture_cases']} sha={readings['fixture_sha_ok']}"
            "（改一个 code 即红）"
        )


def main() -> int:
    """Run every counterfactual and exit non-zero if one face is not falsifiable."""
    census = _load_census()
    results: list[str] = []
    check_shell_faces(census, results)
    check_t1_faces(census, results)
    for line in results:
        print(line)
    failed = sum(1 for line in results if line.startswith("FAIL"))
    print(f"VERDICT counterfactuals={len(results)} failed={failed} exit={'1' if failed else '0'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
