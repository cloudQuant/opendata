#!/usr/bin/env bash
# Daily logical backup of the metadata and warehouse MySQL databases.
#
# Explicit environment variables take precedence over the optional dotenv file.
# Set BACKUP_ENV_FILE="" to skip dotenv loading, which is useful for containers
# and isolated backup jobs. The dotenv reader treats values as data; it never
# executes the file as shell code.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ ${BACKUP_ENV_FILE+x} ]]; then
  ENV_FILE="${BACKUP_ENV_FILE}"
  ENV_FILE_EXPLICIT=true
else
  ENV_FILE="${PROJECT_ROOT}/.env"
  ENV_FILE_EXPLICIT=false
fi

load_dotenv() {
  local file="$1" explicit="$2" line key value trimmed
  [[ -n "$file" ]] || return 0
  if [[ ! -f "$file" ]]; then
    if [[ "$explicit" == true ]]; then
      echo "ERROR: BACKUP_ENV_FILE does not exist: ${file}" >&2
      exit 1
    fi
    return 0
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    trimmed="${trimmed#export }"
    if [[ ! "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      continue
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$key" in
      MYSQL_HOST|MYSQL_PORT|MYSQL_USER|MYSQL_PASSWORD|MYSQL_DATABASE|\
      DATA_MYSQL_HOST|DATA_MYSQL_PORT|DATA_MYSQL_USER|DATA_MYSQL_PASSWORD|\
        DATA_MYSQL_DATABASE|BACKUP_DIR|BACKUP_RETENTION_DAYS|\
        BACKUP_MINUTE_ARCHIVE_DIR|BACKUP_PYTHON)
        ;;
      *) continue ;;
    esac
    # Do not replace values explicitly supplied by the caller.
    [[ ${!key+x} ]] && continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
      value="${value//\\\"/\"}"
      value="${value//\\\\/\\}"
    elif [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
    else
      value="${value%%[[:space:]]\#*}"
      value="${value%"${value##*[![:space:]]}"}"
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$file"
}

load_dotenv "$ENV_FILE" "$ENV_FILE_EXPLICIT"

BACKUP_DIR="${BACKUP_DIR:-${PROJECT_ROOT}/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)_$$"

MAIN_HOST="${MYSQL_HOST:-127.0.0.1}"
MAIN_PORT="${MYSQL_PORT:-3306}"
MAIN_DB="${MYSQL_DATABASE:-opendata}"
MAIN_USER="${MYSQL_USER:-}"
MAIN_PASSWORD="${MYSQL_PASSWORD:-}"

WH_HOST="${DATA_MYSQL_HOST:-127.0.0.1}"
WH_PORT="${DATA_MYSQL_PORT:-3306}"
WH_DB="${DATA_MYSQL_DATABASE:-opendata_data}"
WH_USER="${DATA_MYSQL_USER:-}"
WH_PASSWORD="${DATA_MYSQL_PASSWORD:-}"

if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ || ! "$RETENTION_DAYS" =~ [1-9] ]]; then
  echo "ERROR: BACKUP_RETENTION_DAYS must be a positive whole number." >&2
  exit 1
fi
if [[ -z "$MAIN_USER" || -z "$MAIN_PASSWORD" ]]; then
  echo "ERROR: MYSQL_USER and MYSQL_PASSWORD must be set for metadata backup." >&2
  exit 1
fi
if [[ -z "$WH_USER" || -z "$WH_PASSWORD" ]]; then
  echo "ERROR: DATA_MYSQL_USER and DATA_MYSQL_PASSWORD must be set for warehouse backup." >&2
  exit 1
fi

validate_option_value() {
  local label="$1" value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "ERROR: ${label} cannot contain newline characters." >&2
    exit 1
  fi
}
validate_option_value MYSQL_USER "$MAIN_USER"
validate_option_value MYSQL_PASSWORD "$MAIN_PASSWORD"
validate_option_value MYSQL_HOST "$MAIN_HOST"
validate_option_value MYSQL_PORT "$MAIN_PORT"
validate_option_value DATA_MYSQL_USER "$WH_USER"
validate_option_value DATA_MYSQL_PASSWORD "$WH_PASSWORD"
validate_option_value DATA_MYSQL_HOST "$WH_HOST"
validate_option_value DATA_MYSQL_PORT "$WH_PORT"
validate_option_value BACKUP_DIR "$BACKUP_DIR"
MINUTE_ARCHIVE_DIR="${BACKUP_MINUTE_ARCHIVE_DIR:-}"
validate_option_value BACKUP_MINUTE_ARCHIVE_DIR "$MINUTE_ARCHIVE_DIR"

for tool in mysqldump gzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required backup tool '${tool}' not found on PATH." >&2
    exit 1
  fi
done

if [[ -n "$MINUTE_ARCHIVE_DIR" ]]; then
  PYTHON_BIN="${BACKUP_PYTHON:-python3}"
  validate_option_value BACKUP_PYTHON "$PYTHON_BIN"
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: configured backup Python executable was not found on PATH." >&2
    exit 1
  fi
  exec "$PYTHON_BIN" "${SCRIPT_DIR}/backup_minute_snapshot.py" \
    --backup-script "${SCRIPT_DIR}/backup_mysql.sh" \
    --archive-root "$MINUTE_ARCHIVE_DIR" \
    --backup-dir "$BACKUP_DIR" \
    --retention-days "$RETENTION_DAYS"
