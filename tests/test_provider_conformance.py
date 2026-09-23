"""Provider conformance tests (C1 provider 模板, design §7.1).

Every registered fetcher - whichever package it lives in - must keep the
provider contract: a well-formed capability whose source label equals its
package directory, a registered domain, query validation that fails closed
on unknown fields, and a place in the OpenBB compatibility map (AC-10
admission rule). Parametrized over the registry, so each new provider
inherits the whole contract automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.domains import require_domain
from opendata.data.openbb_map import covered_capabilities
from opendata.data.providers import register_providers
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher

# Parametrization is evaluated at collection time, so the registry must be
# populated before the fetcher list is built (idempotent; process singleton).
register_providers()


def _fetchers() -> list[Fetcher]:  # type: ignore[type-arg]
    registry = get_registry()
    return [
        registry.resolve_domain(capability.domain, source=capability.source)
        for capability in registry.capabilities()
    ]


def _package_source(fetcher: Fetcher) -> str:
    """Return the provider package directory the fetcher module lives in."""
    parts = fetcher.__module__.split(".")
    if "providers" in parts:
        return parts[parts.index("providers") + 1]
    pytest.fail(f"fetcher {fetcher.__module__} does not live under a provider package")


@pytest.mark.parametrize("fetcher", _fetchers(), ids=lambda f: f.capability.source)
class TestProviderConformance:
    def test_capability_is_well_formed(self, fetcher: Fetcher):
        capability: Capability = fetcher.capability

        assert capability.asset_class
        assert capability.period
        assert capability.market
        assert isinstance(capability.verified, bool)
        require_domain(capability.domain)  # 注册域，未注册会抛 LookupError

    def test_source_label_equals_the_package_directory(self, fetcher: Fetcher):
        assert fetcher.capability.source == _package_source(fetcher)

    def test_query_validation_fails_closed_on_unknown_fields(
        self,
        fetcher: Fetcher,  # type: ignore[type-arg]
    ):
        with pytest.raises(ValidationError):
            fetcher.transform_query(**{"definitely_not_a_field": 1})

    def test_appears_in_the_openbb_map(self, fetcher: Fetcher):
        capability = fetcher.capability

        assert (capability.source, capability.domain) in covered_capabilities()
