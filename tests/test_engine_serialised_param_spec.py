"""The engine kernel ``request.serialised_param``: a declared wire form for one query parameter.

Before this facet a parameter reached the wire only in the rendering its *kind* gave it -- ISO for
a date, a comma join for a safe list, ``str(text)`` guarded by ``_SAFE_VALUE`` otherwise -- so the
four census rows whose need is "send the query parameter in this exact serialised spelling" had no
declaration at all: wsj's ETF gainers/losers/active models put a URL-encoded JSON document in the
``id=`` parameter (gainers.py:88), and TreasuryAuctions builds ``%m/%d/%Y`` date strings into
``startDate``/``endDate`` on the request side (treasury_auctions.py:209, :212). One mechanism
serves both: :attr:`ParamSpec.serialise` names the form (``"json"`` or
``"strftime:<format>"``), ``_encode_value`` takes that branch first, and the empty default leaves
every undeclared parameter's rendering byte-for-byte unchanged.

Every case here is offline: a transport from ``_engine.testing`` serves a canned document and an
autouse guard refuses to run the real HTTP read function at all, so "no network" is a property of
the run rather than of the fixtures.

The judgement probe (:func:`_wire_face`) returns one thing: the exact ``dict[str, str]`` the
engine puts on the request for a declared pair (parameter, value). It is judged on *values*, not
on "it ran": the JSON arm asserts the byte-exact compact blob, the date arm the byte-exact
``10/01/2026``, and the undeclared arm the byte-exact renderings the tree shipped before the
field existed.

The counterfact runs both arms in :class:`TestCounterfact`:

* the break arm edits exactly one thing and must turn the probe red with a *pre-named* reading --
  dropping the declaration from the wsj model must raise
  ``PROBE_WSJ_GAINERS_PARAM_UNENCODABLE`` (a list of dicts cannot pass ``_SAFE_VALUE``), dropping
  the date declaration must put ISO ``2026-10-01`` on the wire where ``10/01/2026`` is judged, and
  replacing the engine's ``_serialise_value`` with ``str(value)`` must put the Python repr
  ``[{'type': 'etf', ...}]`` -- single quotes, invalid JSON -- on the wire;
* the clean arm re-runs the identical probe on the untouched tree and must be green, which is
  what makes the break readings attributable to the break and not to the probe.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    encode_query,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context

PROBE_SOURCE = "probe"

# The recorded wsj-style document: the rows live under ``data.instruments``, and the request the
# upstream model builds carries the whole sort/limit specification as one JSON blob under ``id``.
WSJ_DOCUMENT = {
    "data": {
        "instruments": [
            {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
            {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
            {"symbol": "NVDA-CA", "name": "Nvidia Corp"},
        ]
    }
}

WSJ_ID_VALUE: list[dict[str, Any]] = [
    {
        "type": "etf",
        "group": "markets_etf/gainers",
        "sortDescription": "Change% up in last 12 months",
        "sortField": "pc_12m",
        "sortType": "desc",
        "excludeDLN": True,
        "numberOfRow": 25,
    }
]

#: The byte-exact blob the probe must find on the wire: compact separators, keys in declaration
#: order, ascii-safe. An extra space or a reordered key makes this string differ, which is the
#: point of judging values rather than counting requests.
WSJ_ID_BLOB = (
    '[{"type":"etf","group":"markets_etf/gainers",'
    '"sortDescription":"Change% up in last 12 months","sortField":"pc_12m",'
    '"sortType":"desc","excludeDLN":true,"numberOfRow":25}]'
)

AUCTION_START = date(2026, 10, 1)
AUCTION_END = date(2026, 10, 2)


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove offline-ness structurally: the real HTTP read path raises the moment it is reached."""

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a serialised-param case reached the network")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def _spec(
    model: str,
    params: tuple[ParamSpec, ...],
    *,
    rows_pointer: str | None = None,
) -> ModelSpec:
    """A throwaway declaration whose only interesting part is its parameters' wire forms."""
    return ModelSpec(
        model=model,
        domain=f"probe_{model.casefold()}",
        asset_class="etf",
        period="snapshot",
        market="all",
        base_url="https://probe.test",
        path="/markets",
        columns=(
            ColumnSpec("symbol", "str", required=True),
            ColumnSpec("name", "str"),
        ),
        params=params,
        rows_pointer=rows_pointer,
        scenario="离线判定面：查询参数的声明式序列化形态",
        error_prefix=f"PROBE_{model}".upper(),
    )


