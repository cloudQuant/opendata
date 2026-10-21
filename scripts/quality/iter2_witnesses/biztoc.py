"""Offline witness configuration for the ``biztoc`` world-news row.

The RapidAPI feed answers a JSON list of articles; the key is a header credential read from
``BIZTOC_API_KEY`` and the fixture supplies a placeholder so nothing reaches the network. An
article without its ``title`` fails the frozen contract, which is the counterfact leg.

Witnesses run exclusively through the offline ``http_transport`` seam served by
``FixedResponseTransport``. No leg touches a socket.
"""

from __future__ import annotations

from typing import Any, Final


def _article(index: int, *, title: str | None = None) -> dict[str, Any]:
    """One article in the spellings the Biztoc feed publishes."""
    return {
        "id": 1000 + index,
        "uid": f"fixture-{index}",
        "title": title if title is not None else f"Fixture world-news headline {index}",
        "published": f"2026-10-20T{12 + index:02d}:00:00Z",
        "body": f"Fixture article body {index}.",
        "body_preview": f"Fixture preview {index}.",
        "excerpt": f"Fixture excerpt {index}.",
        "author": "Fixture Author",
        "url": f"https://example.invalid/news/{index}",
        "img": [{"url": f"https://example.invalid/image/{index}.jpg", "width": 640, "height": 360}],
        "tags": ["fixture", "world"],
        "score": 0.5 + index,
        "site": "fixture",
        "domain": "example.invalid",
    }


def _biztoc_document() -> list[dict[str, Any]]:
    """A two-article answer from the latest feed."""
    return [_article(0), _article(1)]


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.biztoc.registration",
    "credential_env": {"BIZTOC_API_KEY": "fixture-key-never-sent"},
    "allowed_hosts": ["biztoc.p.rapidapi.com"],
    "models": {
        "WorldNews": {
            "fixture_document": _biztoc_document,
            "query": {},
            "tamper": {"op": "drop", "path": [0, "title"], "column": "title"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "BIZTOC_SHAPE_INVALID",
            "tamper_reason": (
                "publishing an article without its 'title' leaves the required 'title' contract "
                "field unresolvable, so the adapter must refuse with BIZTOC_SHAPE_INVALID rather "
                "than shipping an article the caller cannot identify"
            ),
            "source_files": [
                "opendata/data/providers/biztoc/models/world_news.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/biztoc.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
