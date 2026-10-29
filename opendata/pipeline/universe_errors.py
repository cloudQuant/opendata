"""Safe domain exceptions raised while selecting warehouse symbol universes."""


class UniverseLimitExceededError(ValueError):
    """Raised when a warehouse-derived universe exceeds its configured cap."""


class EmptySymbolUniverseError(ValueError):
    """Raised when a derived universe has no eligible symbols."""


class UniverseQueryError(RuntimeError):
    """Raised when a warehouse symbol-universe query fails safely."""
