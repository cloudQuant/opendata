r"""C25 字段级 canary 的判据归因：把塌陷注入进真机读数，看它能说些什么.

本轮自然巡检四条腿全绿（`live-patrol.txt`），而 canary 要证的三件事都没法靠自然现场出：
①「行数是满的、列是全空的」必须被单独打出来（C18 的现场就是 `[ok] 5578 rows` 里
`list_date` 全空）；②C19 量到的两类构建**之差**不能被当成告警（否则 cron 天天叫）；
③「读不到这一页」不能被判成数据坏了。⇒ 只能在真机上把这三面复现出来。

注入点只有一个：`ThsInstrumentFetcher.transform_data` 的**返回值**（normalize 之后、交给
patrol 之前）。其上全部走生产路径（registry 路由与健康位、patrol 的探针与判定、
`FIELD_CANARIES` 的实测形态集合、`health_patrol.main()` 的报表与退出码），假的只有「这一页
的某一列没填」这一件事，且每个场景把注入模式打在标题里。五个场景：

- A1 把 `a-share-index/list_date` 全清空（生产表只允许 full）⇒ 腿 `[ok]` 不变、路由健康位
  不动，但报表多一行 ``[CANARY]``、退出码翻成 1。
- A2 把 `a-share/list_date` 全清空（生产表按 C19 允许 full|hollow）⇒ 不告警，退出码 0。
  这是 A1 的反向对照：告警面没被扩到「上游本来就有的那两面」。
- A2b 只有半空（既非满值亦非全空）⇒ 第三种形态两类构建都解释不了，要告警。
- A3 只读不到某一页（其余照常）⇒ 打成「未判读」，不告警、不动路由。
- A4 源真挂了（探针就失败）⇒ `[FAIL]` + 标不可达 + 退出码 1，与 A1 严格可分。

只读、免额外 Key、零写入。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C25/canary-injection-attribution.py
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from opendata.data.providers.ths.models.instrument import (
    ThsInstrumentFetcher,
    ThsInstrumentQuery,
)
from opendata.data.registry import ProviderRegistry
from opendata.pipeline import patrol as patrol_module

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType
    from typing import Protocol

    from opendata.data.models import Instrument
    from opendata.pipeline.patrol import PatrolResult

    class PatrolCli(Protocol):
        """The cron entry point plus the two module names a scenario replaces."""

        register_providers: Callable[[], None]
        run_patrol: Callable[[], list[PatrolResult]]
        main: Callable[[], int]


#: 注入标记：输出里必须带上，免得这份归档被读成自然现场。
INJECT = "【C25 证据注入，非上游真实行为】"
#: 生产 `FIELD_CANARIES` 里 a-share-index/list_date 只允许 full，a-share/list_date 允许两面。
STRICT = ("a-share-index", "list_date")
LENIENT = ("a-share", "list_date")
#: A3 里被抽掉的那一页（其余页照常返回）。
UNREADABLE_PAGE = "futures"


class InjectedInstrumentFetcher(ThsInstrumentFetcher):
    """Real catalog fetcher whose normalized page is edited before it lands."""

    #: 注入模式：``hollow:<asset>`` / ``partial:<asset>`` / ``unreadable`` / ``down``.
    mode: str = "off"

    def transform_data(
        self, raw: tuple[Instrument, ...], params: ThsInstrumentQuery
    ) -> tuple[Instrument, ...]:
        """Read the real catalog, then apply the labelled edit to that page.

        Args:
            raw: Rows the extraction stage pulled from fuyao.
            params: The validated query; its ``asset_type`` picks the page.

        Returns:
            The (possibly hollowed) rows, exactly as the patrol sees them.

        Raises:
            RuntimeError: In ``down`` mode, or ``unreadable`` mode for the
                one page named by :data:`UNREADABLE_PAGE`.
        """
        if self.mode == "down":
            raise RuntimeError(f"{INJECT} 整条腿不可达")
        if self.mode == "unreadable" and params.asset_type == UNREADABLE_PAGE:
            raise RuntimeError(f"{INJECT} 这一页读不到 asset_type={params.asset_type}")
        rows = super().transform_data(raw, params)
        asset_type = params.asset_type
        if self.mode == f"hollow:{STRICT[0]}" and asset_type == STRICT[0]:
            return _hollow(rows, STRICT[1])
        if self.mode == f"hollow:{LENIENT[0]}" and asset_type == LENIENT[0]:
            return _hollow(rows, LENIENT[1])
        if self.mode == f"partial:{LENIENT[0]}" and asset_type == LENIENT[0]:
            return _half_hollow(rows, LENIENT[1])
        return rows


def _hollow(rows: Sequence[Instrument], field: str) -> tuple[Instrument, ...]:
    """Blank one column of a real page, the way a hollow catalog would."""
    return tuple(row.model_copy(update={field: None}) for row in rows)


def _half_hollow(rows: Sequence[Instrument], field: str) -> tuple[Instrument, ...]:
    """Blank every other row: a shape neither catalog build has ever produced."""
    return tuple(
        row.model_copy(update={field: None}) if index % 2 == 0 else row
        for index, row in enumerate(rows)
    )


def _load_cli() -> PatrolCli:
    """Import :mod:`scripts/ops/health_patrol.py` for its renderer and exit code.

    Returns:
        The loaded CLI module, whose ``main`` is the very function a cron line
        calls.

    Raises:
        RuntimeError: The script cannot be loaded (it moved or was renamed).
    """
    path = Path(__file__).resolve().parents[3] / "scripts" / "ops" / "health_patrol.py"
    spec = importlib.util.spec_from_file_location("health_patrol_cli", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the patrol CLI at {path}")
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("PatrolCli", module)


def _cron_report(results: list[PatrolResult]) -> tuple[str, int]:
    """Render already-collected results through the production cron report.

    Args:
        results: The patrol results for the injected leg.

    Returns:
        ``(report, exit_code)`` — what a cron line prints and sees.
    """
    cli = _load_cli()
    cli.register_providers = lambda: None  # the leg was injected, not registered
    cli.run_patrol = lambda: results
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = int(cli.main())
    return buffer.getvalue(), code


def _run_patrol(mode: str) -> tuple[list[PatrolResult], ProviderRegistry, str, int]:
    """Probe the real catalog leg once under ``mode`` and render the cron report.

    Args:
        mode: The injection mode applied to the fetcher's normalized page.

    Returns:
        ``(results, registry, report, exit_code)``.
    """
    fetcher = InjectedInstrumentFetcher()
    fetcher.mode = mode
    registry = ProviderRegistry()
    registry.register(fetcher)
    results = list(asyncio.run(patrol_module.patrol(registry)))
    report, code = _cron_report(results)
    leg = results[0]
    print(f"  ok={leg.ok} rows={leg.rows} 偏差={len(leg.field_deviations)} 退出码={code}")
    for reading in leg.canaries:
        print(
            f"    {reading.asset_type}/{reading.field}: 形态={reading.shape} "
            f"缺={reading.missing}/{reading.rows} deviates={reading.deviates} "
            f"允许={sorted(reading.allowed)} error={reading.error}"
        )
    print(f"  auto 路由（instrument/ths）：{'可达' if _auto_reachable(registry) else '不可达'}")
    print("  ---- cron 侧报表 ----")
    print("\n".join(f"  {line}" for line in report.rstrip().splitlines()))
    return results, registry, report, code


def _auto_reachable(registry: ProviderRegistry) -> bool:
    """Whether ``source=auto`` will still route to the leg after the patrol.

    Args:
        registry: The private registry the scenario patrolled.

    Returns:
        True when the instrument leg is still an ``auto`` candidate.
    """
    try:
        registry.resolve("metadata", "instrument", source="auto")
    except LookupError:
        return False
    return True


def _judge(
    results: list[PatrolResult],
    registry: ProviderRegistry,
    report: str,
    code: int,
    **expect: object,
) -> bool:
    """Compare one scenario against what it was supposed to produce.

    Args:
        results: The patrol results.
        registry: The registry the patrol marked, if any.
        report: The CLI text a cron line would print.
        code: The exit code a cron line would see.
        expect: Required facts: ``ok``, ``deviations``, ``exit``, ``reachable``
            and ``alarm_in_report``.

    Returns:
        True when every expectation held.
    """
    leg = results[0]
    checks = {
        "探针 ok": leg.ok is expect.get("ok"),
        "偏差条数": len(leg.field_deviations) == expect.get("deviations"),
        "退出码": code == expect.get("exit"),
        "auto 仍可达": _auto_reachable(registry) is expect.get("reachable"),
        "报表含 CANARY 行": ("[CANARY]" in report) is bool(expect.get("alarm_in_report")),
    }
    for label, passed in checks.items():
        print(f"    {'PASS' if passed else 'FAIL'} {label}")
    return all(checks.values())


def _scenario(name: str, mode: str, **expect: object) -> bool:
    """Run one injection scenario and judge it.

    Args:
        name: Scenario label printed ahead of the readings.
        mode: Injection mode for the catalog fetcher.
        expect: The facts :func:`_judge` must see.

    Returns:
        True when the scenario behaved as designed.
    """
    print(f"## {name}（注入模式：{mode or '无'}）")
    results, registry, report, code = _run_patrol(mode)
    verdict = _judge(results, registry, report, code, **expect)
    print(f"  判定：{'PASS' if verdict else 'FAIL'}")
    return verdict


def main() -> int:
    """Run the five scenarios and judge them.

    Returns:
        0 when every scenario behaved as designed, 1 otherwise.
    """
    print("[0] 生产判据表（本轮钉进 patrol.FIELD_CANARIES 的集合）：")
    for canary in patrol_module.FIELD_CANARIES[("instrument", "ths")]:
        print(f"    {canary.asset_type}/{canary.field} 允许 {sorted(canary.allowed)}")
    checks = {
        "A1 塌陷列：腿照绿、路由不动、报表报警、退出码 1": _scenario(
            "A1 a-share-index/list_date 全清空（生产表只允许 full）",
            f"hollow:{STRICT[0]}",
            ok=True,
            deviations=1,
            exit=1,
            reachable=True,
            alarm_in_report=True,
        ),
        "A2 已知的那一面（全空的 a 股目录）不告警": _scenario(
            "A2 a-share/list_date 全清空（生产表按 C19 允许 full|hollow）",
            f"hollow:{LENIENT[0]}",
            ok=True,
            deviations=0,
            exit=0,
            reachable=True,
            alarm_in_report=False,
        ),
        "A2b 两类构建都解释不了的第三种形态要告警": _scenario(
            "A2b a-share/list_date 半空（既非满值亦非全空）",
            f"partial:{LENIENT[0]}",
            ok=True,
            deviations=1,
            exit=1,
            reachable=True,
            alarm_in_report=True,
        ),
        "A3 读不到那一页只算未判读": _scenario(
            "A3 futures 页读不到（其余照常）",
            "unreadable",
            ok=True,
            deviations=0,
            exit=0,
            reachable=True,
            alarm_in_report=False,
        ),
        "A4 源真挂了：判死、标不可达、退出码 1": _scenario(
            "A4 整条腿不可达",
            "down",
            ok=False,
            deviations=0,
            exit=1,
            reachable=False,
            alarm_in_report=False,
        ),
    }
    print("## 判定汇总")
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
