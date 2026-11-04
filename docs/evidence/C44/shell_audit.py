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
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

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
#: 一个操作数「本身就是个值」的那些写法：期望值往这些结构里递归，别处不递归。
_VALUE_FORMS: Final = (
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.IfExp,
    ast.Starred,
    ast.Await,
)
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


def _numeric_constant(node: ast.AST) -> bool:
    """True for an int/float literal (``* 1000``、``/ 1_000_000``)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, int | float)


def _text_constant(node: ast.AST) -> bool:
    """True for a str/bytes literal (``"c" * 64`` 是在填充字符串，不是在算数量)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)


def _is_deriving_arithmetic(child: ast.BinOp) -> bool:
    """这一条算术是在算数量，还是在书写.

    ``REPO_ROOT / LEDGER_PATH`` 与 ``"c" * 64`` 借用的是 ``/`` 和 ``*`` 两个运算符，
    把它们当成重算会把「读档案」「造一个填充串」这两类合法的本地构造一并点成自指；
    反过来 ``timestamp() * 1000``、``days * 86_400_000_000`` 必须继续算重算。
    除/整除只在有一侧是数字字面量时才是数量换算（路径拼接的两边都不是数字）。
    """
    if isinstance(child.op, ast.Div | ast.FloorDiv):
        return _numeric_constant(child.left) or _numeric_constant(child.right)
    if isinstance(child.op, ast.Mult):
        return not (_text_constant(child.left) or _text_constant(child.right))
    return True


def _does_the_work(node: ast.AST) -> bool:
    """True when the subtree derives its value (arithmetic, or a chain of tz/date conversions).

    单个 ``date.fromisoformat("2024-01-03")`` 只是把测试自己写下的字面量换个写法，
    两步以上的换算链（``combine`` + ``timestamp``）才是把生产实现另算一遍。
    """
    conversions = 0
    for child in ast.walk(node):
        if (
            isinstance(child, ast.BinOp)
            and isinstance(child.op, _ARITHMETIC)
            and _is_deriving_arithmetic(child)
        ):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _CONVERSION_NAMES
        ):
            conversions += 1
    return conversions >= 2


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


