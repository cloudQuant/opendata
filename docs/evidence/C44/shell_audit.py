"""AC-17|08「反空壳抽审」的机器可算面（C44 建立）.

判据原文（``docs/迭代计划/.../验收文档.md`` AC-17 条目 8）问两件事：

1. **三种空壳**：无自指测试、无单断言空壳、无 ``inspect.getsource`` 形式检查
   （定义与处方见 ``代码质量规范.md`` §5.1）；
2. **T1 档基线**（§5.2）：错误翻译用真实信封样例 ≥3、认证头/参数构造断言预计算值
   （黄金向量）、normalize 用真实报文 ≥3。

这个脚本把六个问题各变成一个可复算的读数，每条命中打到 ``file:line``。
规则写死在下面：改规则等于改判据，必须在 ``README.md`` 里重新交代并跑反事实。

用法::

    python docs/evidence/C44/shell_audit.py                  # 面当前工作树
    python docs/evidence/C44/shell_audit.py --root /tmp/c44-pre  # 面修复前的树

退出码：任一空壳面非零、或 T1 档不达标即返回 1（判定面自己也得能红）。
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

#: 测试里出现这些包的名字才算「引用了被测生产符号」。
PRODUCTION_ROOTS: Final = ("opendata", "opendata_fuyao")
#: §5.2 点名的 T1 档测试文件、录制夹具与黄金向量表。
T1_TEST: Final = "test_fuyao_recorded_envelopes.py"
T1_FIXTURE: Final = "fixtures/upstream/fuyao_t1_envelopes"
GOLDEN_MODULE: Final = "fuyao_golden.py"
#: 录制件的读取入口：断言的期望/输入经过它们，才算「面着真实报文」。
RECORD_LOADERS: Final = ("_records", "_body", "_payload", "_client_for")
#: §5.2 的两条下限。
MIN_T1_ERROR_CASES: Final = 3
MIN_T1_NORMALIZE_CASES: Final = 3

_ARITHMETIC: Final = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
#: 生产侧把字面量包一层再声明的常见写法；拆包后仍按字面量算。
_CONSTANT_WRAPPERS: Final = ("Field", "ConfigDict")
#: 出现即说明本地函数在「重算」而不是在「查表」：日期/时区/时间戳换算。
_CONVERSION_NAMES: Final = ("combine", "fromisoformat", "strptime", "timestamp", "utcoffset")
#: 读源码文本当判据的那一族调用。
_FORM_CHECK_NAMES: Final = ("getsource", "getsourcefile", "findsource", "getsourcelines")
_RULES: Final = ("self-reference", "vacuous-assert", "constant-shell", "source-form-check")


class Finding:
    """一条命中：文件、行号、规则名与给人看的一句话."""

    def __init__(self, path: Path, line: int, rule: str, detail: str) -> None:
        """Bind the hit to a place, a rule name and one sentence of prose."""
        self.path = path
        self.line = line
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        """Render as ``file:line [rule] detail``."""
        return f"{self.path}:{self.line} [{self.rule}] {self.detail}"


def _imports_production_names(tree: ast.Module) -> set[str]:
    """Names bound by ``from opendata* import ...`` / ``import opendata*``."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        root = getattr(node, "module", None) or ""
        if isinstance(node, ast.ImportFrom) and root.split(".")[0] in PRODUCTION_ROOTS:
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] in PRODUCTION_ROOTS for alias in node.names
        ):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return bound


def _production_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Test-side production names mapped to ``(module dotted path, imported symbol)``.

    Plain ``import a.b.c`` entries resolve to their own top package with no
    symbol, which the binding lookup below reads as "unresolvable" and keeps
    flagging - the strict default is intentional.
    """
    bound: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in PRODUCTION_ROOTS:
                for alias in node.names:
                    bound[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PRODUCTION_ROOTS:
                    root = (alias.asname or alias.name).split(".")[0]
                    bound[root] = (root, root)
    return bound


def _does_the_work(node: ast.AST) -> bool:
    """True when the subtree derives its value (arithmetic or a tz/date conversion)."""
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, _ARITHMETIC):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _CONVERSION_NAMES
        ):
            return True
    return False


def _computing_helpers(tree: ast.Module) -> set[str]:
    """Local functions whose body computes, rather than looks up."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _does_the_work(node)
    }


