#!/usr/bin/env bash
#
# Daily logical backup of both opendata databases (acceptance AC-19, NFR-9).
#
#   metadata database : users / tasks / executions / data catalog
#   warehouse database: ods_* / dwd_* / dq_diff_report
#
# RPO target: <= 24h. Combine with binlog shipping if a tighter RPO is needed.
#
# Usage:
#   ./scripts/ops/backup_mysql.sh              # backup with .env credentials
#   BACKUP_DIR=/mnt/backups ./scripts/ops/backup_mysql.sh
#
# Cron (daily 02:00):
#   0 2 * * * /opt/opendata/scripts/ops/backup_mysql.sh >> /var/log/opendata-backup.log 2>&1
#
# Requires the MySQL client tools (mysqldump, gzip) on PATH.
#
# Exit codes: 0 success, 1 configuration/preflight error, 2 backup failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Load .env without echoing it.
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_ROOT}/.env"
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-${PROJECT_ROOT}/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

MAIN_HOST="${MYSQL_HOST:-127.0.0.1}"
MAIN_PORT="${MYSQL_PORT:-3306}"
MAIN_DB="${MYSQL_DATABASE:-opendata}"

WH_HOST="${DATA_MYSQL_HOST:-127.0.0.1}"
WH_PORT="${DATA_MYSQL_PORT:-3306}"
WH_DB="${DATA_MYSQL_DATABASE:-opendata_data}"

: "${MYSQL_USER:?MYSQL_USER must be set (see .env.example)}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD must be set (see .env.example)}"

# Preflight: fail before creating any file, so a failed run never leaves an
# empty dump behind that a later restore could mistake for a valid backup.
for tool in mysqldump gzip; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "ERROR: '${tool}' not found on PATH." >&2
    echo "       Add the MySQL client bin directory (e.g. /opt/homebrew/opt/mysql/bin) to PATH." >&2
    exit 1
  fi
done

mkdir -p "${BACKUP_DIR}"

# Credentials go into a 0600 defaults file instead of --password on the command
# line, which would expose them to every local user via the process list.
DEFAULTS_FILE="$(mktemp "${TMPDIR:-/tmp}/opendata-backup.XXXXXX.cnf")"
chmod 600 "${DEFAULTS_FILE}"
cleanup() {
  rm -f "${DEFAULTS_FILE}"
}
trap cleanup EXIT INT TERM

dump_one() {
  local host="$1" port="$2" database="$3" label="$4"
  local target="${BACKUP_DIR}/${label}_${STAMP}.sql"

  cat > "${DEFAULTS_FILE}" <<CNF
[client]
user=${MYSQL_USER}
password=${MYSQL_PASSWORD}
host=${host}
port=${port}
CNF

  echo "==> dumping ${database} from ${host}:${port} -> ${target}"
  if ! mysqldump \
      --defaults-extra-file="${DEFAULTS_FILE}" \
      --single-transaction --quick --routines --triggers \
      --default-character-set=utf8mb4 \
      "${database}" > "${target}"; then
    rm -f "${target}" "${target}.gz"
    echo "ERROR: mysqldump failed for ${database}" >&2
    exit 2
  fi
  if [ ! -s "${target}" ]; then
    rm -f "${target}" "${target}.gz"
    echo "ERROR: mysqldump produced an empty file for ${database}" >&2
    exit 2
  fi
  gzip -f "${target}"
  echo "    $(du -h "${target}.gz" | cut -f1)  ${target}.gz"
}

echo "opendata backup started at ${STAMP}"
dump_one "${MAIN_HOST}" "${MAIN_PORT}" "${MAIN_DB}" "metadata"
dump_one "${WH_HOST}" "${WH_PORT}" "${WH_DB}" "warehouse"

echo "==> pruning backups older than ${RETENTION_DAYS} days"
find "${BACKUP_DIR}" -name 'metadata_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -name 'warehouse_*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "opendata backup finished"
