"""Interface loader service (FR-17, milestone B5.1).

Discovers the data capabilities from the provider registry and loads
them into the ``data_interfaces`` catalog that drives the frontend
"数据接口" page. The legacy reflection scan of the ported package was
removed with the compatibility switch (B5.1): the catalog is now, by
construction, the same capability list the routing layer serves - one
source of truth instead of a second, drifting inventory.

Interfaces are named by their domain identifier, which is exactly what
the acquisition seam matches against when it routes through
``ProviderRegistry.resolve_domain`` (A1.7); display names and contract
models come from the domain registry (``domains.yaml``). Capabilities
of the same domain with different sources fold into one row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select

from opendata.core.database import async_session_maker
from opendata.data.domains import contract_model, display_name, require_domain
from opendata.models.interface import DataInterface, InterfaceCategory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from opendata.data.capability import Capability


class InterfaceLoader:
    """Service for loading the provider catalog into the interface table."""

    async def load_interfaces(self) -> int:
        """Load the catalog from provider-registry capabilities (FR-17).

        Returns:
            Number of interfaces loaded (0 when no capability is
            registered yet - the registry is the single source, so an
            empty registry is an empty catalog, not a fallback to an
            older scan).

        Raises:
            LookupError: A capability references a domain that the
                domain registry does not know (fail closed).
        """
        from opendata.data.registry import get_registry

        capabilities = get_registry().capabilities()
        if not capabilities:
            logger.warning("no provider capabilities registered yet; catalog stays empty")
            return 0
        async with async_session_maker() as db:
            count = 0
            for capability in capabilities:
                if await self._load_capability_interface(capability, db):
                    count += 1
            await db.commit()
        logger.info(f"registry scan found {len(capabilities)} capabilities")
        return count

    async def _load_capability_interface(self, capability: Capability, db: AsyncSession) -> bool:
        """Create the interface row for one capability, idempotently.

        Args:
            capability: The registered capability.
            db: Session for the catalog write.

        Returns:
            True when a new row was created.
        """
        result = await db.execute(
            select(DataInterface).where(DataInterface.name == capability.domain)
        )
        if result.scalar_one_or_none() is not None:
            return False  # Already loaded

        require_domain(capability.domain)  # fail closed on unregistered domains
        category = await self._ensure_category(capability.asset_class, db)
        contract_name = contract_model(capability.domain).__name__
        interface = DataInterface(
            name=capability.domain,
            display_name=display_name(capability.domain),
            description=(
                f"{capability.source} capability for {capability.domain} "
                f"({contract_name}); "
                f"verified={capability.verified}"
            ),
            category_id=category.id,
            module_path="opendata.data.providers",
            function_name=capability.domain,
            parameters={},
            return_type=contract_name,
            is_active=True,
        )
        db.add(interface)
        return True

    async def _ensure_category(self, name: str, db: AsyncSession) -> InterfaceCategory:
        """Find or create an interface category by name.

        Args:
            name: Category name (the capability's asset class).
            db: Session for the lookup and creation.

        Returns:
            The existing or freshly created category.
        """
        result = await db.execute(select(InterfaceCategory).where(InterfaceCategory.name == name))
        category = result.scalar_one_or_none()
        if category is not None:
            return category
        category = InterfaceCategory(
            name=name, description=f"{name} data domains (registry mode)", sort_order=10
        )
        db.add(category)
        await db.flush()
        return category


# Singleton instance
interface_loader = InterfaceLoader()