def _literal_only(node: ast.AST) -> bool:
    """True for a constant, or arithmetic over constants (``512 * 1024 * 1024``)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float | str | bytes)
    if isinstance(node, ast.BinOp):
        return _literal_only(node.left) and _literal_only(node.right)
    if isinstance(node, ast.UnaryOp):
        return _literal_only(node.operand)
    return False


def _callee_names(node: ast.AST) -> set[str]:
    """Bare names used as the callable of some ``Call`` inside ``node``."""
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _asserts_of(node: ast.AST) -> list[ast.Assert]:
    """Every assert statement below ``node``."""
    return [child for child in ast.walk(node) if isinstance(child, ast.Assert)]


def self_reference_findings(path: Path, tree: ast.Module) -> list[Finding]:
    """面①：期望值出自本地重算，同一句断言又打到生产符号（自指）."""
    production = _imports_production_names(tree)
    helpers = _computing_helpers(tree)
    findings: list[Finding] = []
    for statement in _asserts_of(tree):
        used_helpers = _callee_names(statement) & helpers
        if not used_helpers:
            continue
        names = {child.id for child in ast.walk(statement) if isinstance(child, ast.Name)}
        touched = (names & production) - used_helpers
        if touched:
            findings.append(
                Finding(
                    path,
                    statement.lineno,
                    "self-reference",
                    f"期望值来自本地重算 {sorted(used_helpers)}，同一断言又引用生产符号 "
                    f"{sorted(touched)}",
                )
            )
    return findings


def _declared_constant(node: ast.expr | None) -> bool:
    """生产侧的绑定「就是那个值」吗：字面量、字面量算术，或 ``Field``/``ConfigDict`` 一层包装.

    ``SOURCE = Path(__file__).resolve().parent.name`` 这类**派生**绑定返回 False —— 拿它跟
    字面量比是黄金向量（§5.1 推荐的写法），不是抄定义；规则分不清这一层会把作者推向
    删掉合法的预计算断言。
    """
    if node is None:
        return False
    if _literal_only(node):
        return True
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_declared_constant(element) for element in node.elts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _CONSTANT_WRAPPERS
    ):
        given: list[ast.expr] = [*node.args, *(kw.value for kw in node.keywords)]
        return bool(given) and all(_declared_constant(value) for value in given)
    return False


def _binding_values(
    prod_root: Path, module: str, symbol: str, cache: dict[tuple[str, str], list[ast.expr]]
) -> list[ast.expr]:
    """Every value expression bound to ``symbol`` anywhere in that production module."""
    key = (module, symbol)
    if key in cache:
        return cache[key]
    file_path = prod_root.joinpath(*module.split("."))
    for candidate in (file_path.with_suffix(".py"), file_path / "__init__.py"):
        if candidate.is_file():
            file_path = candidate
            break
    values: list[ast.expr] = []
    if file_path.is_file():
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    if not node.targets:
                        continue
                    target, value = node.targets[0], node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    target, value = node.target, node.value
                else:
                    continue
                if _referenced_name(target) == symbol:
                    values.append(value)
    cache[key] = values
    return values


def _referenced_name(node: ast.expr) -> str | None:
    """The plain name a target/subscript/attribute refers to."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    return None


def _immediate_member(compare_left: ast.expr, root: str) -> str | None:
    """The member read straight off a production name (``settings.app_name`` -> ``app_name``)."""
    for node in ast.walk(compare_left):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == root
        ):
            return node.attr
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == root
        ):
            return _referenced_name(node)
    return None


def _is_constant_restatement(
    compare: ast.Compare,
    production: dict[str, tuple[str, str]],
    prod_root: Path,
    cache: dict[tuple[str, str], list[ast.expr]],
) -> bool:
    """``CONSTANT == <字面量/字面量算术>``，左边是裸的生产常量、且它绑的确实是个常量."""
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    if not _literal_only(compare.comparators[0]):
        return False
    if any(isinstance(child, ast.Call) for child in ast.walk(compare.left)):
        return False
    roots = [
        node.id
        for node in ast.walk(compare.left)
        if isinstance(node, ast.Name) and node.id in production
    ]
    if not roots:
        return False
    for root in roots:
        module, symbol = production[root]
        lookup = _immediate_member(compare.left, root) or symbol
        values = _binding_values(prod_root, module, lookup, cache)
        if not values or all(_declared_constant(value) for value in values):
            return True
    return False


