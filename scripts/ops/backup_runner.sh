#!/usr/bin/env bash
# Run the repository backup at 02:00 UTC every day in the optional Compose job.

set -euo pipefail

while true; do
  IFS=' ' read -r hour minute second <<< "$(date -u '+%H %M %S')"
  now_seconds=$((10#$hour * 3600 + 10#$minute * 60 + 10#$second))
  target_seconds=$((2 * 3600))
  wait_seconds=$(((target_seconds - now_seconds + 86400) % 86400))
  if ((wait_seconds == 0)); then
    wait_seconds=86400
  fi
  echo "next opendata backup starts at 02:00 UTC"
  sleep "$wait_seconds"
  /opendata/scripts/ops/backup_mysql.sh
done
