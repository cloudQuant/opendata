"""C85 unit coverage for the disk-backed affected-key spool.

Targets the branches the existing bounded-spool test leaves dark: the scalar
type tags of ``_encode_key``/``_decode_key``, every guard on
:class:`SymbolPartitioning`, the dedupe-conflict path of
``AffectedKeySpool.append``, the slicing ``__getitem__`` protocol of both the
spool and the lazy partition view, the periodic-commit bookkeeping and the
closed-spool errors.

Only the spool's own private SQLite file under ``tmp_path`` is touched - no
warehouse engine, no connection to a shared database, no DDL/DML beyond what
``AffectedKeySpool`` itself issues.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from opendata.pipeline.affected_keys import (
    AffectedKeySpool,
    SymbolPartitioning,
    _decode_key,
    _encode_key,
)

#: One value per supported scalar tag, in the order ``_encode_key`` tests them.
ALL_SCALARS: tuple[object, ...] = (
    None,
    True,
    datetime(2024, 1, 3, 12, 30, 15, tzinfo=timezone.utc),
    date(2024, 1, 2),
    "600519.SH",
    -42,
    1.5,
    Decimal("2.250"),
)


def upper_symbol(value: object) -> str:
    """Normalize by upper-casing, the shape a bounded hook hands over."""
    return str(value).strip().upper()


class TestEncodeKey:
    def test_every_supported_scalar_gets_an_explicit_type_tag(self):
        payload = json.loads(_encode_key(ALL_SCALARS))

        assert payload == [
            {"type": "none"},
            {"type": "bool", "value": True},
            {"type": "datetime", "value": "2024-01-03T12:30:15+00:00"},
            {"type": "date", "value": "2024-01-02"},
            {"type": "str", "value": "600519.SH"},
            {"type": "int", "value": -42},
            {"type": "float", "value": 1.5},
            {"type": "decimal", "value": "2.250"},
        ]

    def test_empty_key_encodes_an_empty_json_list(self):
        assert _encode_key(()) == "[]"

    @pytest.mark.parametrize(
        "value",
        [float("nan"), float("inf"), float("-inf")],
        ids=["nan", "positive-inf", "negative-inf"],
    )
    def test_non_finite_float_is_rejected(self, value):
        with pytest.raises(
            TypeError, match="unsupported affected-key value type: float"
        ) as excinfo:
            _encode_key((value,))

        assert type(excinfo.value) is TypeError

    def test_non_finite_decimal_is_rejected(self):
        with pytest.raises(TypeError, match="unsupported affected-key value type: Decimal"):
            _encode_key((Decimal("Infinity"),))

    @pytest.mark.parametrize(
        ("value", "type_name"),
        [(object(), "object"), (b"raw", "bytes"), (["600519"], "list")],
        ids=["object", "bytes", "list"],
    )
    def test_unsupported_value_type_names_theoffending_class(self, value, type_name):
        with pytest.raises(TypeError, match=f"unsupported affected-key value type: {type_name}"):
            _encode_key((value,))

    def test_finite_decimal_and_bool_keep_their_own_tags(self):
        # ``bool`` is a subclass of ``int``: the tag order has to be checked
        # before the integer arm, otherwise ``True`` would store as an int.
        decoded = json.loads(_encode_key((True, False, Decimal("0.001"))))

        assert decoded[0] == {"type": "bool", "value": True}
        assert decoded[1] == {"type": "bool", "value": False}
        assert decoded[2] == {"type": "decimal", "value": "0.001"}


class TestDecodeKey:
    def test_round_trip_restores_every_scalar_and_its_type(self):
        decoded = _decode_key(_encode_key(ALL_SCALARS))

        assert decoded == ALL_SCALARS
        assert decoded[1] is True
        assert type(decoded[5]) is int
        assert type(decoded[6]) is float
        assert decoded[7] == Decimal("2.250")
        assert decoded[2] == datetime(2024, 1, 3, 12, 30, 15, tzinfo=timezone.utc)

    def test_hand_written_payload_is_decoded_by_tag(self):
        payload = json.dumps(
            [
                {"type": "none"},
                {"type": "bool", "value": 0},
                {"type": "int", "value": 7},
                {"type": "float", "value": "2.5"},
                {"type": "decimal", "value": "10.00"},
            ]
        )

        assert _decode_key(payload) == (None, False, 7, 2.5, Decimal("10.00"))

    def test_date_and_datetime_tags_use_isoformats(self):
        payload = json.dumps(
            [
                {"type": "date", "value": "2024-02-29"},
                {"type": "datetime", "value": "2024-02-29T09:30:00"},
            ]
        )

        assert _decode_key(payload) == (date(2024, 2, 29), datetime(2024, 2, 29, 9, 30))

    def test_empty_payload_decodes_to_empty_tuple(self):
        assert _decode_key("[]") == ()


class TestSymbolPartitioning:
    def test_defaults_to_the_design_batch_ceiling(self):
        bounded = SymbolPartitioning(symbol_key_index=0, normalize_symbol=upper_symbol)

        assert bounded.batch_size == 50

    def test_rejects_a_negative_symbol_index(self):
        with pytest.raises(ValueError, match="symbol_key_index must be nonnegative"):
            SymbolPartitioning(symbol_key_index=-1, normalize_symbol=upper_symbol)

    @pytest.mark.parametrize("batch_size", [0, -1, 51], ids=["zero", "negative", "above-ceiling"])
    def test_rejects_a_batch_size_outside_the_bounded_hook_window(self, batch_size):
        with pytest.raises(
            ValueError, match=r"bounded hook batch_size must be between 1 and 50"
        ) as excinfo:
            SymbolPartitioning(
                symbol_key_index=0,
                normalize_symbol=upper_symbol,
                batch_size=batch_size,
            )

        assert "batch_size" in str(excinfo.value)

    def test_for_key_returns_the_normalized_symbol(self):
        bounded = SymbolPartitioning(symbol_key_index=1, normalize_symbol=upper_symbol)

        assert bounded.for_key(("2024-01-02", " sh600519 ")) == "SH600519"

    def test_for_key_needs_the_configured_symbol_field(self):
        bounded = SymbolPartitioning(symbol_key_index=2, normalize_symbol=upper_symbol)

        with pytest.raises(ValueError, match="affected key has no configured symbol field"):
            bounded.for_key(("600519", date(2024, 1, 2)))

    @pytest.mark.parametrize("normalized", ["", None, 7], ids=["empty", "none", "non-string"])
    def test_for_key_demands_a_nonempty_string_symbol(self, normalized):
        bounded = SymbolPartitioning(symbol_key_index=0, normalize_symbol=lambda _value: normalized)

        with pytest.raises(
            ValueError, match="normalized affected-key symbol must be a nonempty string"
        ):
            bounded.for_key(("600519", date(2024, 1, 2)))

    def test_for_request_binds_the_request_symbol(self):
        bounded = SymbolPartitioning(symbol_key_index=0, normalize_symbol=upper_symbol)

        assert bounded.for_request("600519") == "600519"

    @pytest.mark.parametrize("normalized", ["", 0], ids=["empty", "non-string"])
    def test_for_request_demands_a_nonempty_string_symbol(self, normalized):
        bounded = SymbolPartitioning(symbol_key_index=0, normalize_symbol=lambda _value: normalized)

        with pytest.raises(ValueError, match="normalized request symbol must be a nonempty string"):
            bounded.for_request("   ")

    def test_canonicalize_key_normalizes_only_the_symbol(self):
        bounded = SymbolPartitioning(symbol_key_index=0, normalize_symbol=upper_symbol)

        canonical = bounded.canonicalize_key(("sh600519", date(2024, 1, 2), 12.5))

        assert canonical == ("SH600519", date(2024, 1, 2), 12.5)
        assert canonical == bounded.canonicalize_key(("SH600519", date(2024, 1, 2), 12.5))

    def test_canonicalize_key_needs_the_configured_symbol_field(self):
        bounded = SymbolPartitioning(symbol_key_index=3, normalize_symbol=upper_symbol)

        with pytest.raises(ValueError, match="affected key has no configured symbol field"):
            bounded.canonicalize_key(("600519", date(2024, 1, 2)))


class TestAffectedKeySpool:
    def test_append_rejects_one_dedupe_key_under_two_partition_symbols(self, tmp_path):
        original = ("600519", date(2024, 1, 2))
        aliased = ("SH600519", date(2024, 1, 2))
        other = ("000001", date(2024, 1, 3))

        with AffectedKeySpool(directory=tmp_path) as spool:
            spool.append(original, symbol="600519")
            # The alias normalizes to the stored dedupe tuple, so it is ignored
            # and the count stays at the one row actually written.
            spool.append(aliased, symbol="600519", dedupe_key=original)
            assert len(spool) == 1

            # The same dedupe tuple under a *different* partition symbol cannot
            # be attributed to two symbols at once.
            with pytest.raises(
                ValueError, match="one affected key normalized to multiple partition symbols"
            ):
                spool.append(aliased, symbol="000001", dedupe_key=original)

            assert len(spool) == 1
            spool.append(other, symbol="000001")

            assert len(spool) == 2
            assert list(spool) == [original, other]
            assert list(spool.partition(["000001"])) == [other]
            assert list(spool.partition(["missing"])) == []

    def test_getitem_supports_slices_indexes_and_bounds_errors(self, tmp_path):
        keys = [(f"{index:03d}", date(2024, 1, 2)) for index in range(5)]

        with AffectedKeySpool(directory=tmp_path) as spool:
            for key in keys:
                spool.append(key, symbol=key[0])

            assert spool[0] == keys[0]
            assert spool[4] == keys[4]
            assert spool[-1] == keys[-1]
            assert spool[-4] == keys[1]
            assert spool[1:4] == keys[1:4]
            assert spool[::2] == keys[::2]
            assert spool[:2] == keys[:2]
            assert spool[3:99] == keys[3:5]
            assert spool[10:20] == []
            assert isinstance(spool[1:4], list)

            with pytest.raises(IndexError) as excinfo:
                _ = spool[5]
            assert excinfo.value.args == (5,)

            with pytest.raises(IndexError) as excinfo:
                _ = spool[-6]
            assert excinfo.value.args == (-6,)

    def test_getitem_fails_closed_when_the_count_is_ahead_of_the_rows(self, tmp_path):
        # The in-memory count is O(1) bookkeeping; the OFFSET read on disk is
        # the authority, so a row the counter has but the table lacks has to
        # surface as IndexError rather than ``None``.
        with AffectedKeySpool(directory=tmp_path) as spool:
            spool.append(("600519", date(2024, 1, 2)), symbol="600519")
            assert len(spool) == 1

            spool._count = 3
            assert len(spool) == 3

            with pytest.raises(IndexError) as excinfo:
                _ = spool[1]
            assert excinfo.value.args == (1,)

            assert spool[0] == ("600519", date(2024, 1, 2))

            spool._count = 1
            assert len(spool) == 1
            assert list(spool) == [("600519", date(2024, 1, 2))]

    def test_commit_bookkeeping_flushes_every_configured_inserts(self, tmp_path, monkeypatch):
        with AffectedKeySpool(directory=tmp_path) as spool:
            monkeypatch.setattr(spool, "_COMMIT_EVERY", 2)

            for index in range(3):
                spool.append((f"{index:03d}", date(2024, 1, 2)), symbol=f"{index:03d}")
                if index < 2:
                    assert spool._since_commit == (index + 1) % 2

            assert spool._since_commit == 1
            assert len(spool) == 3
            assert list(spool) == [(f"{index:03d}", date(2024, 1, 2)) for index in range(3)]
            assert spool._since_commit == 0

    def test_iter_keys_ignores_an_empty_symbol_selection(self, tmp_path):
        with AffectedKeySpool(directory=tmp_path) as spool:
            spool.append(("600519", date(2024, 1, 2)), symbol="600519")

            assert list(spool.iter_keys(())) == []
            assert list(spool.iter_keys(["000001", "000001"])) == []
            assert list(spool.iter_keys(["600519"])) == [("600519", date(2024, 1, 2))]

    def test_count_for_symbols_deduplicates_and_handles_an_empty_selection(self, tmp_path):
        with AffectedKeySpool(directory=tmp_path) as spool:
            for index in range(3):
                spool.append((f"600519.{index}", date(2024, 1, 2)), symbol="600519")
            spool.append(("000001", date(2024, 1, 2)), symbol="000001")

            assert spool._count_for_symbols(("600519", "600519")) == 3
            assert spool._count_for_symbols([]) == 0
            assert spool._count_for_symbols(["missing"]) == 0
            assert len(spool.partition(["600519", "600519", "000001"])) == 4


class TestAffectedKeyView:
    def test_view_reads_counts_items_and_slices_from_the_spool(self, tmp_path):
        keys = [(f"{index:03d}", date(2024, 1, 2)) for index in range(5)]

        with AffectedKeySpool(directory=tmp_path) as spool:
            for index, key in enumerate(keys):
                spool.append(key, symbol="A" if index < 3 else "B")

            view = spool.partition(("A", "B", "A"))

            assert len(view) == 5
            assert list(view) == keys
            assert view[0] == keys[0]
            assert view[-1] == keys[4]
            assert view[1:4] == keys[1:4]
            assert view[::2] == keys[::2]
            assert view[10:20] == []
            assert view[:2] == keys[:2]

            with pytest.raises(IndexError) as excinfo:
                _ = view[5]
            assert excinfo.value.args == (5,)

            with pytest.raises(IndexError) as excinfo:
                _ = view[-6]
            assert excinfo.value.args == (-6,)

    def test_view_only_counts_its_own_symbols(self, tmp_path):
        with AffectedKeySpool(directory=tmp_path) as spool:
            spool.append(("A", date(2024, 1, 2)), symbol="A")
            spool.append(("B", date(2024, 1, 3)), symbol="B")

            view = spool.partition(["B"])

            assert len(view) == 1
            assert list(view) == [("B", date(2024, 1, 3))]
            assert view[0] == ("B", date(2024, 1, 3))

    def test_view_reports_index_error_when_the_count_is_ahead_of_the_keys(
        self, tmp_path, monkeypatch
    ):
        with AffectedKeySpool(directory=tmp_path) as spool:
            spool.append(("600519", date(2024, 1, 2)), symbol="600519")
            # A stale ``affected_key_counts`` row must not read as a real key.
            monkeypatch.setattr(spool, "_count_for_symbols", lambda symbols: 4)

            view = spool.partition(["600519"])

            assert len(view) == 4
            assert view[0] == ("600519", date(2024, 1, 2))

            with pytest.raises(IndexError) as excinfo:
                _ = view[3]

            assert excinfo.value.args == (3,)


@pytest.mark.parametrize(
    "operation",
    [
        lambda spool: spool.append(("600519",), symbol="600519"),
        lambda spool: len(spool),
        lambda spool: spool.partition(["600519"]),
        lambda spool: spool[0],
        lambda spool: list(spool.iter_keys()),
        lambda spool: spool._count_for_symbols(["600519"]),
    ],
    ids=["append", "len", "partition", "getitem", "iter-keys", "count-for-symbols"],
)
def test_closed_spool_refuses_every_read_and_write(tmp_path, operation):
    spool = AffectedKeySpool(directory=tmp_path)
    path = spool.path
    with spool:
        spool.append(("600519", date(2024, 1, 2)), symbol="600519")

    assert not path.exists()
    spool.close()  # idempotent: the second call returns before touching SQLite

    with pytest.raises(RuntimeError, match="affected-key spool is closed"):
        operation(spool)


def test_view_is_unusable_after_the_owning_spool_closes(tmp_path):
    spool = AffectedKeySpool(directory=tmp_path)
    with spool:
        spool.append(("600519", date(2024, 1, 2)), symbol="600519")
        view = spool.partition(["600519"])
        assert len(view) == 1

    with pytest.raises(RuntimeError, match="affected-key spool is closed"):
        len(view)

    with pytest.raises(RuntimeError, match="affected-key spool is closed"):
        list(view)
