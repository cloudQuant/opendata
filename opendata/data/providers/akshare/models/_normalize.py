"""Shared key/date normalization for the akshare-source fetchers.

The single owner of symbol key normalization (design §4.2):
``600519`` <-> ``600519.SH``. The contract layer stores plain
six-digit codes; source-specific spellings are built here per call.
"""

from datetime import date
from typing import cast

import pandas as pd


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
    """Spell a symbol in sina report form (``sh600519``).

    Args:
        symbol: Input symbol (plain or suffixed).

    Returns:
        The lowercase-prefixed sina spelling.
    """
    code = plain_symbol(symbol)
    prefix = "sz" if code.startswith(("0", "3")) else "sh"
    return f"{prefix}{code}"


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
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return cast("date", parsed.date())
