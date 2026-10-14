"""Runtime annotation resolution regressions for Intrinio financial fetchers."""

from __future__ import annotations

import sys
from typing import ClassVar, get_type_hints

from opendata.data.models.base import ContractModel
from opendata.data.providers.intrinio.models.iter2_financials import _PagedEstimateFetcher


def test_paged_estimate_output_model_annotation_resolves_at_runtime() -> None:
    """Keep the contract type available to runtime reflection."""
    module_globals = vars(sys.modules[_PagedEstimateFetcher.__module__])
    hints = get_type_hints(
        _PagedEstimateFetcher,
        globalns=module_globals,
        include_extras=True,
    )

    assert hints["output_model"] == ClassVar[type[ContractModel]]