WSJ_SPEC = _spec(
    "WSJGainers",
    (ParamSpec("id", "str", required=True, serialise="json"),),
    rows_pointer="data.instruments",
)
AUCTIONS_SPEC = _spec(
    "TreasuryAuctionsProbe",
    (
        ParamSpec("start_date", "date", query_key="startDate", serialise="strftime:%m/%d/%Y"),
        ParamSpec("end_date", "date", query_key="endDate", serialise="strftime:%m/%d/%Y"),
    ),
)
UNDECLARED_SPEC = _spec(
    "Undeclared",
    (
        ParamSpec("issued", "date"),
        ParamSpec("tickers", "str_list"),
        ParamSpec("flag", "bool"),
    ),
)


def _wire_face(spec: ModelSpec, **query: object) -> dict[str, str]:
    """The judgement probe: exactly the query string this declaration sends for this request."""
    return encode_query(SimpleNamespace(**query), spec, now=date(2026, 10, 11))


class TestJudgementProbe:
    """The declared form arrives byte-exactly; the undeclared face does not move a byte."""

    def test_json_param_is_the_exact_blob_on_the_wire(self) -> None:
        assert _wire_face(WSJ_SPEC, id=WSJ_ID_VALUE) == {"id": WSJ_ID_BLOB}

    def test_strftime_dates_are_the_exact_spellings(self) -> None:
        assert _wire_face(AUCTIONS_SPEC, start_date=AUCTION_START, end_date=AUCTION_END) == {
            "startDate": "10/01/2026",
            "endDate": "10/02/2026",
        }

    def test_omitted_serialised_param_is_not_sent(self) -> None:
        assert _wire_face(AUCTIONS_SPEC, start_date=AUCTION_START) == {"startDate": "10/01/2026"}

    def test_undeclared_renderings_are_the_pre_kernel_bytes(self) -> None:
        """The zero-regression face: default ``serialise=""`` keeps every old rendering."""
        assert _wire_face(
            UNDECLARED_SPEC,
            issued=date(2026, 10, 1),
            tickers=["AAPL", "MSFT"],
            flag=True,
        ) == {"issued": "2026-10-01", "tickers": "AAPL,MSFT", "flag": "true"}

    def test_fetch_through_recorded_fixture_sends_the_blob(self) -> None:
        """End to end offline: the request the transport records carries the serialised form."""
        transport = FixedResponseTransport(HttpResponse(200, WSJ_DOCUMENT))
        generated = make_http_json_fetcher(PROBE_SOURCE, WSJ_SPEC)
        fetcher = type(generated.__name__, (generated,), {"http_transport": transport})()
        rows = list(
            fetcher.fetch(ctx=fixture_context(PROBE_SOURCE, WSJ_SPEC, sends=2), id=WSJ_ID_VALUE)
        )
        assert [row.symbol for row in rows] == ["QQQ", "SPY", "NVDA-CA"]
        assert transport.calls[0]["params"] == {"id": WSJ_ID_BLOB}


class TestConstructionRefusals:
    """A wire form that could not mean what it says is refused when the record is built."""


def test_json_refused_on_a_kind_with_no_structure() -> None:
    with pytest.raises(ValueError, match="may serialise as json only when"):
        ParamSpec("issued", "date", serialise="json")


def test_unknown_spelling_refused() -> None:
    with pytest.raises(ValueError, match="unknown serialiser 'base64'"):
        ParamSpec("id", "str", serialise="base64")


