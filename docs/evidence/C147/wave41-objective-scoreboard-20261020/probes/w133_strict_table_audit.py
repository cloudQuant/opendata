"""Stricter audit of the shared table windows: do any carry identifiers or operators?

``w132`` splits shared six-line windows into logic (a statement keyword matches) and table (nothing
matches). That predicate can miss a call or an arithmetic expression inside a six-line fragment.
This face re-derives the same windows with ``w132``'s own readers and applies a tokenizer-based
predicate: outside strings, comments and container scaffolding, a line is literal-only when it
contains no identifier other than ``True``/``False``/``None`` and no arithmetic or comparison
operator. Scaffolding covers container bindings and typed headers whose right side is a
typing/container subscript (``name: Literal[``); an annotated expression with any other right
side is still examined, and the forged ``value: compute([`` control proves that. It reports how
many shared table windows survive, and proves two-sidedness on forged blocks: a six-line block
of calls must be flagged even though ``w132``'s regex misses it, and an untouched literal block
must not be.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w133_strict_table_audit.txt"
W132 = REPO / BUNDLE / "probes" / "w132_clean_room_overlap.py"
EXPECTED_TABLE = 293
SKIP = (
    tokenize.STRING,
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
)
LITERAL_NAMES = {"True", "False", "None"}
ARITH = {
    "+",
    "-",
    "*",
    "/",
    "%",
    "**",
    "//",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "|",
    "&",
    "^",
    "<<",
    ">>",
    "@",
    "~",
}
TYPE_HEAD = r"(?:Literal|Optional|Union|tuple|list|dict|set|Sequence|Mapping)\["
NARROW_SCAFFOLD = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\s*=\s*[A-Za-z_.]*[\[{(]|[\]})\),\s]+)$")
SCAFFOLD = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\s*=\s*[A-Za-z_.]*[\[{(]"
    r"|[A-Za-z_][A-Za-z0-9_]*\s*:\s*" + TYPE_HEAD + r"$"
    r"|[\]})\),\s]+)$"
)


def _load_w132() -> Any:  # noqa: ANN401
    spec = importlib.util.spec_from_file_location("w132_overlap_readers", W132)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import overlap readers {W132}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w132_overlap_readers"] = module
    spec.loader.exec_module(module)
    return module


def _tokens(line: str) -> list[tuple[int, str]]:
    """Tokenize one fragment line, keeping tokens collected before an unterminated-bracket error."""
    out: list[tuple[int, str]] = []
    generator = tokenize.generate_tokens(io.StringIO(line).readline)
    try:
        for token in generator:
            if token.type in SKIP:
                continue
            out.append((token.type, token.string))
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _expressive(line: str, scaffold: re.Pattern[str] = SCAFFOLD) -> bool:
    """Whether a line carries an identifier, call, attribute or arithmetic outside strings."""
    if scaffold.match(line):
        return False
    tokens = _tokens(line)
    for index, (kind, text) in enumerate(tokens):
        if kind == tokenize.NAME and text not in LITERAL_NAMES:
            return True
        if kind == tokenize.OP and text in ARITH:
            signed = text in {"+", "-"} and index + 1 < len(tokens)
            if signed and tokens[index + 1][0] == tokenize.NUMBER:
                continue
            return True
    return False


def _table_windows(
    w132: Any,  # noqa: ANN401
) -> tuple[list[tuple[str, int, str, tuple[str, ...]]], int, int, int]:
    """Re-derive w132's split and return (table entries, logic count, files, windows)."""
    index, upstream_files, windows = w132._upstream_index()
    ours = w132._ours()
    table: list[tuple[str, int, str, tuple[str, ...]]] = []
    logic = 0
    for path in ours:
        lines = w132._normalized(path)
        for key, first_line in w132._windows(lines).items():
            hit = index.get(key)
            if hit is None:
                continue
            if any(w132.LOGIC.search(text) for text in key):
                logic += 1
                continue
            table.append((w132._label(path), first_line, hit, key))
    return table, logic, len(ours), windows


def _control(w132: Any) -> dict[str, bool]:  # noqa: ANN401
    """Two-sided controls: a call block bites, a literal block and a typing header do not."""
    literal = tuple(f'"k{index}": "v{index}",' for index in range(6))
    call = tuple(f'"k{index}": build_{index}(1),' for index in range(6))
    return {
        "literal_clean": not any(_expressive(text) for text in literal),
        "call_flagged": any(_expressive(text) for text in call),
        "w132_misses": not any(w132.LOGIC.search(text) for text in call),
        "header_scaffold": not _expressive("parameter: Literal["),
        "annotated_expression_flagged": _expressive("value: compute(["),
    }


def main(argv: list[str] | None = None) -> int:
    """Split the shared windows under the strict predicate, or replay the narrow first take."""
    parser = argparse.ArgumentParser(description="stricter table-window audit")
    parser.add_argument(
        "--narrow-scaffold",
        action="store_true",
        help="replay the first take: only container bindings count as scaffolding",
    )
    parser.add_argument(
        "--output",
        default="",
        help="carrier path; required with --narrow-scaffold to protect the green carrier",
    )
    args = parser.parse_args(argv)
    if args.narrow_scaffold and not args.output:
        print("w133_strict_table_audit_exit=2 # narrow mode requires --output")
        return 2
    scaffold = NARROW_SCAFFOLD if args.narrow_scaffold else SCAFFOLD
    output = Path(args.output) if args.output else CARRIER
    w132 = _load_w132()
    table, logic, our_files, windows = _table_windows(w132)
    expressive = [entry for entry in table if any(_expressive(text, scaffold) for text in entry[3])]
    control = _control(w132)
    samples = [f"{name}:{line} -> {hit}" for name, line, hit, _key in expressive[:5]]
    return_code = 0 if not expressive else 1
    lines = [
        f"mode={'narrow' if args.narrow_scaffold else 'refined'}",
        f"upstream_windows={windows} our_files={our_files} window={w132.WINDOW}",
        f"table_windows={len(table)} logic_windows={logic} expected_table={EXPECTED_TABLE}",
        f"strict_literal_only={len(table) - len(expressive)} expressive={len(expressive)}",
        f"expressive_samples={samples}",
        f"control literal_block_clean={control['literal_clean']} "
        f"call_block_flagged={control['call_flagged']} "
        f"w132_regex_misses_call={control['w132_misses']} "
        f"typing_header_scaffold={control['header_scaffold']} "
        f"annotated_expression_flagged={control['annotated_expression_flagged']}",
    ]
    arms = {
        "A_denominator_matches_w132": windows >= 50000
        and our_files >= 300
        and len(table) == EXPECTED_TABLE
        and logic == 0,
        "B_all_table_windows_are_literal_only": not expressive,
        "C_forged_call_block_is_expressive": control["call_flagged"],
        "D_literal_block_is_not_expressive": control["literal_clean"],
        "E_w132_regex_misses_the_forged_call": control["w132_misses"],
        "F_reading_is_not_vacuous": bool(table) and any(len(entry[3]) == 6 for entry in table),
        "G_typing_header_is_scaffolding": control["header_scaffold"],
        "H_annotated_expression_is_still_expressive": control["annotated_expression_flagged"],
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"strict_table_audit arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w133_strict_table_audit_exit={return_code}\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w133_strict_table_audit_exit={return_code}")
    return return_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
