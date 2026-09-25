"""C16 探针参数面复核：**离线**（不联网、不碰数仓）验证 19 条 verified 能力的探针参数.

对照 C15 的 ``docs/evidence/C15/patrol-probe-audit.txt``（彼时 7 通过 / 12 抛
``ValidationError``）：本轮把 ``PROBE_PARAMS`` 的键改成 ``(domain, source)`` 并逐条填
真实参数，本脚本按注册表逐条取参，跑**与真机巡检同一个** ``transform_query``（validate
阶段），要求 19/19 通过。

期权滚动腿的探针要读标的目录，这里用一个桩 fetcher 替代该接缝，因此全程零网络请求
（目录真实取数与耗时见 ``docs/evidence/C16/live-patrol.txt``）。运行（输出重定向到
同名 ``.txt``）::

    python docs/evidence/C16/probe-param-audit.py
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, ClassVar

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.registry import get_registry
from opendata.pipeline import patrol as patrol_module
from opendata.pipeline.patrol import probe_params

if TYPE_CHECKING:
    from typing import Any

    from opendata.data.registry import ProviderRegistry

#: 固定探针日：滚动腿（期货/期权）的标的由该日推出，写死才能复现
PROBE_DATE = date(2026, 9, 25)


class _StubRow:
    """Catalog row carrying only what the option resolver reads."""

    def __init__(self, symbol: str, delist_date: date | None) -> None:
        self.symbol = symbol
        self.delist_date = delist_date


class _StubCatalogQuery(QueryParams):
    """Query shape the option resolver sends to the instrument catalog."""

    asset_type: str = ""


class _StubCatalog(Fetcher[_StubCatalogQuery, tuple[_StubRow, ...]]):
    """Stand-in for the instrument fetcher so the audit stays offline."""

    capability: ClassVar[Capability] = Capability(
        asset_class="instrument",
        domain="instrument",
        period="1D",
        market="cn",
        source="ths",
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> _StubCatalogQuery:
        """Validate the catalog query."""
        return _StubCatalogQuery.model_validate(kwargs)

    def extract_data(self, params: _StubCatalogQuery, ctx: FetchContext) -> tuple[_StubRow, ...]:
        """Serve the stub listing instead of the upstream catalog."""
        del params, ctx
        return (
            _StubRow("10011425.SH", date(2026, 12, 23)),
            _StubRow("10011420.SH", date(2026, 10, 1)),
        )

    def transform_data(
        self, raw: tuple[_StubRow, ...], params: _StubCatalogQuery
    ) -> tuple[_StubRow, ...]:
        """Pass the rows through unchanged."""
        del params
        return raw


def _stub_the_catalog() -> None:
    """Swap the catalog seam for the stub (the audit must not reach upstream)."""
    patrol_module._catalog_fetcher = lambda registry: _StubCatalog()


def _describe(query: QueryParams) -> str:
    """Render the fields the probe actually landed on the query model."""
    given = {key: getattr(query, key) for key in query.model_fields_set}
    return ", ".join(f"{key}={value!s}" for key, value in sorted(given.items())) or "(空参数)"


def _audit(capability: Capability, registry: ProviderRegistry) -> str:
    """Probe one verified capability offline and describe the outcome."""
    fetcher: Fetcher[Any, Any] = registry.resolve(
        capability.asset_class, capability.domain, source=capability.source
    )
    try:
        params = probe_params(capability, registry, today=PROBE_DATE)
    except Exception as exc:  # 装配失败本身就是要看结论的那件事
        return f"[FAIL-装配] {capability.source}/{capability.domain}: {type(exc).__name__}: {exc}"
    try:
        query = fetcher.transform_query(**params)
    except Exception as exc:  # 同上：C15 记录的就是这一形态
        return f"[FAIL] {capability.source}/{capability.domain}: {type(exc).__name__}: {exc}"
    return f"[OK]   {capability.source}/{capability.domain}: {_describe(query)}"


def main() -> int:
    """Print the offline probe audit for every verified capability."""
    from opendata.data.providers import register_providers

    _stub_the_catalog()
    register_providers()
    registry = get_registry()
    verified = [cap for cap in registry.capabilities() if cap.verified]
    lines = [_audit(capability, registry) for capability in verified]
    print(f"探针日：{PROBE_DATE.isoformat()}（滚动腿据此命名标的）")
    print(f"verified 能力 {len(verified)} 条，逐条离线复核（无网络）：")
    for line in lines:
        print(line)
    passed = sum(1 for line in lines if line.startswith("[OK]"))
    print(f"\n汇总：通过 {passed} / {len(verified)}；C15 离线基线为 7 / 19")
    return 0 if passed == len(verified) else 1


if __name__ == "__main__":
    raise SystemExit(main())