def constant_shell_findings(
    path: Path,
    tree: ast.Module,
    prod_root: Path,
    cache: dict[tuple[str, str], list[ast.expr]],
) -> list[Finding]:
    """面②：恒真断言，或整个用例只在把常量定义抄一遍."""
    production = _production_imports(tree)
    findings: list[Finding] = []
    for function in _test_functions(tree):
        for statement in _asserts_of(function):
            tested = statement.test
            if isinstance(tested, ast.Constant):
                findings.append(
                    Finding(
                        path,
                        statement.lineno,
                        "vacuous-assert",
                        f"断言字面量 {ast.unparse(tested)}",
                    )
                )
            elif isinstance(tested, ast.Compare) and _is_constant_restatement(
                tested, production, prod_root, cache
            ):
                findings.append(
                    Finding(
                        path,
                        statement.lineno,
                        "constant-shell",
                        f"「{ast.unparse(tested.left)} == {ast.unparse(tested.comparators[0])}」"
                        "只是把定义抄了一遍",
                    )
                )
    return findings


def form_check_findings(path: Path, tree: ast.Module) -> list[Finding]:
    """面③：以源码文本（``inspect.getsource`` 一族）为判据的形式检查."""
    return [
        Finding(path, node.lineno, "source-form-check", "读源码文本而不是读行为")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _FORM_CHECK_NAMES
    ]


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """All ``test*`` functions in the module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    ]


def _case_multiplicity(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """How many cases a ``pytest.mark.parametrize`` decorator actually fans out to."""
    total = 1
    for decorator in function.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        if call is None or not _callee_matches(call, "parametrize"):
            continue
        for arg in call.args[1:]:
            if isinstance(arg, ast.List | ast.Tuple):
                total *= len(arg.elts)
    return total


def _callee_matches(call: ast.Call, attribute: str) -> bool:
    """True for ``pytest.mark.<attribute>`` / bare ``<attribute>``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == attribute
    return isinstance(func, ast.Name) and func.id == attribute


def _raises_fuyao_error(node: ast.AST) -> bool:
    """True when the subtree expects ``pytest.raises(FuyaoError)``."""
    return any(
        isinstance(child, ast.Call)
        and _callee_matches(child, "raises")
        and any(isinstance(arg, ast.Name) and arg.id == "FuyaoError" for arg in child.args)
        for child in ast.walk(node)
    )


def _reads_recording(node: ast.AST) -> bool:
    """True when the subtree pulls bytes through the recorded-fixture loaders."""
    return bool(_callee_names(node) & set(RECORD_LOADERS))


