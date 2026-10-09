"""``authority.json`` 与活注册表的对账体检（AC-3 / 任务 #30）：三份读，一份报告.

本轮改的是**元数据**（权威表），而元数据最容易犯的错是"声明了一条不存在的腿"：
``GET /api/v1/data/sources`` 把表原样回给人看（``opendata/api/data.py:59``），所以
表里的假腿就是对外错误声明；反过来，注册表里有 verified 腿却不在表里，就退回
"谁先注册谁答"（C22 在宏观域上复现过）。``opendata/data/registry.py`` 的
``reconcile_authority`` 是这两件事的判据，本脚本把它和**改动前后的行为等价**一起量出来：

* **A 面（差集）**：把 HEAD 提交的表与工作树当前的表并排，逐对 (域, 源) 到**活注册表**
  里查有没有对应能力。"7 对假腿"在这份输出里是可复现的读数，不是叙述。
* **B 面（路由等价）**：两版表分别灌进路由层，对注册表里每一个 (asset_class, domain,
  period, market) 组、以及每一种"只给域"的调用，各走一次**生产 ``resolve(source=auto)``**
  取答案，逐组对比。本轮没有新增/删除任何一条腿，所以两组答案都必须一字不变；
  只要有任何一组变了，就说明"只改元数据"这个说法不成立，脚本判红。
* **C 面（对账判据）**：``reconcile_authority(活注册表)`` 必须为空——表说的腿都存在，
  存在的腿都归表管，域一个不漏。

只读：不发公网请求、不碰数仓、不读也不打印任何密钥（本脚本只看注册表里的能力声明）。
表内容换成临时文件时走 ``registry._AUTHORITY_PATH`` 这个既有接缝，``finally`` 复位，
并在校验"临时文件读回的内容与 ``git show`` 逐字节同构"之后才用于路由。

用法（py313 环境，无需公网）：
    python scripts/ops/authority_reconcile_check.py
退出码：A/B/C 三面都判"本轮未改变任何路由答案"且 C 面干净返回 0；否则 1。
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404  # sole subprocess.run: literal git show argv, no shell
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opendata.data.capability import Capability

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opendata.data import registry as registry_module  # noqa: E402
from opendata.data.providers import register_providers  # noqa: E402
from opendata.data.registry import (  # noqa: E402
    ProviderRegistry,
    authority_baseline,
    get_registry,
    reconcile_authority,
)

AUTHORITY_RELPATH = "opendata/data/authority.json"

#: One routing question: the ``(asset_class, domain, period, market)`` a caller asks with.
Request = tuple[str, str, str | None, str | None]


class Table(NamedTuple):
    """One version of the authority table, with where it came from.

    Attributes:
        label: Which side of the diff this table is (``HEAD`` / ``worktree``).
        text: The exact file content, so the archive can hash it.
        domains: Parsed ``domains`` mapping, source order preserved.
    """

    label: str
    text: str
    domains: dict[str, list[str]]


def parse_table(label: str, text: str) -> Table:
    """Parse a table body, failing closed on anything that is not the shipped shape.

    Args:
        label: Side of the diff this table belongs to.
        text: Raw JSON text.

    Returns:
        The parsed table.

    Raises:
        ValueError: If the JSON is unreadable or has no ``domains`` mapping.
    """
    try:
        raw = json.loads(text)
        domains = raw["domains"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"{label} authority table is unreadable: {exc}") from exc
    if not isinstance(domains, dict):
        raise ValueError(f"{label} authority table has no domains mapping")
    return Table(label=label, text=text, domains={k: list(v) for k, v in domains.items()})


def read_head_table() -> Table:
    """Read the committed authority table straight out of git.

    The comparison must not be written from memory: HEAD is the only independent
    record of what shipped before this round.

    Returns:
        The committed table.

    Raises:
        ValueError: If git cannot produce the file.
    """
    result = subprocess.run(  # noqa: S603  # nosec B603 B607  # fixed argv; git PATH exec; read-only HEAD show
        ["git", "show", f"HEAD:{AUTHORITY_RELPATH}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git show HEAD:{AUTHORITY_RELPATH} failed: {result.stderr.strip()}")
    return parse_table("HEAD", result.stdout)


def read_worktree_table() -> Table:
    """Read the authority table the deployment is about to ship.

    Returns:
        The working-tree table.
    """
    path = ROOT / AUTHORITY_RELPATH
    return parse_table("worktree", path.read_text(encoding="utf-8"))


@contextmanager
def routed_by(table: Table) -> Iterator[None]:
    """Point the routing layer at one table version for the duration of the block.

    ``authority_baseline()`` is the seam the resolver uses, and it reads
    ``_AUTHORITY_PATH``, so swapping that path and clearing the cache makes the
    production code answer from a chosen table without re-implementing ranking.

    Args:
        table: The table version to route with.

    Yields:
        Nothing; the registry answers from ``table`` inside the block.

    Raises:
        ValueError: If the temp file does not read back as the table it was given.
    """
    original = registry_module._AUTHORITY_PATH
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        handle.write(table.text)
        staged = Path(handle.name)
    try:
        if json.loads(staged.read_text(encoding="utf-8")) != json.loads(table.text):
            raise ValueError(f"{table.label}: staged table did not round-trip")
        registry_module._AUTHORITY_PATH = staged
        authority_baseline.cache_clear()
        yield
    finally:
        registry_module._AUTHORITY_PATH = original
        authority_baseline.cache_clear()
        staged.unlink(missing_ok=True)


def answer(registry: ProviderRegistry, request: Request) -> str:
    """Ask the production resolver one question and return its answer as text.

    A fail-closed ``LookupError`` is an answer too: the C22 cross-market guard
    exists precisely so that "which source replied" is not the only thing a
    caller can observe.

    Args:
        registry: The fully populated live registry.
        request: ``(asset_class, domain, period, market)`` to route.

    Returns:
        The winning source, or the fail-closed reason text.
    """
    asset_class, domain, period, market = request
    try:
        fetcher = registry.resolve(asset_class, domain, period=period, market=market, source="auto")
    except LookupError as exc:
        return f"FAIL-CLOSED: {exc}"
    return fetcher.capability.source


def routing_questions(capabilities: list[Capability]) -> list[Request]:
    """Build the set of routing questions the registry can actually be asked.

    Both shapes per domain: every ``(period, market)`` the legs really cover, and
    the domain-only request that drops both dimensions (the market-blind shape
    the fail-closed guard judges).

    Args:
        capabilities: The live registry's capabilities.

    Returns:
        Distinct requests, sorted so the archive reads the same way every run.
    """
    shaped: list[Request] = sorted(
        {(c.asset_class, c.domain, c.period, c.market) for c in capabilities},
        key=lambda item: (item[1], item[2], item[3], item[0]),
    )
    blind: list[Request] = sorted(
        {(c.asset_class, c.domain, None, None) for c in capabilities},
        key=lambda item: (item[1], item[0]),
    )
    return shaped + blind


def question_label(request: Request) -> str:
    """Render a request as one readable line.

    Args:
        request: The routing request to name.

    Returns:
        A ``domain period market [写法]`` label.
    """
    asset_class, domain, period, market = request
    shape = "只给域" if period is None and market is None else f"{period}/{market}"
    return f"{asset_class}/{domain} {shape}"


def capability_for(capabilities: list[Capability], domain: str, source: str) -> bool:
    """Whether the live registry has any capability for this ``(domain, source)`` pair.

    Args:
        capabilities: The live registry's capabilities.
        domain: Domain identifier claimed by the table.
        source: Source claimed for that domain.

    Returns:
        True when a capability is registered for the pair.
    """
    return any(item.domain == domain and item.source == source for item in capabilities)


def report_diff(capabilities: list[Capability], before: Table, after: Table) -> tuple[int, int]:
    """Print which ``(domain, source)`` claims each table makes and can back up.

    Args:
        capabilities: The live registry's capabilities.
        before: The committed table.
        after: The working-tree table.

    Returns:
        ``(phantom_before, phantom_after)`` - the counts the exit code depends on.
    """
    print("===== A 面：两版权威表逐对 (域, 源) 到活注册表里查有没有腿 =====")
    phantoms: dict[str, list[str]] = {}
    for table in (before, after):
        found: list[str] = []
        for domain, sources in table.domains.items():
            for source in sources:
                if capability_for(capabilities, domain, source):
                    print(f"  [{table.label}] {domain}/{source}: 有腿")
                else:
                    found.append(f"{domain}/{source}")
                    print(f"  [{table.label}] {domain}/{source}: 无腿（对外假声明）")
        phantoms[table.label] = found
    for label in (before.label, after.label):
        detail = f"：{phantoms[label]}" if phantoms[label] else ""
        print(f"  {label} 假声明数 = {len(phantoms[label])}{detail}")
    removed = sorted(set(phantoms[before.label]) - set(phantoms[after.label]))
    added = sorted(set(phantoms[after.label]) - set(phantoms[before.label]))
    print(f"  本轮消掉的假声明 {len(removed)} 对：{removed}")
    print(f"  本轮新增的假声明 {len(added)} 对：{added}")
    return len(phantoms[before.label]), len(phantoms[after.label])


def report_routing(
    registry: ProviderRegistry,
    capabilities: list[Capability],
    before: Table,
    after: Table,
) -> list[str]:
    """Compare every routing answer between the two table versions.

    Args:
        registry: The fully populated live registry.
        capabilities: The live registry's capabilities (they define the questions).
        before: The committed table.
        after: The working-tree table.

    Returns:
        Labels of the questions whose answer changed; empty means the metadata
        edit provably moved no routing decision.
    """
    questions = routing_questions(capabilities)
    print(f"\n===== B 面：{len(questions)} 个路由问句，两版表逐条对答案（都走生产 resolve）=====")
    answers: dict[str, dict[str, str]] = {}
    for table in (before, after):
        with routed_by(table):
            staged = {key: list(value) for key, value in authority_baseline().items()}
            if staged != table.domains:
                raise ValueError(f"{table.label}: routing layer did not read the staged table")
            answers[table.label] = {question_label(q): answer(registry, q) for q in questions}
    changed: list[str] = []
    for question in questions:
        label = question_label(question)
        first = answers[before.label][label]
        second = answers[after.label][label]
        if first == second:
            print(f"  {label}: 两版一致 -> {first[:72]}")
        else:
            changed.append(label)
            print(f"  {label}: 答案变了！HEAD={first[:60]} -> worktree={second[:60]}")
    print(f"  答案改变的路由问句数 = {len(changed)} / {len(questions)}")
    return changed


def report_reconciliation(capabilities: list[Capability]) -> tuple[str, ...]:
    """Print the reconciliation verdict for the shipped table.

    Args:
        capabilities: The live registry's capabilities.

    Returns:
        The violations found; empty means the table and registry agree both ways.
    """
    print("\n===== C 面：reconcile_authority(活注册表) 判据 =====")
    violations = reconcile_authority(capabilities)
    for violation in violations:
        print(f"  {violation}")
    print(f"  违规数 = {len(violations)}")
    return violations


def main() -> int:
    """Run the three readings and decide the exit code.

    Returns:
        0 when the shipped table has no phantom claim, no routing answer moved
        between the two versions, and the reconciliation is clean; 1 otherwise.
    """
    register_providers()
    registry = get_registry()
    capabilities = registry.capabilities()
    sources = sorted({capability.source for capability in capabilities})
    domains = {capability.domain for capability in capabilities}
    print(f"活注册表：{len(capabilities)} 条能力 / {len(domains)} 个域 / 源={sources}")
    before = read_head_table()
    after = read_worktree_table()
    for table in (before, after):
        digest = hashlib.sha256(table.text.encode("utf-8")).hexdigest()
        print(f"  {table.label}: {len(table.domains)} 行  sha256={digest}")

    phantom_before, phantom_after = report_diff(capabilities, before, after)
    changed = report_routing(registry, capabilities, before, after)
    violations = report_reconciliation(capabilities)

    ok = phantom_after == 0 and not changed and not violations
    print("\n===== 判读 =====")
    print(f"  对外声明面：假声明 {phantom_before} -> {phantom_after}")
    print(f"  行为面：路由答案变化 {len(changed)} 组")
    print(f"  判据面：对账违规 {len(violations)} 条")
    print(f"  结论：{'表与现实一致，且本轮未改变任何路由答案' if ok else '不一致，见上面逐项读数'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
