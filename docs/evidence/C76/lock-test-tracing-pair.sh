#!/bin/sh
# Re-run: sh docs/evidence/C76/lock-test-tracing-pair.sh > docs/evidence/C76/lock-test-tracing-pair-face.txt
#
# The same lock test twice on one box, back to back. Arm "on" keeps pytest.ini's addopts, so it runs
# the way member 12 runs it (--cov=opendata --cov-report=term-missing --cov-report=html, and the
# .pth hook therefore traces the test's own python -c worker). Arm "off" drops addopts, so nothing is
# traced. The worker prints its own stage markers, so the two arms separate the child's cost from
# the parent's reporting cost.
set -eu

cd /Users/yunjinqi/Documents/new_projects/opendata
T="tests/test_minute_archive.py::test_global_snapshot_lock_blocks_writer_and_expired_shard_purge_processes"

for arm in on off; do
    if [ "$arm" = on ]; then
        EXTRA=""
    else
        EXTRA="--override-ini=addopts="
    fi
    rm -rf "/tmp/lock-cov-$arm"
    echo "=== cov-$arm: python3.11 -m pytest $T -q -p no:randomly $EXTRA --durations=3 ==="
    # shellcheck disable=SC2086
    python3.11 -m pytest "$T" -q -p no:randomly $EXTRA --durations=3 --basetemp="/tmp/lock-cov-$arm" 2>&1 | tail -14
    find "/tmp/lock-cov-$arm" -name '*-stage' | sort | while read -r f; do
        printf '  %s = %s\n' "$(basename "$f")" "$(cat "$f")"
    done
    echo
done