def _local_definitions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """``def`` names in the module mapped to their node (the use-def hop for helpers)."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _assigned_values(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """Plain local assignments: ``name`` -> every expression written to it in ``scope``.

    Only ``x = ...`` and ``x: T = ...`` count. Following those is what lets the rule read
    ``expected = _recompute(...)`` … ``assert result == expected`` as a self-reference instead
    of a literal comparison; loop/comprehension bindings are deliberately not followed.
    """
    values: dict[str, list[ast.expr]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            value: ast.expr = node.value
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            value = node.value
            names = [node.target.id]
        else:
            continue
        for name in names:
            values.setdefault(name, []).append(value)
    return values


def _parent_links(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Child -> parent links, so an assert can be read inside the scope it sits in."""
    parents: dict[ast.AST, ast.AST] = {}
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            stack.append(child)
    return parents


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """The function an assert is written in, or the module when it sits at import level."""
    current = node
    while True:
        parent = parents.get(current)
        if parent is None:
            return current
        if isinstance(parent, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
            return parent
        current = parent


def _production_names_touched(
    node: ast.AST,
    production: set[str],
    values: dict[str, list[ast.expr]],
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    seen: set[str],
) -> set[str]:
    """这一侧到底点到哪些生产符号.

    裸名（``Bar``）、属性链（``adapter._parse``）都算，经普通局部赋值
    （``fetcher = Fetcher(client)``）或本地构造函数（``snapshot = registry(...)``）
    绕了一路还是落到生产的也算。
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Name):
            continue
        if child.id in production:
            found.add(child.id)
            continue
        if child.id in seen:
            continue
        seen.add(child.id)
        sources: list[ast.AST] = list(values.get(child.id, ()))
        definition = defs.get(child.id)
        if definition is not None:
            sources.append(definition)
        for source in sources:
            found |= _production_names_touched(source, production, values, defs, seen)
    return found


def _recomputed_by(
    node: ast.AST,
    helpers: set[str],
    values: dict[str, list[ast.expr]],
    production: set[str],
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    seen: set[str],
) -> set[str]:
    """这一侧的期望值是哪几个本地算子算出来的（空集 = 不是本地重算）.

    只沿「值」走：字面量容器、算术、调用参数、以及普通局部赋值（``expected = _recompute(...)``
    之后断 ``== expected``，第一版漏的假阴性就在这条跳边上）。两条不走的边各有理由：
    ``x = a.b`` / ``x = a[0]`` 读的是那个对象自带的内容，不是本地重算（否则
    ``pytest.approx(legacy_bar.close * 0.8)`` 这种「相对输入陈述期望」会被当成自指）；
    一次调用如果本身就在跑生产实现，它的参数只是喂进去的输入（``adapter.build(_worksheet(...))``，
    第一版误伤的 input-builder 形状）。
    """
    if isinstance(node, ast.Name):
        if node.id in seen:
            return set()
        seen.add(node.id)
        follow: set[str] = set()
        for expression in values.get(node.id, ()):
            follow |= _recomputed_by(expression, helpers, values, production, defs, seen)
        return follow
    if isinstance(node, ast.Call):
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id in helpers:
            return {callee.id}
        if _production_names_touched(callee, production, values, defs, set()):
            return set()
        given = [*node.args, *(keyword.value for keyword in node.keywords)]
        return _recomputed_in(given, helpers, values, production, defs, seen)
    if isinstance(node, ast.Attribute):
        return set()
    if isinstance(node, ast.Subscript):
        return _recomputed_by(node.slice, helpers, values, production, defs, seen)
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return _recomputed_by(node.elt, helpers, values, production, defs, seen)
    if isinstance(node, ast.DictComp):
        return _recomputed_in([node.key, node.value], helpers, values, production, defs, seen)
    if isinstance(node, _VALUE_FORMS):
        return _recomputed_in(ast.iter_child_nodes(node), helpers, values, production, defs, seen)
    return set()


def _recomputed_in(
    nodes: Iterable[ast.AST],
    helpers: set[str],
    values: dict[str, list[ast.expr]],
    production: set[str],
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    seen: set[str],
) -> set[str]:
    """``_recomputed_by`` over several operand expressions of one node."""
    found: set[str] = set()
    for child in nodes:
        found |= _recomputed_by(child, helpers, values, production, defs, seen)
    return found


def _sides(compare: ast.Compare) -> list[ast.expr]:
    """每个操作数（链式比较 ``a == b == c`` 就是三个）."""
    return [compare.left, *compare.comparators]


def _local_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    """``class`` names in the module mapped to their node (the ``class Fake(Pipeline)`` hop).

    ``_local_definitions()`` only knows ``def``; a test double that subclasses a production
    class is therefore reachable from ``spec = FakePipeline(...)`` only through this map.
    """
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _root_reaches_production(
    node: ast.expr,
    production: set[str],
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
    seen: set[str],
) -> bool:
    """这个表达式落到底是不是生产实现（名字指向谁，不看它装着什么值）.

    认四条边：生产裸名（``DataPipeline``、``import opendata... as run_ledger_module`` 的模块名）、
    属性链（``models.Fred``）、一次造出生产对象的调用（``DataPipeline(spec)``、
    ``RecordingPipeline(spec)``）、普通局部赋值（``pipeline = RecordingPipeline(...)``）。
    三条**不**走的边各有理由：构造调用的参数（``local = fake(specs.PROD_NAME)`` 里参数出现过
    生产名，并不使 ``local`` 成为生产对象）；本地 ``def`` 的函数体（``cache_key(...)`` 那类
    「两侧同经一个本地包装」的行为判据要放过的正是这条路）；以及生产**函数**的返回值
    （``first = build_migration_worklist(...)`` 拿到的是一个 dict，对它调 ``first.items()``
    是在读一个已经拿到的值，不是叫生产再算一遍）。
    """
    if isinstance(node, ast.Name):
        return _name_reaches_production(node.id, production, values, classes, seen)
    if isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
        current: ast.expr = node
        if isinstance(current, ast.Call):
            return _callee_is_production_class(current.func, production, values, classes, seen)
        while isinstance(current, ast.Attribute | ast.Subscript):
            current = current.value
        return _root_reaches_production(current, production, values, classes, seen)
    return False


def _name_reaches_production(
    name: str,
    production: set[str],
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
    seen: set[str],
) -> bool:
    """这个名字是不是生产对象：直接导入、继承生产的本地子类实例，或顺着普通局部赋值追到."""
    if not name or name in seen:
        return False
    seen.add(name)
    if name in production:
        return True
    for source in values.get(name, ()):
        if isinstance(source, ast.Call):
            if _callee_is_production_class(source.func, production, values, classes, seen):
                return True
        elif _root_reaches_production(source, production, values, classes, seen):
            return True
    return False


def _callee_is_production_class(
    callee: ast.expr,
    production: set[str],
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
    seen: set[str],
) -> bool:
    """被调用的那个东西造出来的是不是一台**生产机器**（生产类，或继承生产类的本地替身）.

    生产**函数**的返回值不算：``first = build_migration_worklist(...)`` 拿到的是一个 dict，
    ``first.items()`` 是那个 dict 自带的读取，不是叫生产再算一遍期望值。类与函数之间只用
    「名字首字母大写」这条仓库命名规范来分——判错的方向是把合法写法多放一步（少判），
    而不是把合法写法点红。
    """
    if isinstance(callee, ast.Name):
        if callee.id in production:
            return callee.id[:1].isupper()
        definition = classes.get(callee.id)
        if definition is None:
            return False
        return any(
            _root_reaches_production(base, production, values, classes, seen)
            for base in definition.bases
        )
    if isinstance(callee, ast.Attribute):
        if not callee.attr[:1].isupper():
            return False
        return _root_reaches_production(callee.value, production, values, classes, seen)
    return False


def _local_class_chain(
    name: str,
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
    seen: set[str],
) -> list[ast.ClassDef]:
    """这个名字的对象，其类链上的**本地**类定义：测试替身自己覆写的成员住在这里."""
    if name in seen:
        return []
    seen.add(name)
    found: list[ast.ClassDef] = []
    for source in values.get(name, ()):
        callee = source.func if isinstance(source, ast.Call) else None
        definition = classes.get(callee.id) if isinstance(callee, ast.Name) else None
        if definition is None:
            continue
        found.append(definition)
        for base in definition.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                found.extend(_local_class_chain(base.id, values, classes, seen))
    return found


def _member_is_locally_overridden(
    root: str, member: str, values: dict[str, list[ast.expr]], classes: dict[str, ast.ClassDef]
) -> bool:
    """本地替身自己覆写了这个成员吗——覆写了就算测试代码在算，不算生产自当预言机."""
    for definition in _local_class_chain(root, values, classes, set()):
        for statement in definition.body:
            if (
                isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
                and statement.name == member
            ):
                return True
    return False


def _production_member_calls_on(
    side: ast.expr,
    production: set[str],
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
) -> list[tuple[str, str]]:
    """这一侧**直接写着**的 ``name.member(...)``，且 ``name`` 落到生产（成员名一起带回）.

    只认接收者是**裸名**的调用：``capture.manifest.response_sha256``、``params.values()``、
    ``traced.iloc[0]["_merged_at"].date()`` 这些接收者本身是一串读取/下标的，是在读一个
    已经拿到的值，不是叫生产再算一遍期望。
    """
    found: list[tuple[str, str]] = []
    for node in ast.walk(side):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not isinstance(receiver, ast.Name):
            continue
        if _root_reaches_production(receiver, production, values, classes, set()):
            found.append((receiver.id, node.func.attr))
    return found


def _reads_member_of(sides: list[ast.expr], index: int, root: str) -> bool:
    """另一侧是不是**挂着这个对象读成员**（``pipeline.invalidated`` 对 ``pipeline._pipeline_id``）.

    只把对象整个传给别人（``canonical_sha256(contract)``）不算「读它的成员」：那种断言里
    两侧都是生产函数的输出，比的是行为（round-trip、幂等），不是「读出来 vs 叫它算」。
    """
    for position, side in enumerate(sides):
        if position == index:
            continue
        for node in ast.walk(side):
            if not isinstance(node, ast.Attribute | ast.Subscript):
                continue
            base: ast.expr = node
            while isinstance(base, ast.Attribute | ast.Subscript):
                base = base.value
            if isinstance(base, ast.Name) and base.id == root:
                return True
    return False


def _oracle_expectation(
    sides: list[ast.expr],
    touched_by_side: list[set[str]],
    production: set[str],
    values: dict[str, list[ast.expr]],
    classes: dict[str, ast.ClassDef],
) -> tuple[str, str, set[str]] | None:
    """这一条比较是不是「期望值叫生产对象自己算出来」（生产自当预言机）.

    旧的身份判定把「碰到生产的一侧」整个当被测值放过，于是
    ``assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)] * 2`` 读成 0：两侧都碰
    生产，谁都不算本地重算。可那正是空壳——期望值是问生产对象要的，生产把 ``_pipeline_id``
    的算法改掉，这条断言跟着一起改，永远不会红。命中要求五件事，缺一不算（判据，不是加宽）：

    1. 期望值那一侧**直接**写着 ``name.member(...)``，``name`` 落到生产（导入的生产名/生产
       模块、生产类造出来的对象、继承生产类的本地替身都算）；
    2. 这一侧不是最左操作数——最左侧是「跑一遍被测实现」的写法位置
       （``assert adapter.parse(_worksheet(...)) == {'code': ...}``、
       ``assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW``），
       期望值写在右边；
    3. 这条比较里只有这一侧这样调用生产成员：两侧都调的是行为判据
       （``codec.encode(a) == codec.encode(b)``、``x.key() != y.key()``）；
    4. 另一侧确实**读**到生产（成员读取/下标），它是被测值：比较链里出现写下来的字面量
       （``assert budget.attempts_used == budget.attempts_used_for("fred") == 32``）、
       或者另一侧只是个字面量（§5.2 要的「跑一遍生产、期望写预计算值」），都不命中；
    5. 分工得明确到这两种形之一：(a) 观测那一侧**挂着同一个生产对象读成员**（读的是它、
       期望这一侧又叫同一个对象算）；(b) 调的是生产的**私有成员**
       （``module._datetime_to_epoch_us(...)``）。公开成员不命中 (b)：
       ``ddl.Column("volume", "json", nullable=True)``、
       ``_LegacyQuery.model_json_schema(...)``、``data_query.registered_legs()``
       是用生产类型/生产目录书写预计算期望，§5.1/§5.2 推荐的写法。
       本地替身自己覆写了那个成员也不命中：那时算期望的是测试代码，不是生产实现。

    必须绿的相对期望：``reclaimed.fencing_token == old_claim.fencing_token + 1`` 与
    ``pytest.approx(legacy_bar.close * 0.8)`` 不调用任何成员；
    ``unadjust_bars(bars, []) == tuple(bars)`` 的生产调用在最左，而且是个裸名函数。
    """
    flags = [_production_member_calls_on(side, production, values, classes) for side in sides]
    for index, members in enumerate(flags):
        if not members or index == 0:
            continue
        if any(other for position, other in enumerate(flags) if position != index):
            continue
        others = [side for position, side in enumerate(sides) if position != index]
        if any(_literal_only(side) for side in others):
            continue
        observed: set[str] = set()
        for position, names in enumerate(touched_by_side):
            if position != index:
                observed |= names
        if not observed:
            continue
        for root, member in members:
            if not (_reads_member_of(sides, index, root) or member.startswith("_")):
                continue
            if _member_is_locally_overridden(root, member, values, classes):
                continue
            return root, member, observed
    return None


def self_reference_findings(path: Path, tree: ast.Module) -> list[Finding]:
    """面①：期望值出自本地重算，或出自「叫生产对象自己算一下」（自指）.

    第一版整条 assert 走一遍，只看「出现过本地算子」+「出现过生产裸名」，两边各自的职责不分：
    ``assert adapter.build(_worksheet(...)) == {'literal': 'x'}``（``_worksheet`` 只是在造输入、
    期望值写的是字面量）被判成自指；反过来 ``expected = _recompute(...)`` 加
    ``assert result == expected``，以及 ``fetcher = Fetcher(client)`` 加
    ``assert _ms(day) == fetcher.norm(...)``（生产藏在属性链后面）这两类真自指反而看不见。
    现在按比较的操作数各判一次，一个操作数只有两种身份：

    * **期望值那一侧**：值必须是本地重算出来的（本地算子直接产出，或顺着普通局部赋值追到它），
      并且这一侧自己不碰生产 —— 碰了生产的那一侧是「把生产产出当期望」或「用生产类型书写期望」，
      不是本地重算；
    * **另一侧**：必须跑到生产符号（裸名、属性链、经局部变量或本地构造函数绕过去都算）。

    两侧同时满足才算自指；只出现本地算子而期望值仍是字面量（输入构造器），或只出现生产符号
    而期望值是字面量（黄金向量），都不命中。

    上面那层身份判定有个致命的放过：期望值那一侧「碰到生产」就被当成被测值，于是
    ``assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)] * 2`` 两侧都碰生产，
    谁都不算本地重算，读数 0 —— 但期望值正是叫生产对象自己算出来的，生产改了算法这条断言
    永远跟着改，AC-17|08 要抓的空壳就是这个。所以再加一层 ``_oracle_expectation()``：
    期望值那一侧直接调用生产对象的成员（含私有方法），另一侧只是读生产的观测值，即命中。
    这一层是判据而非加宽：它要求「这一侧调成员、那一侧只读取」这种明确的分工才红，
    本地算子那一族（``helpers``）为空也要跑，因为它跟本地重算无关。
    """
    production = _imports_production_names(tree)
    if not production:
        return []
    helpers = _computing_helpers(tree)
    defs = _local_definitions(tree)
    classes = _local_classes(tree)
    parents = _parent_links(tree)
    module_values = _assigned_values(tree)
    scope_values: dict[int, dict[str, list[ast.expr]]] = {}
    findings: list[Finding] = []
    for statement in _asserts_of(tree):
        scope = _enclosing_scope(statement, parents)
        values = scope_values.get(id(scope))
        if values is None:
            values = dict(module_values)
            if scope is not tree:
                for name, expressions in _assigned_values(scope).items():
                    values.setdefault(name, []).extend(expressions)
            scope_values[id(scope)] = values
        for compare in (node for node in ast.walk(statement) if isinstance(node, ast.Compare)):
            sides = _sides(compare)
            # 每个操作数先判一次身份：碰到生产的就是「被测值」，没碰到的才可能是期望值。
            touched_by_side = [
                _production_names_touched(side, production, values, defs, set()) for side in sides
            ]
            hit = False
            for index, side in enumerate(sides):
                if touched_by_side[index]:
                    continue
                recomputed = _recomputed_by(side, helpers, values, production, defs, set())
                if not recomputed:
                    continue
                touched: set[str] = set()
                for position, names in enumerate(touched_by_side):
                    if position != index:
                        touched |= names
                if touched:
                    findings.append(
                        Finding(
                            path,
                            statement.lineno,
                            "self-reference",
                            f"期望值来自本地重算 {sorted(recomputed)}，同一断言另一侧引用生产符号 "
                            f"{sorted(touched)}",
                        )
                    )
                    hit = True
                    break
            if hit:
                continue
            oracle = _oracle_expectation(sides, touched_by_side, production, values, classes)
            if oracle is not None:
                findings.append(
                    Finding(
                        path,
                        statement.lineno,
                        "self-reference",
                        f"期望值叫生产对象 {oracle[0]} 的成员 {oracle[1]}(...) 现算"
                        f"（生产自当预言机），同一断言另一侧引用生产符号 {sorted(oracle[2])}",
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
