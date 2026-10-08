"""C54 仪器：宽→长熔解口径进了口径映射表之后，逐面把「谁声明、谁读、谁拒绝」读出来.

不连库、不写入、不读密钥：全部读的是映射表文本、加载后的映射，以及三个
熔解器在合成帧上的现场行为。面 E 是反事实面（改表就改熔解结果），面 G 把
「今天没有行为差别的声明」原样登记，不把它算成承载。

运行：``python -u docs/evidence/C54/caliber_pivot_face.py``
"""

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import ContractModel

from opendata.data import mapping as mapping_module
from opendata.data.mapping import (
    load_mapping,
    mapping_as_json,
    mapping_sources,
    normalize_frame,
    require_domain_mapping,
    require_pivot,
)

MAPPINGS = Path("opendata/data/mappings")

#: 新浪资产负债表页的实测形状（C10 采样）：五个元数据列 + 两个科目列 + 两个行级日期列。
_SINA_FRAME = pd.DataFrame(
    {
        "报告日": ["2024-06-30"],
        "货币资金": [5.8e10],
        "应收账款": [1.2e8],
        "数据源": ["新浪"],
        "是否审计": ["是"],
        "公告日期": ["2024-08-30"],
        "币种": ["人民币"],
        "类型": ["合并报表"],
        "更新日期": ["2024-08-30"],
    }
)

#: 同形状，只把「更新日期」换成数值型（20240830）：passthrough 的 excluded 只在
#: 元数据列本身可转成数时才有行为差别，这一帧用来把那一面量出来。
_SINA_FRAME_NUMERIC_PAGE = _SINA_FRAME.assign(**{"更新日期": [20240830]})

_EM_FRAME = pd.DataFrame(
    {
        "SECUCODE": ["600519.SH"],
        "SECURITY_NAME_ABBR": ["贵州茅台"],
        "REPORT_DATE": ["2024-06-30"],
        "NOTICE_DATE": ["2024-08-30"],
        "EPSJB": [23.88],
        "ROEJQ": [16.9],
    }
)


def _face(title: str) -> None:
    print(f"\n=== {title} ===")


def _reload() -> None:
    """Drop the loader's per-source memo (these faces rewrite the table)."""
    mapping_module.load_mapping.cache_clear()


