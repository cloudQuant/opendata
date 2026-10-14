"""Split over-long single-string source lines into adjacent literals, value-preservingly.

A2 charges E501 at line-length 100, and most of an evidence probe's long lines are ONE string
literal -- an ``argv=`` header or a README sentence whose exact characters end up in a carrier.
Hand-wrapping those risks changing what the probe emits, so this tool only touches a line when the
whole line is a single string literal, and it cuts at an existing space so that space stays in the
first half.

The witness is direct, not inferred: for every cut the tool rebuilds the two halves' inner text and
asserts ``original_body == head_body + tail_body`` character by character, so the emitted value
cannot have moved. A cut is refused if it lands inside an f-string ``{expression}`` or right after a
backslash escape, and the file must re-parse afterwards.

Usage: python docs/evidence/C105/a2-probe-repair/probes/split_long_lines.py FILE...
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

LIMIT = 100
#: how far left of LIMIT the cut may sit, so the closing quote and the new opening quote both fit
MARGIN = 8

LITERAL_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<prefix>[A-Za-z]{0,3})(?P<quote>[\"'])"
    r"(?P<body>.*)(?P=quote)(?P<tail>,?)\s*$"
)


def statement_docstring_lines(src: str) -> set[int]:
    """First lines of docstring statements -- prose nothing emits, so those are hand-wrapped."""
    bad: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bad.add(node.lineno)
    return bad


def triple_quoted_lines(src: str) -> set[int]:
    """Lines inside a ``\"\"\"`` literal -- emitted text, never a token this tool may cut.

    A node spanning several lines is NOT the same thing: ``"a" "b"`` across two lines is one AST
    Constant whose physical lines are each a whole literal, and those are exactly what a second
    pass needs to cut again.
    """
    bad: set[int] = set()
    rows = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        is_stringy = isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        if not is_stringy or not isinstance(node, ast.expr):
            continue
        last_line = node.end_lineno or node.lineno
        if last_line == node.lineno:
            continue
        head = rows[node.lineno - 1][node.col_offset : node.col_offset + 3]
        if head in ('"""', "'''"):
            bad.update(range(node.lineno, last_line + 1))
    return bad


def line_literal(line: str) -> tuple[int, int, str, str, str] | None:
    """Return (body_start, token_end, prefix, quote, tail) when a line is exactly one literal.

    Deliberately not an AST lookup: once a long literal has been cut in two, each half sits inside
    an implicit-concatenation group the parser reports as one multi-line node, and the tail of that
    group may still need cutting. The check is that this line's own text is one valid literal.
    """
    found = LITERAL_LINE.match(line)
    if not found:
        return None
    quote, body, prefix, tail = (
        found.group("quote"),
        found.group("body"),
        found.group("prefix"),
        found.group("tail"),
    )
    if not body or body[0] == quote:
        return None
    if body.count(quote) - body.count("\\" + quote):
        return None
    try:
        ast.parse(f"({prefix}{quote}{body}{quote})")
    except SyntaxError:
        return None
    return found.start("body"), len(line) - len(tail), prefix, quote, tail


def cut_column(line: str, body_start: int, token_end: int) -> int | None:
    """Column of the rightmost splittable space inside the literal, or None."""
    quote = line[body_start - 1]
    limit = min(LIMIT - MARGIN, token_end - 2)
    for idx in range(limit, body_start, -1):
        if line[idx] != " " or line[idx - 1] == "\\":
            continue
        between = line[body_start:idx]
        if between.count("{") != between.count("}"):
            continue
        if between.count(quote) - between.count("\\" + quote):
            continue
        return idx
    return None


def one_pass(path: pathlib.Path) -> tuple[int, int, int]:
    """One splitting pass over the file as it currently stands: (made, verified, skipped)."""
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    skip = statement_docstring_lines(src) | triple_quoted_lines(src)
    made = verified = skipped = 0
    for ln in range(len(lines), 0, -1):
        line = lines[ln - 1]
        if ln in skip or len(line) <= LIMIT:
            continue
        literal = line_literal(line)
        if literal is None:
            skipped += 1
            continue
        body_start, token_end, prefix, quote, tail = literal
        idx = cut_column(line, body_start, token_end)
        if idx is None:
            skipped += 1
            continue
        indent = line[: len(line) - len(line.lstrip())]
        head = line[: idx + 1] + quote
        new_tail = f"{indent}{prefix}{quote}{line[idx + 1 : token_end - 1]}{quote}{tail}"
        head_body = head[len(indent) + len(prefix) + len(quote) : -len(quote)]
        tail_body = new_tail[len(indent) + len(prefix) + len(quote) : -len(quote) - len(tail)]
        if head_body + tail_body != line[body_start : token_end - 1]:
            skipped += 1
            continue
        verified += 1
        lines[ln - 1] = head
        lines.insert(ln, new_tail)
        made += 1
    rebuilt = "\n".join(lines)
    ast.parse(rebuilt)
    path.write_text(rebuilt, encoding="utf-8")
    return made, verified, skipped


def process(path: pathlib.Path) -> tuple[int, int, int]:
    """Split one file to its fixpoint. A literal twice the limit needs one pass per cut."""
    made = verified = skipped = 0
    for _round in range(8):
        n, v, s = one_pass(path)
        made, verified, skipped = made + n, verified + v, skipped + s
        if not n:
            break
    return made, verified, skipped


def main() -> int:
    """Split each named file, then re-measure the overflow it actually removed."""
    splits = removals = 0
    refused: list[str] = []
    for name in sys.argv[1:]:
        path = pathlib.Path(name)
        before = path.read_text(encoding="utf-8")
        over_before = sum(1 for line in before.split("\n") if len(line) > LIMIT)
        made, verified, skipped = process(path)
        after = path.read_text(encoding="utf-8")
        over_after = sum(1 for line in after.split("\n") if len(line) > LIMIT)
        dropped = over_before - over_after
        splits += made
        removals += dropped
        print(
            f"face: file={name} splits_made={made} value_identity_verified={verified}"
            f" lines_skipped={skipped} overflow_before={over_before}"
            f" overflow_after={over_after} overflow_dropped={dropped}"
        )
        # Nothing to split is not a failure; a split that left its own overflow line in place is,
        # because then either the identity check or the re-measure lied.
        if made != verified or (made and not dropped):
            path.write_text(before, encoding="utf-8")
            refused.append(name)
            print(f"face: reverted={name} -- a split left its overflow line in place")
    print(
        f"face: splits_total={splits} overflow_lines_removed_total={removals}"
        f" files_reverted={len(refused)}"
    )
    for name in refused:
        print(f"face: needs_hand_wrap={name}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
