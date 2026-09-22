"""Interface loader service.

Discovers and loads akshare data interface definitions.
"""

import inspect
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import opendata_http as ak
from opendata.core.database import async_session_maker
from opendata.data.capability import Capability
from opendata.data.domains import contract_model, display_name, require_domain
from opendata.models.interface import (
    DataInterface,
    InterfaceCategory,
    InterfaceParameter,
    ParameterType,
)


class InterfaceLoader:
    """Service for loading akshare interfaces into the database.

    Discovers akshare functions and creates interface definitions
    with metadata for use in the web interface.
    """

    # Category mapping for akshare functions
    CATEGORY_MAPPING = {
        # Stock
        "stock_zh_a_hist": "stock",
        "stock_zh_a_spot": "stock",
        "stock_info_a_code_name": "stock",
        "stock_individual_info_em": "stock",
        # Fund
        "fund_open_fund_info_em": "fund",
        "fund_etf_spot_em": "fund",
        # Futures
        "futures_sina_main_sina": "futures",
        # Macro/Economic
        "macro_china_gdp": "macro",
        "economic_cicc_gold_spot": "economic",
    }

    async def load_interfaces(self) -> int:
        """Load the interface catalog honouring the scan-source switch (A1.7).

        Returns:
            Number of interfaces loaded.

        Raises:
            ValueError: On an unknown scan-source value (fail closed).
        """
        from opendata.core.config import settings

        source = settings.interface_scan_source
        if source == "legacy":
            return await self.load_from_akshare()
        if source == "registry":
            return await self.load_from_registry()
        raise ValueError(f"unknown interface_scan_source {source!r} (legacy | registry)")

    async def load_from_registry(self) -> int:
        """Load the catalog from provider-registry capabilities (FR-17).

        Registry-mode interfaces are named by their domain
        identifier so the acquisition seam routes their fetches
        through ``ProviderRegistry.resolve_domain`` (A1.7);
        display names and contract models come from the domain
        registry (domains.yaml). Capabilities of the same domain
        with different sources fold into one row.

        Returns:
            Number of interfaces loaded.
        """
        from opendata.data.registry import get_registry

        capabilities = get_registry().capabilities()
        if not capabilities:
            logger.warning(
                "interface_scan_source=registry but no capabilities registered yet "
                "(A2.4 pending); nothing to load"
            )
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
            name: Category name (registry mode: the asset class).
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

    async def load_from_akshare(self) -> int:
        """Load all akshare interfaces into the database (legacy scan).

        Returns:
            Number of interfaces loaded.
        """
        async with async_session_maker() as db:
            # Ensure categories exist
            await self._ensure_categories(db)

            # Get all akshare functions
            functions = self._discover_akshare_functions()

            # Load interfaces
            count = 0
            for func_name, func in functions:
                if await self._load_function_safe(func_name, func, db):
                    count += 1

            await db.commit()
            logger.info(f"Loaded {count} interfaces from akshare")
            return count

    async def _load_function_safe(
        self,
        func_name: str,
        func: Any,  # noqa: ANN401
        db: AsyncSession,
    ) -> bool:
        """Load one interface, reporting failures instead of raising."""
        try:
            await self._load_interface(func_name, func, db)
        except Exception as e:
            logger.warning(f"Failed to load interface {func_name}: {e}")
            return False
        return True

    async def _ensure_categories(self, db: AsyncSession) -> None:
        """Ensure all interface categories exist."""
        categories = [
            InterfaceCategory(name="stock", description="股票数据", sort_order=1),
            InterfaceCategory(name="fund", description="基金数据", sort_order=2),
            InterfaceCategory(name="futures", description="期货数据", sort_order=3),
            InterfaceCategory(name="index", description="指数数据", sort_order=4),
            InterfaceCategory(name="bond", description="债券数据", sort_order=5),
            InterfaceCategory(name="forex", description="外汇数据", sort_order=6),
            InterfaceCategory(name="economic", description="经济数据", sort_order=7),
            InterfaceCategory(name="macro", description="宏观数据", sort_order=8),
        ]

        for category in categories:
            # Check if exists
            result = await db.execute(
                select(InterfaceCategory).where(InterfaceCategory.name == category.name)
            )
            if result.scalar_one_or_none() is None:
                db.add(category)

        await db.commit()

    def _discover_akshare_functions(self) -> list[tuple[str, Any]]:
        """Discover all public functions in akshare module.

        Returns:
            List of (function_name, function) tuples
        """
        functions = []

        # Get all public attributes
        for name in dir(ak):
            if not name.startswith("_"):
                attr = getattr(ak, name)
                if callable(attr):
                    functions.append((name, attr))

        logger.info(f"Discovered {len(functions)} functions in akshare")
        return functions

    async def _load_interface(
        self,
        func_name: str,
        func: Any,  # noqa: ANN401
        db: AsyncSession,
    ) -> None:
        """Load a single interface definition."""
        # Check if already exists
        result = await db.execute(select(DataInterface).where(DataInterface.name == func_name))
        if result.scalar_one_or_none() is not None:
            return  # Already loaded

        # Get function signature
        try:
            sig = inspect.signature(func)
        except ValueError:
            sig = None

        # Get docstring
        docstring = inspect.getdoc(func) or ""

        # Determine category
        category_name = self._get_category(func_name)

        # Get category
        result = await db.execute(
            select(InterfaceCategory).where(InterfaceCategory.name == category_name)
        )
        category = result.scalar_one_or_none()

        if category is None:
            category_name = "stock"  # Default
            result = await db.execute(
                select(InterfaceCategory).where(InterfaceCategory.name == category_name)
            )
            category = result.scalar_one_or_none()

        # Create interface
        interface = DataInterface(
            name=func_name,
            display_name=self._generate_display_name(func_name),
            description=self._parse_description(docstring),
            category_id=category.id if category else 1,
            module_path="opendata_http",
            function_name=func_name,
            parameters=self._parse_parameters(sig, docstring) if sig else {},
            return_type="DataFrame",
            example=self._parse_example(docstring),
            is_active=True,
        )

        db.add(interface)

        # Create parameter definitions
        if sig:
            await self._create_parameter_definitions(interface, sig, db)

    def _get_category(self, func_name: str) -> str:
        """Determine category from function name."""
        # Check explicit mapping
        if func_name in self.CATEGORY_MAPPING:
            return self.CATEGORY_MAPPING[func_name]

        # Infer from name prefix
        for prefix in ["stock", "fund", "futures", "index", "bond", "forex", "economic", "macro"]:
            if func_name.startswith(prefix):
                return prefix

        return "stock"  # Default

    def _generate_display_name(self, func_name: str) -> str:
        """Generate display name from function name."""
        # Replace underscores with spaces and capitalize
        return func_name.replace("_", " ").title()

    def _parse_description(self, docstring: str) -> str:
        """Parse description from docstring."""
        if not docstring:
            return ""

        # Get first line or paragraph
        lines = docstring.strip().split("\n")
        if lines:
            return lines[0].strip()
        return ""

    def _parse_example(self, docstring: str) -> str | None:
        """Parse example usage from docstring."""
        if not docstring or "Example" not in docstring:
            return None

        # Try to extract example section
        if "Example" in docstring:
            parts = docstring.split("Example")
            if len(parts) > 1:
                example = parts[1].strip().split("\n")[0]
                return example[:500]  # Limit length

        return None

    def _parse_parameters(self, sig: inspect.Signature, docstring: str) -> dict:
        """Parse parameters into JSON schema."""
        parameters = {}

        for param_name, param in sig.parameters.items():
            if param_name in ["self", "kwargs"]:
                continue

            param_info = {
                "type": self._get_type_name(param.annotation),
                "required": param.default == inspect.Parameter.empty,
                "default": str(param.default) if param.default != inspect.Parameter.empty else None,
            }
            parameters[param_name] = param_info

        return parameters

    async def _create_parameter_definitions(
        self,
        interface: DataInterface,
        sig: inspect.Signature,
        db: AsyncSession,
    ) -> None:
        """Create parameter definition records."""
        for i, (param_name, param) in enumerate(sig.parameters.items()):
            if param_name in ["self", "kwargs"]:
                continue

            param_def = InterfaceParameter(
                interface_id=interface.id,
                name=param_name,
                display_name=self._generate_display_name(param_name),
                param_type=self._map_parameter_type(param.annotation),
                description=f"Parameter: {param_name}",
                default_value=str(param.default)
                if param.default != inspect.Parameter.empty
                else None,
                required=param.default == inspect.Parameter.empty,
                sort_order=i,
            )
            db.add(param_def)

    def _get_type_name(self, annotation: Any) -> str:  # noqa: ANN401
        """Get type name from annotation."""
        if annotation is inspect.Parameter.empty or annotation is str:
            return "string"
        if annotation is int:
            return "integer"
        if annotation is float:
            return "float"
        if annotation is bool:
            return "boolean"
        return "string"

    def _map_parameter_type(self, annotation: Any) -> ParameterType:  # noqa: ANN401
        """Map Python type to ParameterType enum."""
        if annotation is str or annotation is inspect.Parameter.empty:
            return ParameterType.STRING
        if annotation is int:
            return ParameterType.INTEGER
        if annotation is float:
            return ParameterType.FLOAT
        if annotation is bool:
            return ParameterType.BOOLEAN
        return ParameterType.STRING


# Singleton instance
interface_loader = InterfaceLoader()
