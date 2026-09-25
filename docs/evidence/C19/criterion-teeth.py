"""C19: does the rewritten live criterion actually have teeth, or only the old one did.

C19 replaced a live assertion whose premise measurement disproved ("the page I
just read must carry listing dates" - the answer turns out to be a routing
coin). A swap like that is only honest if the new predicate catches something
the old one did not, so this script feeds the three catalog shapes through both
predicates and reports which one passes what.

The shapes are the null profiles C19 measured, rebuilt on a real page: ``全值``
is the 8-null face the dated catalog answers with (C14 measured 9 new listings
with no date yet), ``全空`` is the 22-of-36 hollow face, and ``散值`` - half the
rows dated - is the shape the fleet never once answered with. They are built by
rewriting ``list_date`` in both directions rather than by waiting for a coin to
land, because the point is the discriminative power of the predicate, not
another sample of the flip. The fill date is C14's archived listing date for
600519; its value is irrelevant to both predicates, which only count nulls.

Row counts come from one live read of the real catalog so the shapes are built
on the contract type the test asserts over; nothing is written to the warehouse.

Run (stdout redirected to the sibling ``.txt``)::

    python docs/evidence/C19/criterion-teeth.py
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from opendata.data.providers import register_providers
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.models import Instrument

#: Rows a page may leave undated and still count as fully dated - the same
#: tolerance the live assertion uses (C14 measured 9 new listings without one).
NULL_TOLERANCE = 20

#: Rows left undated by the ``全值`` face, as measured on the dated catalog.
DATED_PAGE_NULLS = 8

#: Stand-in listing date for the rows a shape keeps dated.
FILL_DATE = date(2001, 8, 27)


def shaped(rows: list[Instrument], nulls: int) -> list[Instrument]:
    """Return ``rows`` rewritten so exactly ``nulls`` of them lack a date."""
    keep = len(rows) - nulls
    return [
        row.model_copy(update={"list_date": FILL_DATE if index < keep else None})
        for index, row in enumerate(rows)
    ]


def old_predicate(nulls: int, total: int) -> bool:
    """C14's assertion: the page read this time must be covered."""
    return nulls <= NULL_TOLERANCE


def new_predicate(nulls: int, total: int) -> bool:
    """C19's assertion: the page must be one of the two measured shapes."""
    return nulls <= NULL_TOLERANCE or nulls == total


def main() -> int:
    """Compare both predicates across the three catalog shapes."""
    register_providers()
    routed = get_registry().resolve_domain("instrument", source="ths")
    rows = list(routed.fetch(asset_type="a-share"))
    total = len(rows)
    live_nulls = sum(1 for row in rows if row.list_date is None)
    print(f"[1] 真机读到的 a-share 目录：{total} 行，list_date 空 {live_nulls} 行")
    drawn = "满" if live_nulls <= NULL_TOLERANCE else "空"
    print(
        f"    这一次抽到的是{drawn}页 ⇒ 本轮判据 "
        f"{'通过' if new_predicate(live_nulls, total) else '失败'}"
        f" / C14 判据 {'通过' if old_predicate(live_nulls, total) else '失败'}"
    )

    shapes = (
        (f"全值（实测空 {DATED_PAGE_NULLS} 行）", DATED_PAGE_NULLS),
        ("全空（实测 22/36 次）", total),
        ("散值（36 次里从未出现）", total // 2),
    )
    print("\n[2] 三种形态逐条过两条判据（● 通过 / ○ 失败）")
    print(f"    {'形态':<24} {'实际空行数':>9}  C14 判据    本轮判据")
    for label, target in shapes:
        nulls = sum(1 for row in shaped(rows, target) if row.list_date is None)
        flag = "" if nulls == target else "←构造未达标"
        old_pass = old_predicate(nulls, total)
        new_pass = new_predicate(nulls, total)
        print(
            f"    {label:<24} {nulls:>9} {flag:<11}"
            f"{'● 通过' if old_pass else '○ 失败':<9}{'● 通过' if new_pass else '○ 失败'}"
        )

    print(
        "\n[3] 读法：C14 判据在「全空」上失败，而全空是路由的结果（36 次里 22 次），"
        "与被判的数据对不对无关 ⇒ 它测的是抽样而不是判据；\n"
        "    本轮判据对两种实测形态都通过，只对「散值」失败 —— 那正是上游若开始按行丢日期"
        "会出现的形状，旧判据只有恰好抽到满页时才看得见它。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