fi

mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
DEFAULTS_FILE="$(mktemp "${TMPDIR:-/tmp}/opendata-backup.XXXXXX")"
chmod 600 "$DEFAULTS_FILE"
declare -a TEMP_FILES=()
declare -a PUBLISHED_FILES=()
BACKUP_COMPLETE=false

cleanup() {
  local path
  if (( ${#TEMP_FILES[@]} > 0 )); then
    for path in "${TEMP_FILES[@]}"; do
      rm -f "$path"
    done
  fi
  if [[ "$BACKUP_COMPLETE" != true ]]; then
    if (( ${#PUBLISHED_FILES[@]} > 0 )); then
      for path in "${PUBLISHED_FILES[@]}"; do
        rm -f "$path"
      done
    fi
  fi
  rm -f "$DEFAULTS_FILE"
}
trap cleanup EXIT
trap 'exit 2' INT TERM

escape_option_value() {
  local escaped="$1"
  if [[ "$escaped" == *$'\n'* || "$escaped" == *$'\r'* ]]; then
    echo "ERROR: MySQL credentials cannot contain newline characters." >&2
    return 1
  fi
  escaped="${escaped//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  printf '%s' "$escaped"
}

write_defaults_file() {
  local username="$1" password="$2" host="$3" port="$4"
  local escaped_username escaped_password escaped_host escaped_port
  escaped_username="$(escape_option_value "$username")" || return 1
  escaped_password="$(escape_option_value "$password")" || return 1
  escaped_host="$(escape_option_value "$host")" || return 1
  escaped_port="$(escape_option_value "$port")" || return 1
  {
    printf '[client]\n'
    printf 'user="%s"\n' "$escaped_username"
    printf 'password="%s"\n' "$escaped_password"
    printf 'host="%s"\n' "$escaped_host"
    printf 'port="%s"\n' "$escaped_port"
  } > "$DEFAULTS_FILE"
}

dump_one() {
  local host="$1" port="$2" database="$3" label="$4" username="$5" password="$6"
  local raw_temp compressed_temp final
  raw_temp="$(mktemp "${BACKUP_DIR}/.${label}_${STAMP}.XXXXXX")"
  compressed_temp="${raw_temp}.gz"
  final="${BACKUP_DIR}/${label}_${STAMP}.sql.gz"
  TEMP_FILES+=("$raw_temp" "$compressed_temp")
  if [[ -e "$final" ]]; then
    echo "ERROR: refusing to overwrite existing backup: ${final}" >&2
    return 1
  fi

  write_defaults_file "$username" "$password" "$host" "$port"
  echo "==> dumping ${label} database to ${final}"
  if ! mysqldump \
    --defaults-extra-file="$DEFAULTS_FILE" \
    --single-transaction --quick --routines --triggers \
    --default-character-set=utf8mb4 \
    "$database" > "$raw_temp"; then
    echo "ERROR: mysqldump failed for ${label} database." >&2
    return 2
  fi
  if [[ ! -s "$raw_temp" ]]; then
    echo "ERROR: mysqldump produced an empty ${label} dump." >&2
    return 2
  fi
  if ! gzip -n -c "$raw_temp" > "$compressed_temp" || ! gzip -t "$compressed_temp"; then
    echo "ERROR: compression validation failed for ${label} database." >&2
    return 2
  fi
  if [[ ! -s "$compressed_temp" ]]; then
    echo "ERROR: compression produced an empty ${label} dump." >&2
    return 2
  fi
  printf -v "${label}_TEMP_GZ" '%s' "$compressed_temp"
  printf -v "${label}_FINAL" '%s' "$final"
}

echo "opendata backup started at ${STAMP}"
dump_one "$MAIN_HOST" "$MAIN_PORT" "$MAIN_DB" metadata "$MAIN_USER" "$MAIN_PASSWORD"
dump_one "$WH_HOST" "$WH_PORT" "$WH_DB" warehouse "$WH_USER" "$WH_PASSWORD"

# Rename validated gzip files into their final names only after both database
# dumps completed. The exit trap rolls back a partially published pair.
for label in metadata warehouse; do
  temp_name="${label}_TEMP_GZ"
  final_name="${label}_FINAL"
  temp_path="${!temp_name}"
  final_path="${!final_name}"
  if ! ln "$temp_path" "$final_path"; then
    echo "ERROR: could not publish validated backup ${final_path}." >&2
    exit 2
  fi
  PUBLISHED_FILES+=("$final_path")
  rm -f "$temp_path"
done
BACKUP_COMPLETE=true

echo "==> pruning backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -type f \
  \( -name 'metadata_*.sql.gz' -o -name 'warehouse_*.sql.gz' \) \
  -mtime "+${RETENTION_DAYS}" ! -path "${BACKUP_DIR}/snapshot_*/*" -delete

echo "opendata backup finished"
