"""IMF SDMX 3.0 transport and structural metadata helpers."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from opendata.data.http_client import HttpFetchError, get_shared_http_client

IMF_SDMX_BASE_URL = "https://api.imf.org/external/sdmx/3.0"


class ImfSdmxError(RuntimeError):
    """Stable IMF SDMX API errors with the failing upstream status."""

    def __init__(self, code: str, *, status: int | None = None, url: str | None = None) -> None:
        self.code = code
        self.status = status
        self.url = url
        detail = "" if status is None else f" status={status}"
        detail += "" if url is None else f" url={url}"
        super().__init__(code + detail)


def _get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=timeout if timeout is not None else 30.0,
            source="imf",
        )
    except HttpFetchError as exc:
        raise ImfSdmxError("IMF_HTTP_ERROR", status=exc.status, url=exc.url) from exc
    return response.status_code, response.text


def _check_status(status: int, url: str) -> None:
    if status != 200:
        raise ImfSdmxError("IMF_HTTP_ERROR", status=status, url=url)


def fetch_dataflows(*, timeout: float | None) -> dict[str, Any]:
    """Fetch the public IMF SDMX dataflow catalogue (metadata only)."""
    url = f"{IMF_SDMX_BASE_URL}/structure/dataflow/all/all"
    params = {"format": "sdmx-3.0"}
    status, text = _get(url, params, timeout)
    _check_status(status, url)
    try:
        document = json.loads(text)
        data = document["data"]
        flows = data["dataflows"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImfSdmxError("IMF_BAD_RESPONSE", status=status, url=url) from exc
    if not isinstance(data, dict) or not isinstance(flows, list):
        raise ImfSdmxError("IMF_BAD_RESPONSE", status=status, url=url)
    return data


def fetch_dataflow_structure(
    agency: str, dataflow: str, version: str, *, timeout: float | None
) -> dict[str, Any]:
    """Fetch one IMF flow with its DSD and code lists."""
    url = f"{IMF_SDMX_BASE_URL}/structure/dataflow/{agency}/{dataflow}/{version}"
    params = {"format": "sdmx-3.0", "references": "all"}
    status, text = _get(url, params, timeout)
    _check_status(status, url)
    try:
        document = json.loads(text)
        data = document["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImfSdmxError("IMF_BAD_RESPONSE", status=status, url=url) from exc
    if not isinstance(data, dict):
        raise ImfSdmxError("IMF_BAD_RESPONSE", status=status, url=url)
    return data


def fetch_observations(
    agency: str,
    dataflow: str,
    version: str,
    key: str,
    *,
    start: str | None,
    end: str | None,
    timeout: float | None,
) -> list[dict[str, str | None]]:
    """Fetch and shape-check CSV observations for one SDMX key."""
    url = f"{IMF_SDMX_BASE_URL}/data/dataflow/{agency}/{dataflow}/{version}/{key}"
    params = {"format": "csvfile"}
    if start is not None:
        params["startPeriod"] = start
    if end is not None:
        params["endPeriod"] = end
    status, text = _get(url, params, timeout)
    _check_status(status, url)
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"TIME_PERIOD", "OBS_VALUE"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ImfSdmxError("IMF_BAD_RESPONSE", status=status, url=url)
    return list(reader)


def flow_records(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    flows = document.get("dataflows")
    if not isinstance(flows, list):
        raise ImfSdmxError("IMF_BAD_RESPONSE")
    return [flow for flow in flows if isinstance(flow, dict)]


def data_dimensions(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    structures = document.get("dataStructures")
    if not isinstance(structures, list) or len(structures) != 1:
        raise ImfSdmxError("IMF_BAD_RESPONSE")
    components = structures[0].get("dataStructureComponents")
    dimension_list = components.get("dimensionList") if isinstance(components, dict) else None
    dimensions = dimension_list.get("dimensions") if isinstance(dimension_list, dict) else None
    if not isinstance(dimensions, list) or not dimensions:
        raise ImfSdmxError("IMF_BAD_RESPONSE")
    result = [item for item in dimensions if isinstance(item, dict)]
    result.sort(key=lambda item: int(item.get("position", -1)))
    if len(result) != len(dimensions) or any(item.get("position") is None for item in result):
        raise ImfSdmxError("IMF_BAD_RESPONSE")
    return result


def codelist(document: Mapping[str, Any], list_id: str) -> list[dict[str, Any]]:
    lists = document.get("codelists")
    if not isinstance(lists, list):
        return []
    for item in lists:
        if not isinstance(item, dict) or item.get("id") != list_id:
            continue
        codes = item.get("codes")
        return [code for code in codes if isinstance(code, dict)] if isinstance(codes, list) else []
    return []


def code_labels(document: Mapping[str, Any], list_id: str) -> dict[str, str]:
    """Return English code labels, rejecting duplicate codes as bad metadata."""
    result: dict[str, str] = {}
    for item in codelist(document, list_id):
        code = item.get("id")
        name = item.get("names")
        label = name.get("en") if isinstance(name, dict) else item.get("name")
        if isinstance(code, str) and isinstance(label, str) and label.strip():
            if code in result:
                raise ImfSdmxError("IMF_BAD_RESPONSE")
            result[code] = label.strip()
    return result


def dimension_code_list_id(dimension: Mapping[str, Any]) -> str | None:
    """Infer the IMF code-list identifier from a dimension's concept scheme."""
    identity = dimension.get("conceptIdentity")
    if not isinstance(identity, str):
        return None
    marker = "."
    if marker not in identity:
        return None
    scheme = identity.rsplit(marker, maxsplit=1)[0].split(":")[-1].split("(", maxsplit=1)[0]
    dimension_id = identity.rsplit(marker, maxsplit=1)[-1]
    if scheme.startswith("CS_") and scheme != "CS_MASTER_SYSTEM":
        return "CL_" + scheme[3:] + "_" + dimension_id
    if dimension_id in {"FREQ", "FREQUENCY"}:
        return "CL_FREQ"
    if dimension_id in {"COUNTRY", "REF_AREA"}:
        return "CL_COUNTRY"
    if dimension_id == "INDICATOR":
        return "CL_INDICATOR"
    return None