def _normalize_over_recording(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count ``normalize_*`` calls in one test that read the recorded body."""
    if not _reads_recording(function):
        return 0
    return sum(
        1
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("normalize_")
    )


def _t1_readings(tests_dir: Path) -> dict[str, str]:
    """§5.2 三问，面着录制夹具与 T1 测试文件逐项算."""
    readings: dict[str, str] = {
        "fixture_cases": "0",
        "fixture_sha_ok": "absent",
        "provenance": "absent",
        "t1_error_cases": "0",
        "t1_success_cases": "0",
        "t1_golden_days": "0",
        "t1_normalize_cases": "0",
    }
    responses = tests_dir / T1_FIXTURE / "responses.json.gz"
    t1_file = tests_dir / T1_TEST
    if not responses.exists() or not t1_file.exists():
        return readings

    cases = json.loads(gzip.decompress(responses.read_bytes()).decode("utf-8"))
    readings["fixture_cases"] = str(len(cases))
    readings["fixture_sha_ok"] = (
        "yes"
        if all(
            record["truncated"] is False
            and hashlib.sha256(str(record["body_text"]).encode("utf-8")).hexdigest()
            == record["body_sha256"]
            for record in cases
        )
        else "no"
    )
    meta_path = tests_dir / T1_FIXTURE / "meta.json"
    if meta_path.exists():
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        readings["provenance"] = (
            "yes"
            if str(meta.get("recorder", "")).endswith(".py")
            and "联调录制" in str(meta.get("source", ""))
            else "no"
        )

    tree = ast.parse(t1_file.read_text(encoding="utf-8"))
    functions = _test_functions(tree)
    readings["t1_error_cases"] = str(
        sum(
            _case_multiplicity(fn)
            for fn in functions
            if _raises_fuyao_error(fn) and _reads_recording(fn)
        )
    )
    readings["t1_success_cases"] = str(
        sum(
            _case_multiplicity(fn)
            for fn in functions
            if not _raises_fuyao_error(fn) and _reads_recording(fn) and _asserts_of(fn)
        )
    )
    readings["t1_normalize_cases"] = str(sum(_normalize_over_recording(fn) for fn in functions))
    golden_file = tests_dir / GOLDEN_MODULE
    if golden_file.exists():
        readings["t1_golden_days"] = str(
            _golden_table_size(ast.parse(golden_file.read_text("utf-8")))
        )
    return readings


def _golden_table_size(tree: ast.Module) -> int:
    """Number of pinned ``"YYYY-MM-DD": <int>`` entries in the golden module."""
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            continue
        keys = [key for key in value.keys if isinstance(key, ast.Constant)]
        if keys and all(str(key.value).count("-") == 2 for key in keys):
            total += sum(1 for entry in value.values if isinstance(entry, ast.Constant))
    return total


def audit(tests_dir: Path) -> tuple[list[Finding], dict[str, str]]:
    """Run the three shell faces over ``tests_dir`` and read the T1 faces beside it."""
    findings: list[Finding] = []
    prod_root = tests_dir.parent
    cache: dict[tuple[str, str], list[ast.expr]] = {}
    #: 递归而不是只数 ``tests/test_*.py``：将来落在子目录里的用例不能靠人记着改这里。
    files = sorted(tests_dir.rglob("test_*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        findings += self_reference_findings(path, tree)
        findings += constant_shell_findings(path, tree, prod_root, cache)
        findings += form_check_findings(path, tree)
    readings = _t1_readings(tests_dir)
    readings["files_scanned"] = str(len(files))
    return findings, readings


def rule_counts(findings: list[Finding]) -> dict[str, int]:
    """Findings per rule name (the three shell readings)."""
    counts = dict.fromkeys(_RULES, 0)
    for finding in findings:
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
    return counts


def verdict(counts: dict[str, int], readings: dict[str, str]) -> bool:
    """True when every shell face is clean and the T1 baselines are met."""
    shells = sum(counts[rule] for rule in _RULES)
    return shells == 0 and _t1_met(readings)


def _t1_met(readings: dict[str, str]) -> bool:
    """The §5.2 baselines, read off the recorded fixture and the T1 test file."""
    numbers = {key: int(readings.get(key, "0")) for key in ("t1_error_cases", "t1_normalize_cases")}
    return (
        numbers["t1_error_cases"] >= MIN_T1_ERROR_CASES
        and numbers["t1_normalize_cases"] >= MIN_T1_NORMALIZE_CASES
        and int(readings.get("t1_success_cases", "0")) >= 1
        and int(readings.get("t1_golden_days", "0")) >= 1
        and readings.get("fixture_sha_ok") == "yes"
        and readings.get("provenance") == "yes"
    )


def main(argv: list[str] | None = None) -> int:
    """Print every reading, list every hit, and exit non-zero when a face fails."""
    parser = argparse.ArgumentParser(description="AC-17|08 shell census and T1 baselines")
    parser.add_argument("--root", default=".", help="Tree to measure (contains tests/).")
    args = parser.parse_args(argv)
    tests_dir = Path(args.root).resolve() / "tests"

    findings, readings = audit(tests_dir)
    counts = rule_counts(findings)
    for rule in _RULES:
        print(f"shell_count[{rule}] = {counts[rule]}")
    for key in (
        "files_scanned",
        "fixture_cases",
        "fixture_sha_ok",
        "provenance",
        "t1_error_cases",
        "t1_success_cases",
        "t1_normalize_cases",
        "t1_golden_days",
    ):
        print(f"{key} = {readings[key]}")
    for finding in findings:
        print(f"HIT {finding}")

    passed = verdict(counts, readings)
    shells = sum(counts[rule] for rule in _RULES)
    print(
        f"VERDICT shells={'clean' if shells == 0 else 'found'} "
        f"t1={'met' if _t1_met(readings) else 'gap'} exit={'0' if passed else '1'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
