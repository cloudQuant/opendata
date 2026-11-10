"""The scheduler columns must be written in the same naive-UTC shape as the execution rows.

Production evidence (2026-11-06): of 1098 armed tasks, 852 had ``last_execution_at`` equal to the
UTC instant of their newest execution and 184 were exactly +8h -- two writers, two clocks, and
``run_task_now`` writes last, so its host-local value wins.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from opendata.afi_port.runtime.clock import to_naive_utc, utcnow_naive
from opendata.afi_port.runtime.scheduler import _inactive_job_task_ids, _job_id

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_shanghai_wall_slot_becomes_the_utc_instant():
    shanghai_0300 = datetime(2026, 11, 6, 3, 0, tzinfo=SHANGHAI)

    converted = to_naive_utc(shanghai_0300)

    assert converted.tzinfo is None
    assert converted == datetime(2026, 11, 5, 19, 0)


def test_utc_aware_and_none_pass_through_without_shifting():
    utc_noon = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    assert to_naive_utc(utc_noon) == datetime(2026, 11, 6, 12, 0)
    assert to_naive_utc(None) is None


def test_utcnow_naive_is_naive_and_matches_the_utc_clock():
    stored = utcnow_naive()

    assert stored.tzinfo is None
    drift = abs(datetime.now(timezone.utc).replace(tzinfo=None) - stored)
    assert drift < timedelta(seconds=5)


def test_cron_slot_from_the_scheduler_timezone_is_stored_as_utc():
    trigger = CronTrigger.from_crontab("0 3 * * *", timezone=SHANGHAI)
    next_run = trigger.get_next_fire_time(None, datetime.now(SHANGHAI))

    assert next_run is not None
    stored = to_naive_utc(next_run)

    assert stored.hour == (next_run.hour - next_run.utcoffset().total_seconds() / 3600) % 24
    assert stored.minute == next_run.minute
    assert stored.tzinfo is None


def test_reload_drops_jobs_whose_task_is_no_longer_armed():
    armed = [11, 12]
    registered = [
        _job_id(11),
        _job_id(12),
        _job_id(1050),
        _job_id(1099),
        "some_other_job",
        "ak_task_notanint",
    ]

    assert _inactive_job_task_ids(armed, registered) == [1050, 1099]


@pytest.mark.parametrize("task_id", [1, 1098])
def test_job_id_round_trips_to_the_task_id(task_id: int):
    job_id = _job_id(task_id)

    assert job_id.startswith("ak_task_")
    assert _inactive_job_task_ids([], [job_id]) == [task_id]
