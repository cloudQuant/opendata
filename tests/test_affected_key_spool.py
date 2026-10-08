"""Focused checks for bounded affected-key storage."""

from datetime import date, datetime, timezone

import pytest

from opendata.pipeline.affected_keys import AffectedKeySpool


def test_spool_round_trips_typed_keys_deduplicates_and_cleans_up(tmp_path):
    spool_path = None
    first = ("600519.SH", date(2024, 1, 2))
    second = ("000001", datetime(2024, 1, 3, 12, 30, tzinfo=timezone.utc))

    with AffectedKeySpool(directory=tmp_path) as spool:
        spool_path = spool.path
        assert spool.path.stat().st_mode & 0o777 == 0o600
        spool.append(first, symbol="600519", dedupe_key=("600519", date(2024, 1, 2)))
        spool.append(second, symbol="000001")
        spool.append(first, symbol="600519", dedupe_key=("600519", date(2024, 1, 2)))
        spool.append(
            ("600519", date(2024, 1, 2)),
            symbol="600519",
            dedupe_key=("600519", date(2024, 1, 2)),
        )

        assert len(spool) == 2
        assert list(spool) == [first, second]
        assert list(spool.partition(("000001",))) == [second]
        assert len(spool.partition(("000001",))) == 1

    assert spool_path is not None
    assert not spool_path.exists()


@pytest.mark.parametrize("exception", [RuntimeError("failed"), KeyboardInterrupt()])
def test_spool_cleans_up_for_exception_and_baseexception(tmp_path, exception):
    spool = AffectedKeySpool(directory=tmp_path)
    spool_path = spool.path

    with pytest.raises(type(exception)), spool:
        spool.append(("600519", date(2024, 1, 2)), symbol="600519")
        raise exception

    assert not spool_path.exists()


def test_spool_iteration_uses_bounded_fetchmany(tmp_path, monkeypatch):
    spool = AffectedKeySpool(directory=tmp_path)
    monkeypatch.setattr(spool, "_FETCH_SIZE", 7)
    with spool:
        expected = [(f"{index:06d}", date(2024, 1, 2)) for index in range(1_003)]
        for key in expected:
            spool.append(key, symbol=key[0])

        assert len(spool) == len(expected)
        iterator = iter(spool)
        assert next(iterator) == expected[0]
        assert list(iterator) == expected[1:]


def test_spool_creation_failure_removes_the_private_file(tmp_path, monkeypatch):
    from opendata.pipeline import affected_keys

    def fail_chmod(descriptor, mode):
        raise RuntimeError("chmod failed")

    monkeypatch.setattr(affected_keys.os, "fchmod", fail_chmod)
    with pytest.raises(RuntimeError, match="chmod failed"):
        AffectedKeySpool(directory=tmp_path)

    assert list(tmp_path.iterdir()) == []
