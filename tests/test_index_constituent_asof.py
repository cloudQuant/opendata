"""``index_constituent`` 的 ``as_of`` 口径，以及挂在实测读数上的转正判据（C34）.

本轮拍定（用户决策「钉成清单日并翻正」）：``as_of`` = **该源所发布清单的所属快照
日**，不是调用时刻。落地成四组离线可跑的规则：

1. **两条腿各自声明自己填的是哪一天**。ths 端点不发布清单日期（响应里唯一的时间戳
   是请求时刻），所以它的行是观测日，并把这个缺口写进 ``notes``；akshare 侧读中国
   证券指数网权重文件的 ``日期`` 列，填的是清单自己的日期。口径不同必须从 ``/sources``
   看得见，而不是被同一句注释盖过去（C26 的"静默"同一类）。
2. **akshare 侧确实从文件那一列取日期**，不是 ``date.today()``。
3. ``verified`` **不许越过归档读数**。本轮把样本从 C24 的 1 个指数加宽到 21 个，
   量出 4 个科创板代码两侧成员集合不同（3 个等量换入换出、1 个不等量），所以
   akshare 腿必须是 ``false``；把归档换成一份没有 MISMATCH 的读数，同一条规则反过来
   要求它是 ``true``。判据是双向的，不会因为"已经写了 false"而变绿。
4. **等量换入换出 ≠ 谁错了**：那是两份不同日期的清单在按季调样指数上的典型形状；
   不等量的那一条（科创综指）单独登记为待归因。``NO_READING`` 的 8 个代码不参与任何
   方向的结论（C28：量不出来不是通过，也不是不符）。

第 5 组钉住 C24 那句被泛化了的 note：``scripts/ops/akshare_fallback_cross_check.py``
只在 000300 一个代码上对照过，"成分集合逐只相同"不能读成跨指数成立。

真机读数在 ``docs/evidence/C34/index-constituent-generality-sweep.txt``（由
``docs/evidence/C34/index_constituent_generality_sweep.py`` 产生，重跑判定移动即
DRIFT、退出码 1）；这里只做离线回读，不联网。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
import pytest

from opendata.data.models import IndexConstituent
from opendata.data.providers import register_providers
from opendata.data.providers.akshare.models.index_constituent import (
    AkshareIndexConstituentFetcher,
)
from opendata.data.registry import authority_baseline, get_registry

if TYPE_CHECKING:
    from opendata.data.capability import Capability

REPO_ROOT = Path(__file__).resolve().parents[1]
SWEEP_ARCHIVE = REPO_ROOT / "docs/evidence/C34/index-constituent-generality-sweep.txt"
CROSS_CHECK = REPO_ROOT / "scripts/ops/akshare_fallback_cross_check.py"

#: ``  [PASS      ] 000300: ... 仅 ths=0 仅 akshare=0`` - the archive line carries
#: the verdict and the two gap counts, so the guard reads the numbers that decided
#: it rather than a prose summary.
_VERDICT_LINE = re.compile(r"^\s*\[(?P<verdict>PASS|MISMATCH|NO_READING)\s*\]\s+(?P<code>\S+?):")
_GAP_COUNTS = re.compile(r"仅 ths=(?P<only_ths>\d+) 仅 akshare=(?P<only_akshare>\d+)")

#: What the widened sample measured on 2026-09-26. Pinned as a set, not as a count:
#: the round's finding is *which* codes disagree (the quarterly-reconstituted 科创板
#: family), and a moved member is a new measurement rather than a re-spelling.
MISMATCH_CODES: frozenset[str] = frozenset({"000688", "000689", "000698", "000680"})
AGREED_CODES: frozenset[str] = frozenset(
    {"000300", "000016", "000905", "000852", "000010", "000913", "000928", "000934", "000032"}
)
UNREADABLE_CODES: frozenset[str] = frozenset(
    {"000988", "930050", "000963", "000825", "000922", "000919", "000949", "000801"}
)

#: The one code whose gap is not an equal swap, i.e. not the reconstitution shape.
#: It stays on the list until something attributes it - the round does not resolve it.
UNEQUAL_GAP_CODE = "000680"


def archived_readings() -> dict[str, tuple[str, int | None, int | None]]:
    """Read the C34 sweep archive: ``code -> (verdict, only_ths, only_akshare)``.

    Returns:
        One entry per swept index code. The two counts are ``None`` where the
        archive line has no membership reading (the refused codes).
    """
    text = SWEEP_ARCHIVE.read_text(encoding="utf-8")
    readings: dict[str, tuple[str, int | None, int | None]] = {}
    for line in text.splitlines():
        match = _VERDICT_LINE.match(line)
        if match is None:
            continue
        gaps = _GAP_COUNTS.search(line)
        readings[match.group("code")] = (
            match.group("verdict"),
            None if gaps is None else int(gaps.group("only_ths")),
            None if gaps is None else int(gaps.group("only_akshare")),
        )
    return readings


def verdict_of(status: str) -> set[str]:
    """The codes the archive recorded with one verdict."""
    return {code for code, reading in archived_readings().items() if reading[0] == status}


def leg(source: str) -> Capability:
    """The registered ``index_constituent`` capability of one source."""
    register_providers()
    capability = (
        get_registry()
        .resolve("index", "index_constituent", period="snapshot", market="cn", source=source)
        .capability
    )
    assert capability.domain == "index_constituent"
    return capability


class TestAsOfSaysWhichDateEachLegPublishes:
    """The decided 口径 is visible per leg, not averaged into one sentence."""

    def test_ths_leg_declares_that_its_endpoint_has_no_list_date(self):
        notes = leg("ths").notes
        assert "observation day" in notes
        assert "publishes no list date" in notes

    def test_akshare_leg_declares_the_list_date_it_carries(self):
        notes = leg("akshare").notes
        assert "as_of is the list's own data date" in notes

    def test_the_two_legs_do_not_claim_the_same_date(self):
        """The divergence is registered; a shared wording would hide it."""
        assert leg("ths").notes != leg("akshare").notes

    def test_the_contract_still_requires_a_date(self):
        """Why a source with no list date declares a gap instead of omitting it."""
        assert IndexConstituent.model_fields["as_of"].is_required()


class TestAkshareAsOfIsTheFilesOwnColumn:
    """The behavioral half of the 口径: the date comes from ``日期``."""

    RAW = pd.DataFrame(
        [
            {"日期": "2026-06-30", "指数代码": "000688", "成分券代码": "688002", "权重": 0.51},
            {"日期": "2026-06-30", "指数代码": "000688", "成分券代码": "688065", "权重": 0.42},
            {"日期": "2026-03-31", "指数代码": "000688", "成分券代码": "688072", "权重": 0.33},
            {"日期": None, "指数代码": "000688", "成分券代码": "688099", "权重": 0.22},
        ]
    )

    def rows(self) -> list[IndexConstituent]:
        """The weight file normalized, typed as the contract it claims to satisfy."""
        fetcher = AkshareIndexConstituentFetcher()
        query = fetcher.transform_query(symbol="000688")
        result = list(fetcher.transform_data(self.RAW, query))
        assert all(isinstance(row, IndexConstituent) for row in result)
        return cast("list[IndexConstituent]", result)

    def test_each_row_carries_its_own_files_date(self):
        rows = self.rows()
        assert [row.as_of for row in rows] == [
            date(2026, 6, 30),
            date(2026, 6, 30),
            date(2026, 3, 31),
        ]

    def test_the_call_day_is_never_written_into_as_of(self):
        """The failure mode this guard exists for: today stands in for the file."""
        assert all(row.as_of != date.today() for row in self.rows())

    def test_a_row_without_a_list_date_is_dropped_not_dated(self):
        assert [row.symbol for row in self.rows()] == ["688002", "688065", "688072"]


class TestVerifiedFollowsTheArchivedReading:
    """The flip is gated on the widened measurement in both directions."""

    def test_the_archive_holds_the_measured_three_way_split(self):
        assert verdict_of("PASS") == AGREED_CODES
        assert verdict_of("MISMATCH") == MISMATCH_CODES
        assert verdict_of("NO_READING") == UNREADABLE_CODES
        assert len(archived_readings()) == 21

    def test_the_akshare_leg_is_verified_only_if_the_sweep_came_out_clean(self):
        """Bidirectional: a clean re-run must flip it, a dirty one must not.

        Reading the verdicts out of the archive rather than hardcoding
        ``verified is False`` is what keeps this from becoming a monument to
        today's measurement - C24's precondition was "加宽样本的跨 vendor 等价读数",
        and this is that precondition executable. The non-emptiness of today's
        mismatch set is pinned by the test above, so this rule is not
        green-by-vacuity either way.
        """
        assert leg("akshare").verified is (verdict_of("MISMATCH") == set())

    def test_the_ths_leg_is_not_bent_by_the_fallbacks_gap(self):
        """The head leg's own qualification (C9) is a separate claim."""
        assert leg("ths").verified is True

    def test_the_row_still_ranks_the_second_source_in_the_table(self):
        """Unverified is not withdrawn: ``auto`` may use it the day it qualifies."""
        assert authority_baseline().get("index_constituent") == ("ths", "akshare")


