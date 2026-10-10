#!/usr/bin/env python3
"""Compare the retired grep-shaped nosec census with the shipped comment-token census.

C86 witness instrument for AC-17|02. bandit keys ``# nosec`` on ``tokenize.COMMENT`` tokens and
honors none in a file it cannot tokenize, so a census that greps raw lines counts exemptions the
scanner never read: 4 sites inside byte literals in ``write_benchmark_evidence.py`` and, after
the fix itself, one site in this instrument's own docstring. Both arms are printed side by side
because a zero on the shipped arm is only worth anything next to the non-zero it replaced.

The comparison arm (grep) is rebuilt here from the module's own ``NOSEC_PAT`` because that shape
no longer exists in the instrument; the shipped arm calls ``_nosec_rows`` itself, so the numbers
can be disagreed with only by disagreeing with the gate probe.
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality.acceptance_item_probe import (  # noqa: E402
    NOSEC_PAT,
    Context,
    _bandit_census,
    _census_roots,
    _exclude_dirs,
    _excluded,
    _has_reason,
    _nosec_rows,
    load_context,
)

if TYPE_CHECKING:
    Rows = list[tuple[str, int, str]]
    Keys = collections.Counter[tuple[str, str]]

RULE_ID = re.compile(r"B\d{2,3}")


def grep_rows(ctx: Context, roots: list[str], excludes: list[str]) -> Rows:
    """Return the retired semantics: every raw line scanned, string literals included."""
    rows: Rows = []
    for root in roots:
        base = ctx.root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = str(path.relative_to(ctx.root))
            if _excluded(rel, excludes):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                hit = NOSEC_PAT.search(line)
                if hit:
                    rows.append((rel, i, hit.group(1).strip()))
    return rows


def declared(rows: Rows) -> Keys:
    """Count declared exemptions per (file, rule id) the way the probe does."""
    out: collections.Counter = collections.Counter()
    for rel, _i, rest in rows:
        for tid in set(re.findall(r"B\d+", rest)):
            out[(rel, tid)] += 1
    return out


def decorative(rows: Rows, raw: Keys) -> tuple[int, list[str]]:
    """Return exemption sites that name a rule bandit never reported on that file."""
    dec = declared(rows)
    sites = sum(max(0, n - raw.get((name, tid), 0)) for (name, tid), n in dec.items())
    sample = sorted(
        f"{name}:{tid} declared {n} / raw {raw.get((name, tid), 0)}"
        for (name, tid), n in dec.items()
        if n > raw.get((name, tid), 0)
    )
    return sites, sample


def main() -> int:
    """Print both censuses, the sites only the grep arm sees, and bandit's own totals."""
    ctx = load_context()
    roots = _census_roots(ctx)
    excludes = _exclude_dirs(ctx)
    shipped, unreadable = _nosec_rows(ctx, roots, excludes)
    retired = grep_rows(ctx, roots, excludes)
    shipped_keys = {(rel, i) for rel, i, _rest in shipped}
    dropped = sorted({(rel, i) for rel, i, _rest in retired} - shipped_keys)

    print(f"roots={', '.join(roots)}")
    print(f"grep_census_sites={len(retired)} comment_census_sites={len(shipped)}")
    print(f"untokenizable_files={unreadable}")
    print(f"grep_only_sites={len(dropped)}")
    cache: dict[str, list[str]] = {}
    for rel, lineno in dropped:
        if rel not in cache:
            cache[rel] = (ctx.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"  GREP-ONLY {rel}:{lineno}  {cache[rel][lineno - 1].strip()[:92]}")

    _kc, _kj, _ki, kept_keys, kept_suppressed = _bandit_census(ctx, ())
    _rc, _rj, _ri, raw_keys, _rs = _bandit_census(ctx, ("--ignore-nosec",))
    old_sites, old_sample = decorative(retired, raw_keys)
    new_sites, new_sample = decorative(shipped, raw_keys)
    print(f"decorative grep={old_sites} comment={new_sites}")
    for line in old_sample:
        print(f"  DECORATIVE-grep {line}")
    for line in new_sample:
        print(f"  DECORATIVE-comment {line}")
    old_no_id = [f"{rel}:{i}" for rel, i, rest in retired if not RULE_ID.search(rest)]
    new_no_id = [f"{rel}:{i}" for rel, i, rest in shipped if not RULE_ID.search(rest)]
    old_no_reason = [f"{rel}:{i}" for rel, i, rest in retired if not _has_reason(rest)]
    new_no_reason = [f"{rel}:{i}" for rel, i, rest in shipped if not _has_reason(rest)]
    print(f"without_rule_id grep={len(old_no_id)} comment={len(new_no_id)}")
    for line in old_no_id:
        print(f"  NO-RULE-ID-grep {line}")
    print(f"without_reason grep={len(old_no_reason)} comment={len(new_no_reason)}")
    for line in old_no_reason:
        print(f"  NO-REASON-grep {line}")
    print(
        f"bandit remaining={sum(kept_keys.values())} raw={sum(raw_keys.values())} "
        f"skipped_self_report={kept_suppressed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
