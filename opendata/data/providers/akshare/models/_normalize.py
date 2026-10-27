"""Shared key/date normalization for the akshare-source fetchers.

The single owner of symbol key normalization (design §4.2):
``600519`` <-> ``600519.SH``. The contract layer stores plain
six-digit codes; source-specific spellings are built here per call.
:func:`within_window` covers the second shared job: the sina daily-line
and dividend upstreams have no window parameter, so each of them
filters the caller's range locally.
"""

from datetime import date
from typing import Any, cast

import pandas as pd

_SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688")
_SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")
_BJ_A_SHARE_PREFIXES = ("4", "8", "920")


def plain_symbol(symbol: str) -> str:
    """Normalize a symbol to the plain six-digit code.

    Args:
        symbol: Input symbol; accepts plain codes and any
            ``.SH``/``.SZ``/``.BJ``-suffixed spelling.

    Returns:
        The bare code with any exchange suffix stripped.
    """
    return symbol.split(".", maxsplit=1)[0].strip()


def em_symbol(symbol: str) -> str:
    """Spell a symbol in eastmoney F10 form (``600519.SH``).

    The suffix is inferred from the first digit: 6 -> SH, 0/3 -> SZ,
    4/8 -> BJ; anything else defaults to SH and relies on the
    upstream error for unknown codes.

    Args:
        symbol: Input symbol (plain or suffixed).

    Returns:
        The suffixed eastmoney spelling.
    """
    code = plain_symbol(symbol)
    if code.startswith(("0", "3")):
        suffix = "SZ"
    elif code.startswith(("4", "8")):
        suffix = "BJ"
    else:
        suffix = "SH"
    return f"{code}.{suffix}"


def sina_symbol(symbol: str) -> str:
    """Spell a supported mainland A-share code in Sina report form.

    Args:
        symbol: Six-digit A-share code, optionally suffixed with ``.SH`` or ``.SZ``.

    Returns:
        The lowercase-prefixed Sina spelling (for example, ``sh600519``).

    Raises:
        ValueError: If the code is malformed, belongs to an unsupported exchange
            or code family, or has a suffix inconsistent with its code family.
    """
    parts = symbol.strip().split(".")
    if len(parts) not in {1, 2}:
        raise ValueError(f"cannot derive the sina symbol from malformed code: {symbol!r}")

    code = parts[0].strip()
    explicit_exchange = parts[1].strip().upper() if len(parts) == 2 else None
    if len(code) != 6 or not code.isascii() or not code.isdecimal():
        raise ValueError(f"cannot derive the sina symbol from malformed code: {symbol!r}")

    if code.startswith(_BJ_A_SHARE_PREFIXES):
        raise ValueError(
            f"cannot derive the sina symbol for Beijing Stock Exchange code: {symbol!r}"
        )

    if code.startswith(_SH_A_SHARE_PREFIXES):
        exchange = "SH"
    elif code.startswith(_SZ_A_SHARE_PREFIXES):
        exchange = "SZ"
    else:
        raise ValueError(f"cannot derive the sina symbol for unsupported A-share code: {symbol!r}")

    if explicit_exchange not in {None, "SH", "SZ"}:
        raise ValueError(f"cannot derive the sina symbol from unsupported suffix: {symbol!r}")
    if explicit_exchange is not None and explicit_exchange != exchange:
        raise ValueError(f"Sina exchange suffix conflicts with code prefix: {symbol!r}")
    prefix = exchange.lower()
    return f"{prefix}{code}"


def within_window(day: date, start: date | None, end: date | None) -> bool:
    """Whether a row date falls inside the window the caller asked for.

    The sina-backed upstreams publish a symbol's whole history and take
    no window argument, so ``normalize()`` is where a ``start_date`` /
    ``end_date`` request gets honored. Both ends are inclusive; either
    one being None leaves that side open.

    Args:
        day: The row's trade date or ex-date.
        start: Lower bound, None for open-ended.
        end: Upper bound, None for open-ended.

    Returns:
        True when the row belongs in the result.
    """
    if start is not None and day < start:
        return False
    return not (end is not None and day > end)


def as_date(value: object) -> date | None:
    """Coerce one cell to a date, None when unparsable.

    Args:
        value: Cell value in any upstream spelling (``YYYY-MM-DD``,
            ``YYYYMMDD``, datetime, ``date``).

    Returns:
        The parsed date, or None when the value does not parse.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    # A cell read from an upstream DataFrame has a dynamic scalar type;
    # pandas owns coercion and invalid-value handling at this boundary.
    parsed = pd.to_datetime(cast("Any", value), errors="coerce")
    if pd.isna(parsed):
        return None
    return cast("date", parsed.date())