def _refuses(call: Callable[[], Any]) -> str:
    """Run one call and report what it raised - or that nothing did."""
    try:
        result = call()
    except (LookupError, ValueError, RuntimeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return f"没有拒绝 -> {result!r}"


def _sina_items(frame: pd.DataFrame) -> list[str]:
    """熔一帧，返回摊出来的 item 列（用出厂表）.

    行属性走 ``model_dump()``：熔解器的返回类型是 ``FetchResult``（契约模型或
    帧），面要读的是摊出来的字段名，不是某个具体模型的属性。
    """
    from opendata.data.providers.akshare.models.financial_statement import (
        AkshareFinancialStatementFetcher,
    )

    fetcher = AkshareFinancialStatementFetcher()
    params = fetcher.transform_query(symbol="600519", statement_type="资产负债表")
    rows = cast("Sequence[ContractModel]", fetcher.transform_data(frame, params))
    return [str(row.model_dump()["item"]) for row in rows]


def face_a_legs_and_declarations() -> None:
    """面 A：注册在册的 financial 腿 vs 表里声明的腿——覆盖的到底是哪几条腿."""
    from opendata.data.providers import register_providers
    from opendata.pipeline import alert_matrix
    from opendata.pipeline.freshness import freshness_field

    register_providers()
    legs = alert_matrix.registered_legs()
    declared = {source: set(load_mapping(source).domains) for source in mapping_sources()}
    _face("面 A registered legs vs 映射表声明（含新鲜度可读性）")
    for domain in ("financial_statement", "financial_indicator"):
        for source in legs[domain]:
            in_table = domain in declared.get(source, set())
            domain_mapping = require_domain_mapping(source, domain) if in_table else None
            pivot = domain_mapping.pivot if domain_mapping is not None else None
            readable = freshness_field(domain) in getattr(domain_mapping, "fields", {})
            print(
                f"  {domain}/{source:<8} 表内={'yes' if in_table else 'no '} "
                f"pivot={getattr(pivot, 'mode', '-'):<12} "
                f"freshness({freshness_field(domain)})可读={readable}"
            )
        for source in sorted(set(declared) - set(legs[domain])):
            print(f"  （{source} 没有注册 {domain} 腿，表里也没有——不是漏声明）")


def face_b_exports() -> None:
    """面 B：三条腿的 pivot 声明随映射一起导出（证据能指名科目全集）."""
    _face("面 B mapping_as_json 导出的 pivot 块")
    for source, domain in (
        ("ths", "financial_statement"),
        ("akshare", "financial_statement"),
        ("akshare", "financial_indicator"),
    ):
        rendered = json.loads(mapping_as_json(require_domain_mapping(source, domain)))
        pivot = rendered["pivot"]
        print(f"  {source}/{domain}")
        print(
            f"    mode={pivot['mode']} item_field={pivot['item_field']} "
            f"value_field={pivot['value_field']} group_field={pivot['group_field']}"
        )
        print(f"    row_columns={pivot['row_columns']} excluded={pivot['excluded']}")
        if pivot["groups"]:
            counts = {group: len(columns) for group, columns in pivot["groups"].items()}
            print(f"    groups={counts}")
        print(f"    key={rendered['key']}")
        print(
            f"    adjust={rendered['adjust']} suspension={rendered['suspension']} "
            f"denominator={rendered['denominator']}"
        )
    one_to_one = json.loads(mapping_as_json(require_domain_mapping("ths", "stock_daily")))
    print(f"  1:1 域 ths/stock_daily 导出的 pivot = {one_to_one['pivot']}（不是空声明）")


def face_c_live_reads() -> None:
    """面 C：三个熔解器现场从表里取词表（现场调用，不是复述用例）."""
    from opendata.data.providers.akshare.models.financial_indicator import (
        AkshareFinancialIndicatorFetcher,
    )
    from opendata_fuyao.endpoints import financial_statement_items

    _face("面 C 三个熔解器的现场读数")
    income = financial_statement_items("income")
    print(f"  ths(fuyao) income 科目数 = {len(income)}；首两个 = {income[:2]}；末个 = {income[-1]}")
    print(f"  akshare sina 熔解出的 item = {_sina_items(_SINA_FRAME)}")
    fetcher = AkshareFinancialIndicatorFetcher()
    rows = cast(
        "Sequence[ContractModel]",
        fetcher.transform_data(_EM_FRAME, fetcher.transform_query(symbol="600519")),
    )
    melted = [row.model_dump() for row in rows]
    print(f"  akshare em 熔解出的 indicator = {[row['indicator'] for row in melted]}")
    first = melted[0]
    print(f"    行数={len(melted)} 首行 report_period={first['report_period']}")
    print(f"                 首行 announce_date={first['announce_date']}")


def face_d_refusals() -> None:
    """面 D：五个拒绝面——判据的反面必须会红."""
    cases: list[tuple[str, Callable[[], Any]]] = [
        ("1:1 域问 pivot                ", lambda: require_pivot("ths", "stock_daily")),
        (
            "未登记的报表组              ",
            lambda: require_pivot("ths", "financial_statement").item_columns("shadow"),
        ),
        (
            "groups 模式问 line_items    ",
            lambda: require_pivot("ths", "financial_statement").line_items(["net_profit"]),
        ),
        (
            "未登记的行级列              ",
            lambda: require_pivot("akshare", "financial_statement").row_column("currency"),
        ),
        (
            "宽帧走 1:1 投影             ",
            lambda: normalize_frame(
                _SINA_FRAME, require_domain_mapping("akshare", "financial_statement")
            ),
        ),
    ]
    _face("面 D 拒绝面（现场调用）")
    for label, call in cases:
        print(f"  {label}: {_refuses(call)[:200]}")


def face_e_table_moves_the_melt() -> None:
    """面 E：改表就改熔解结果——这一面把「声明」和「装饰」分开."""
    from opendata.data.providers.akshare.models.financial_statement import (
        AkshareFinancialStatementFetcher,
    )
    from opendata_fuyao.endpoints import financial_statement_items

    _face("面 E 反事实：表改一行，熔解结果跟着动")
    original_dir = mapping_module._MAPPINGS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        try:
            mapping_module._MAPPINGS_DIR = Path(tmp)
            _reload()
            fetcher = AkshareFinancialStatementFetcher()
            params = fetcher.transform_query(symbol="600519", statement_type="资产负债表")

            def melt(frame: pd.DataFrame) -> list[dict[str, Any]]:
                """一帧熔成契约行（按字段名读，见 _sina_items 的同款理由）."""
                rows = cast("Sequence[ContractModel]", fetcher.transform_data(frame, params))
                return [row.model_dump() for row in rows]

            _write_akshare(tmp, exclude=["数据源", "是否审计", "币种", "类型", "更新日期"])
            kept = {str(row["item"]) for row in melt(_SINA_FRAME_NUMERIC_PAGE)}
            _write_akshare(tmp, exclude=["数据源", "是否审计", "币种", "类型"])
            added = {str(row["item"]) for row in melt(_SINA_FRAME_NUMERIC_PAGE)}
            print(f"  数值型「更新日期」仍在 excluded -> item = {sorted(kept)}")
            print(f"  从 excluded 里去掉它            -> item = {sorted(added)}")
            print(f"  差集 = {sorted(added - kept)}（表少一行声明，熔解就多一个科目）")

            _write_akshare(tmp, exclude=["数据源"], period_column="不存在的列")
            rows = melt(_SINA_FRAME)
            print(f"  把 report_period 的列名改错        -> 行数 = {len(rows)}")
            print("     （行级列名也是表说了算：读不到报告期末就没有行）")

            ths = yaml.safe_load((MAPPINGS / "ths.yaml").read_text(encoding="utf-8"))
            groups = dict(ths["domains"]["financial_statement"]["pivot"]["groups"])
            groups["income"] = ["only_this_item"]
            _write_ths(tmp, groups)
            print(f"  改 ths 表的 income 组              -> {financial_statement_items('income')}")
        finally:
            mapping_module._MAPPINGS_DIR = original_dir
            _reload()


def face_f_no_financial_ods() -> None:
    """面 F：表里的 financial fields 块是摊平后的长表视图，不是观测到的 ods schema."""
    _face("面 F 表内容 vs 库事实（本库无 ods_financial_* 表）")
    for source in ("ths", "akshare"):
        for domain in ("financial_statement", "financial_indicator"):
            try:
                domain_mapping = require_domain_mapping(source, domain)
            except LookupError:
                print(f"  {source}/{domain}: 表内无此腿")
                continue
            print(
                f"  {source}/{domain}: fields={sorted(domain_mapping.fields)}"
                f"（长表视图；本库没有 ods_{domain}_{source} 表可对照）"
            )


def face_g_declared_but_unread() -> None:
    """面 G：今天不改变熔解输出的两格声明，原样登记（不算承载，也不粉饰）."""
    _face("面 G 声明里今天不改变熔解输出的两格")
    five = sorted(require_pivot("akshare", "financial_statement").excluded)
    print(f"  sina 腿 excluded = {five}")
    before = _sina_items(_SINA_FRAME)
    original_dir = mapping_module._MAPPINGS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        try:
            mapping_module._MAPPINGS_DIR = Path(tmp)
            _reload()
            _write_akshare(tmp, exclude=[])
            after = _sina_items(_SINA_FRAME)
        finally:
            mapping_module._MAPPINGS_DIR = original_dir
            _reload()
    print(f"  把 excluded 清空 -> item = {after}（与出厂 {before} 相同）")
    print("  ⇒ 挡住字符串型元数据列的是数值强转，不是 excluded；excluded 的行为差别")
    print("     只在元数据列本身可转成数时出现（面 E 第一组）。这一格按『口径要先声明』")
    print("     登记，不按『今天它改变输出』主张。")
    print("  group_field 的读者是加载器（组必须是业务键字段）与导出面；三个熔解器都不读")
    print("     它——组值来自请求参数，不来自响应列。")


def _pivot_block(mode: str, **changes: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": mode,
        "item_field": "item",
        "value_field": "value",
        "group_field": "statement_type",
    }
    base.update(changes)
    return base


def _write_akshare(tmp: str, exclude: list[str], period_column: str = "报告日") -> None:
    source = yaml.safe_load((MAPPINGS / "akshare.yaml").read_text(encoding="utf-8"))
    domain = dict(source["domains"]["financial_statement"])
    domain["pivot"] = _pivot_block(
        "passthrough",
        row_columns={"report_period": period_column, "announce_date": "公告日期"},
        excluded=exclude,
    )
    _dump(tmp, "akshare", {"financial_statement": domain})


def _write_ths(tmp: str, groups: dict[str, list[str]]) -> None:
    source = yaml.safe_load((MAPPINGS / "ths.yaml").read_text(encoding="utf-8"))
    domain = dict(source["domains"]["financial_statement"])
    domain["pivot"] = _pivot_block(
        "groups",
        row_columns={"announce_date": "report_date_ms"},
        groups=groups,
    )
    _dump(tmp, "ths", {"financial_statement": domain})


def _dump(tmp: str, source: str, domains: dict[str, Any]) -> None:
    payload = {"version": 1, "source": source, "domains": domains}
    (Path(tmp) / f"{source}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _reload()


def main() -> int:
    """Run every face."""
    face_a_legs_and_declarations()
    face_b_exports()
    face_c_live_reads()
    face_d_refusals()
    face_e_table_moves_the_melt()
    face_f_no_financial_ods()
    face_g_declared_but_unread()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
