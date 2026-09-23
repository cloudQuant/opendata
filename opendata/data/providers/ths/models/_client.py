"""fuyao client plumbing shared by the ths fetchers (A3.4).

Keeps three concerns out of the domain modules: credential resolution
(environment first, then application settings so ``.env`` works), a
short-lived sync client per call, and the plain-code to ``thscode``
resolution. The latter is never guessed: a code that already carries the
exchange suffix is used as-is, and anything else is resolved through the
upstream ticker search - exactly one exact ``ticker`` match, otherwise the
call fails closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opendata.core.config import settings
from opendata_fuyao import FuyaoCredentials, FuyaoHttpClient
from opendata_fuyao.endpoints import search_instruments

if TYPE_CHECKING:
    from collections.abc import Iterator


#: 股票代码的交易所后缀（指数等其它后缀不参与解析）。
_STOCK_EXCHANGE_SUFFIXES = frozenset({"SH", "SZ", "BJ"})


class ThsProviderError(RuntimeError):
    """Stable failures of the ths provider adapter."""


def credentials() -> FuyaoCredentials:
    """Resolve fuyao credentials, failing closed when unconfigured.

    Returns:
        Credentials from the environment, or from application settings
        (which reads ``.env``).

    Raises:
        ThsProviderError: No key is configured.
    """
    resolved = FuyaoCredentials.from_environment()
    if resolved is None:
        resolved = FuyaoCredentials.from_settings(
            api_key=settings.fuyao_api_key,
            base_url=settings.fuyao_api_base_url,
        )
    if resolved is None:
        raise ThsProviderError("THS_NOT_CONFIGURED")
    return resolved


@contextmanager
def client(*, timeout_seconds: float | None = None) -> Iterator[FuyaoHttpClient]:
    """Build a short-lived client for one fetch call.

    Args:
        timeout_seconds: Optional per-call timeout override.

    Yields:
        A configured client, closed on exit.
    """
    resolved = credentials()
    active = (
        FuyaoHttpClient(credentials=resolved)
        if timeout_seconds is None
        else FuyaoHttpClient(credentials=resolved, timeout_seconds=timeout_seconds)
    )
    try:
        yield active
    finally:
        active.close()


def resolve_code(active: FuyaoHttpClient, symbol: str) -> str:
    """Resolve a symbol to the upstream ``thscode`` form.

    Args:
        active: Client used for the lookup when needed.
        symbol: ``600519``-style code, or an already-qualified
            ``600519.SH`` code.

    Returns:
        The qualified code.

    Raises:
        ThsProviderError: The symbol is blank, or the lookup does not
            produce exactly one exact ticker match (the upstream ``ticker``
            field is the plain code, so index codes - which share the
            numeric prefix but carry a non-stock suffix - cannot slip in).
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ThsProviderError("THS_SYMBOL_INVALID")
    candidate = symbol.strip().upper()
    if "." in candidate:
        return candidate
    matches = [
        code
        for instrument in search_instruments(active, query=candidate, limit=10)
        for code in [instrument.symbol.upper()]
        if code.startswith(f"{candidate}.") and code.rpartition(".")[2] in _STOCK_EXCHANGE_SUFFIXES
    ]
    if len(matches) != 1:
        raise ThsProviderError("THS_SYMBOL_UNRESOLVED")
    return matches[0]


__all__ = ["ThsProviderError", "client", "credentials", "resolve_code"]