def test_empty_strftime_format_refused() -> None:
    with pytest.raises(ValueError, match="strftime with an empty format"):
        ParamSpec("issued", "date", serialise="strftime:")


def test_strftime_refused_on_a_non_date_kind() -> None:
    with pytest.raises(ValueError, match="only a date has a format to render"):
        ParamSpec("id", "str", serialise="strftime:%Y-%m")


def test_serialiser_paired_with_a_value_map_refused() -> None:
    with pytest.raises(ValueError, match="translated twice on its way out"):
        ParamSpec("id", "str", serialise="json", value_map=(("a", "b"),))


def test_unrenderable_blob_refused_at_the_wire() -> None:
    """A json parameter whose value is not JSON gets the named refusal, never ``str(value)``."""
    with pytest.raises(ProviderEngineError) as refusal:
        _wire_face(WSJ_SPEC, id=[object()])
    assert "PROBE_WSJGAINERS_PARAM_UNENCODABLE" in str(refusal.value)


def test_strftime_on_a_value_without_a_format_refused_at_the_wire() -> None:
    with pytest.raises(ProviderEngineError) as refusal:
        _wire_face(AUCTIONS_SPEC, start_date="10/01/2026")
    assert "PROBE_TREASURYAUCTIONSPROBE_PARAM_UNENCODABLE" in str(refusal.value)


class TestCounterfact:
    """Both arms: the probe only means something if a named break flips the reading red."""

    def test_clean_arm_the_probe_is_green(self) -> None:
        """The same probe the break arm runs, on the untouched tree, judges these exact bytes."""
        assert _wire_face(WSJ_SPEC, id=WSJ_ID_VALUE)["id"] == WSJ_ID_BLOB
        assert _wire_face(AUCTIONS_SPEC, start_date=AUCTION_START)["startDate"] == "10/01/2026"

    def test_break_declaration_off_the_wsj_param_reads_unencodable(self) -> None:
        """One edit on the declaration side: drop ``serialise`` and the judgement goes red with
        the pre-named reading ``PROBE_WSJGAINERS_PARAM_UNENCODABLE`` -- a list of dicts cannot
        pass ``_SAFE_VALUE``, which is exactly the absence the census row recorded."""
        broken = replace(WSJ_SPEC, params=(replace(WSJ_SPEC.params[0], serialise=""),))
        with pytest.raises(ProviderEngineError) as refusal:
            _wire_face(broken, id=WSJ_ID_VALUE)
        assert "PROBE_WSJGAINERS_PARAM_UNENCODABLE" in str(refusal.value)

    def test_break_declaration_off_the_date_param_reads_iso(self) -> None:
        """Dropping the date declaration sends ISO where ``10/01/2026`` is judged: the named
        reading is the ISO byte string the kernel exists to replace."""
        broken = replace(
            AUCTIONS_SPEC,
            params=(replace(AUCTIONS_SPEC.params[0], serialise=""), AUCTIONS_SPEC.params[1]),
        )
        face = _wire_face(broken, start_date=AUCTION_START)
        assert face == {"startDate": "2026-10-01"}  # the pre-named break reading, red verbatim
        assert face["startDate"] != "10/01/2026"

    def test_break_engine_renderer_reads_python_repr(self) -> None:
        """One engine-side edit: route the blob through ``str(value)`` instead of the serialiser
        and the probe judges the Python repr -- single-quoted, not JSON. Named reading:
        ``str(WSJ_ID_VALUE)``, which differs from ``WSJ_ID_BLOB`` at the first quote."""
        monkeypatched = lambda parameter, value, spec: str(value)  # noqa: E731
        original = http_json._serialise_value
        http_json._serialise_value = monkeypatched
        try:
            face = _wire_face(WSJ_SPEC, id=WSJ_ID_VALUE)
        finally:
            http_json._serialise_value = original
        assert face == {"id": str(WSJ_ID_VALUE)}
        assert face["id"] != WSJ_ID_BLOB
