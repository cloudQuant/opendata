"""Self-developed provider registrations (design §1.4, license boundary).

Fetchers wrapping the ported ``opendata_http`` tree are self-developed
code and therefore live here, never inside ``opendata_http/`` itself
(design "boundary is verifiable": the ported subtree stays a pristine
MIT island). Overseas providers follow the same shape in their own
packages once they land.
"""

from opendata.data.capability import Capability


def register_providers() -> list[Capability]:
    """Register every bundled provider into the process registry.

    Idempotent: capabilities already present are skipped, so repeated
    startup calls (tests, multi-entry lifespan) never raise.

    Returns:
        The newly registered capabilities (empty when all were
        already present).
    """
    # Lazy import: registration must not pull the ported tree (or any
    # heavy module) into processes that never fetch (design §7.1).
    from opendata.data.providers.akshare import register as register_akshare
    from opendata.data.providers.ths import register as register_ths

    return [*register_akshare(), *register_ths()]