def dimension_labels(document: Mapping[str, Any], dimension: Mapping[str, Any]) -> dict[str, str]:
    """Resolve a dimension's code labels from the flow's referenced lists."""
    list_id = dimension_code_list_id(dimension)
    if list_id is None:
        return {}
    exact = code_labels(document, list_id)
    if exact:
        return exact
    # IMF's master-scheme lists are shared and do not repeat the dimension ID.
    dimension_id = dimension.get("id")
    fallback = {
        "FREQ": "CL_FREQ",
        "FREQUENCY": "CL_FREQ",
        "COUNTRY": "CL_COUNTRY",
        "REF_AREA": "CL_COUNTRY",
        "INDICATOR": "CL_INDICATOR",
        "INDEX_TYPE": "CL_INDEX_TYPE",
        "COICOP_1999": "CL_COICOP_1999",
        "TYPE_OF_TRANSFORMATION": "CL_CPI_TYPE_OF_TRANSFORMATION",
        "COUNTERPART_COUNTRY": "CL_IMTS_COUNTRY",
    }.get(str(dimension_id))
    return code_labels(document, fallback) if fallback else {}


def parse_period(value: object) -> date:
    """Convert annual, quarterly, monthly, or ISO SDMX periods to dates."""
    text = str(value).strip()
    if len(text) == 4 and text.isdigit():
        return date(int(text), 1, 1)
    if len(text) == 7 and text[4] == "Q" and text[5:].isdigit():
        quarter = int(text[5:])
        if quarter not in {1, 2, 3, 4}:
            raise ValueError(text)
        return date(int(text[:4]), 1 + (quarter - 1) * 3, 1)
    if len(text) == 7 and text[4] == "-":
        return date.fromisoformat(text + "-01")
    return date.fromisoformat(text)


__all__ = [
    "IMF_SDMX_BASE_URL",
    "ImfSdmxError",
    "code_labels",
    "data_dimensions",
    "dimension_code_list_id",
    "fetch_dataflow_structure",
    "fetch_dataflows",
    "fetch_observations",
    "flow_records",
    "parse_period",
]