class TestMismatchIsTheReconstitutionShape:
    """Equal swaps are two dates' lists; an unequal gap needs attribution."""

    def test_three_of_the_four_differ_by_an_equal_number_each_way(self):
        readings = archived_readings()
        equal = {
            code
            for code, reading in readings.items()
            if reading[1] is not None and reading[2] is not None and reading[1] == reading[2] > 0
        }

        assert equal == MISMATCH_CODES - {UNEQUAL_GAP_CODE}

    def test_the_unequal_gap_is_a_one_sided_extra_rather_than_a_rename(self):
        _, only_ths, only_akshare = archived_readings()[UNEQUAL_GAP_CODE]

        assert (only_ths, only_akshare) == (1, 0)

    def test_the_agreed_codes_agree_symbol_for_symbol(self):
        """``0 仅 ths / 0 仅 akshare`` - not "close enough"."""
        readings = archived_readings()

        assert {readings[code][1:] for code in AGREED_CODES} == {(0, 0)}

    def test_refused_codes_are_not_counted_as_agreement(self):
        """C28: a code one side cannot answer settles nothing either way."""
        readings = archived_readings()

        assert {readings[code] for code in UNREADABLE_CODES} == {("NO_READING", None, None)}


class TestSingleIndexReadingStaysNarrowlyClaimed:
    """C24's note said "成分集合逐只相同" on the strength of one probe symbol."""

    #: The note is an implicit-concatenation literal, so the whole ``Case(...)``
    #: block is read rather than one quoted piece of it.
    BLOCK_PATTERN = re.compile(r'Case\(\s*"index_constituent",.*?\n    \),', re.S)

    def note(self) -> str:
        match = self.BLOCK_PATTERN.search(CROSS_CHECK.read_text(encoding="utf-8"))
        assert match is not None
        return match.group(0)

    def test_the_note_says_which_call_it_measured_and_where_the_widening_lives(self):
        note = self.note()
        assert "该次调用" in note
        assert "docs/evidence/C34/" in note

    def test_the_note_does_not_generalize_one_symbol_to_every_index(self):
        assert "单代码 PASS 不等于跨指数成立" in self.note()


def test_archive_carries_the_run_it_came_from() -> None:
    """The record has to be attributable to a run, not to an editor (C-round rule)."""
    text = SWEEP_ARCHIVE.read_text(encoding="utf-8")
    head = text.splitlines()[:8]

    assert any(line.startswith("captured_at=") for line in head)
    assert any(line.startswith("branch=dev HEAD=") for line in head)
    assert text.count("SWEEP_EXIT=0") == 1


@pytest.mark.parametrize("status", ["PASS", "MISMATCH", "NO_READING"])
def test_every_verdict_class_is_populated(status: str) -> None:
    """A guard over an empty selection is the vacuity this family keeps catching."""
    assert verdict_of(status)
