"""fuyao client plumbing shared by the ths fetchers (A3.4).

Keeps three concerns out of the domain modules: credential resolution
(environment first, then application settings so ``.env`` works), a
short-lived sync client per call, and the plain-code to ``thscode``
resolution. The latter is never guessed: a code that already carries the
exchange suffix is used as-is, and anything else is resolved through the
upstream ticker search - exactly one exact ``ticker`` match, otherwise the
call fails closed. Indices, futures and on-exchange funds resolve through a
separate route because the search endpoint is name-based there and plain codes
repeat across asset classes; see :func:`resolve_index_code`,
:func:`resolve_futures_code` and :func:`resolve_fund_code`. Option
contracts carry an opaque numeric ``thscode`` (the human-readable code
lives in ``ticker``) over a catalog too large to enumerate per call, so
they must be requested qualified; see :func:`resolve_option_code`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opendata.core.config import settings
from opendata.data.providers.ths import FuyaoCredentials, FuyaoHttpClient
from opendata.data.providers.ths.endpoints import (
    FUND_ASSET_TYPE,
    FUTURES_ASSET_TYPE,
    INDEX_ASSET_TYPE,
    MAX_LIST_LIMIT,
    list_instruments,
    search_instruments,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


#: 股票代码的交易所后缀（指数等其它后缀不参与解析）。
_STOCK_EXCHANGE_SUFFIXES = frozenset({"SH", "SZ", "BJ"})


class ThsProviderError(RuntimeError):
    """Stable failures of the ths provider adapter.

    Attributes:
        code: The stable failure identifier the adapter was raised with, with
            any trailing detail stripped. The sibling macro providers have
            carried ``code`` as a field since C21; ths kept it as prose only,
            which left every failure of the authoritative A-share source
            unclassifiable by the credential health plane.
    """

    def __init__(self, message: str) -> None:
        """Raise one stable failure.

        Args:
            message: The stable code, optionally followed by ``: detail``. The
                detail stays in the message for humans; :attr:`code` keeps only
                the code, so caller-controlled text cannot reach a report.
        """
        self.code = message.split(":", 1)[0].strip()
        super().__init__(message)


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
    candidate = _candidate_symbol(symbol)
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


def _candidate_symbol(symbol: str) -> str:
    """Trim and upper-case a caller-supplied code, failing closed on blank."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ThsProviderError("THS_SYMBOL_INVALID")
    return symbol.strip().upper()


def _resolve_by_listing(
    active: FuyaoHttpClient, candidate: str, *, asset_type: str, unresolved_code: str
) -> str:
    """Match a bare code against one asset class' listing (unique or fail)."""
    matches = [
        instrument.symbol.upper()
        for instrument in list_instruments(
            active, limit=MAX_LIST_LIMIT, offset=0, asset_type=asset_type
        )
        if instrument.symbol.upper().rpartition(".")[0] == candidate
    ]
    if len(matches) != 1:
        raise ThsProviderError(unresolved_code)
    return matches[0]


def resolve_index_code(active: FuyaoHttpClient, symbol: str) -> str:
    """Resolve an index symbol to the upstream ``thscode`` form.

    Index codes cannot go through :func:`resolve_code`: ticker search is
    name-based (a numeric query returns nothing for indices), and the
    suffixes differ (``SH`` / ``SZ`` / ``BJ`` / ``TI`` / ``CSI``). The
    upstream index universe is small enough to enumerate (about 1.4k
    instruments in one list call), so a bare code is matched against that
    listing and resolves only when exactly one index carries it.

    Args:
        active: Client used for the lookup when needed.
        symbol: ``000300``-style code, or an already-qualified
            ``000300.SH`` / ``886042.TI`` code.

    Returns:
        The qualified code.

    Raises:
        ThsProviderError: The symbol is blank, or the index universe does
            not contain exactly one match.
    """
    candidate = _candidate_symbol(symbol)
    if "." in candidate:
        return candidate
    return _resolve_by_listing(
        active,
        candidate,
        asset_type=INDEX_ASSET_TYPE,
        unresolved_code="THS_INDEX_SYMBOL_UNRESOLVED",
    )


def resolve_futures_code(active: FuyaoHttpClient, symbol: str) -> str:
    """Resolve a futures contract symbol to the upstream ``thscode`` form.

    Same route as the index one: the futures catalog lists about 1.1k
    contracts in a single call and every ``thscode`` prefix (``IF2610``,
    ``CU2609``, ``SC2612``) is unique across the exchanges, so a bare
    contract code resolves without guessing the exchange.

    Args:
        active: Client used for the lookup when needed.
        symbol: ``IF2610``-style contract code, or an already-qualified
            ``IF2610.CFE`` / ``CU2609.SHF`` code.

    Returns:
        The qualified code.

    Raises:
        ThsProviderError: The symbol is blank, or the futures catalog does
            not contain exactly one match.
    """
    candidate = _candidate_symbol(symbol)
    if "." in candidate:
        return candidate
    return _resolve_by_listing(
        active,
        candidate,
        asset_type=FUTURES_ASSET_TYPE,
        unresolved_code="THS_FUTURES_SYMBOL_UNRESOLVED",
    )


def resolve_fund_code(active: FuyaoHttpClient, symbol: str) -> str:
    """Resolve an on-exchange fund symbol to the upstream ``thscode`` form.

    Not :func:`resolve_code`: its suffix filter is the stock one, and a plain
    six-digit code is shared across asset classes (``000001`` is a stock, an
    index and an OTC fund), so a search hit could let a stock answer for a
    fund. The ETF listing is the bounded universe instead - 1,696 rows in one
    call, bare codes unique across the two exchanges (measured 2026-09-25) -
    so resolution only ever lands on a listed ETF.

    Args:
        active: Client used for the lookup when needed.
        symbol: ``510300``-style code, or an already-qualified
            ``510300.SH`` code.

    Returns:
        The qualified code.

    Raises:
        ThsProviderError: The symbol is blank, or the ETF listing does not
            contain exactly one match - which includes OTC fund codes, as
            those are not in this universe and must be passed qualified.
    """
    candidate = _candidate_symbol(symbol)
    if "." in candidate:
        return candidate
    return _resolve_by_listing(
        active,
        candidate,
        asset_type=FUND_ASSET_TYPE,
        unresolved_code="THS_FUND_SYMBOL_UNRESOLVED",
    )


def resolve_option_code(active: FuyaoHttpClient, symbol: str) -> str:
    """Validate a qualified option ``thscode`` (no bare-code lookup).

    The option catalog holds well over the 10k per-call listing cap and
    its ``thscode`` is an opaque number (``90007464.SZ``) while the
    readable contract code lives in ``ticker``
    (``159922P2612M003800``), so a bare code cannot be resolved cheaply or
    unambiguously. Callers pass the qualified contract code.

    Args:
        active: Client, unused - kept for signature parity with the other
            resolvers.
        symbol: ``IO2601-C-4000.CFE`` / ``90007464.SZ`` style code.

    Returns:
        The qualified code.

    Raises:
        ThsProviderError: The symbol is blank or carries no exchange
            suffix.
    """
    del active
    candidate = _candidate_symbol(symbol)
    if "." not in candidate:
        raise ThsProviderError("THS_OPTION_SYMBOL_QUALIFIED_REQUIRED")
    return candidate


__all__ = [
    "ThsProviderError",
    "client",
    "credentials",
    "resolve_code",
    "resolve_fund_code",
    "resolve_futures_code",
    "resolve_index_code",
    "resolve_option_code",
]
