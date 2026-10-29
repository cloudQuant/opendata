# Fuyao stock-daily one-shot pilot

This is a manually invoked, one-time observation tool for the existing THS/Fuyao
provider. It is **not** a production ingestion job and has not been run against
Fuyao as part of this implementation. The only permitted query is one `GET` to
`https://fuyao.aicubes.cn/api/a-share/prices/historical` for `600519.SH`, from
2026-10-19 through 2026-10-23 inclusive, using the unadjusted series (`adjust=none`).
It does not create database rows, publish an API, register a provider, enable a
scheduler, or claim complete history.

The operator supplied authorization to use AkShare and Fuyao data. That statement
is recorded as the operator's authorization; this pilot does not independently
verify a vendor contract, source retention rights, or permission to redistribute
the result to another service. Keep this captured source sample private until those
rights and the observed fields have been separately reviewed.

## Admission and execution

Run only from a local development environment. The script requires `APP_ENV`
to be exactly `development` and refuses a testing environment. Importing the
module does not read credentials or perform I/O. Credentials are resolved only
after the exact one-use authorization has been validated; the existing Fuyao
settings path reads `FUYAO_API_KEY` from the environment or project `.env`.
Never put the key in command arguments, the authorization file, source code, or
the evidence directory. The script rejects any configured API origin other than
the fixed HTTPS Fuyao host.

Create a new private working directory owned by the current user. The directory
must already have mode `0700`; the script will not change permissions on it or
on any ancestor. For example, choose a suitable existing private parent and run:

```sh
mkdir -m 700 /absolute/private/path/fuyao-pilot
```

Use a fresh authorization document for each manual attempt. This helper writes
the exact fixed scope, a 10-minute expiry, and a random nonce to a new `0600`
file. Set `FUYAO_PILOT_PRIVATE_ROOT` to the private directory created above.
The helper prints no authorization values:

```sh
FUYAO_PILOT_PRIVATE_ROOT=/absolute/private/path/fuyao-pilot python - <<'PY'
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path(os.environ["FUYAO_PILOT_PRIVATE_ROOT"])
now = datetime.now(timezone.utc)
timestamp = lambda value: value.isoformat(timespec="microseconds").replace("+00:00", "Z")
document = {
    "schema_version": 1,
    "purpose": "fuyao_stock_daily_pilot",
    "symbol": "600519.SH",
    "start_date": "2026-10-19",
    "end_date": "2026-10-23",
    "adjustment_basis": "unadjusted",
    "max_requests": 1,
    "max_response_bytes": 65536,
    "issued_at_utc": timestamp(now),
    "expires_at_utc": timestamp(now + timedelta(minutes=10)),
    "nonce": secrets.token_hex(16),
}
path = root / "authorization.json"
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short authorization write")
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
```

From the OpenData repository root, invoke the script once with a **new** evidence
directory beneath the same private root:

```sh
APP_ENV=development python -m scripts.ops.fuyao_stock_daily_pilot \
  --authorization-file /absolute/private/path/fuyao-pilot/authorization.json \
  --evidence-dir /absolute/private/path/fuyao-pilot/evidence
```

The authorization nonce is atomically marked consumed in the authorization
directory before credential resolution or transport. The marker contains only
hashes and a timestamp. A second use of that nonce in the same directory is
rejected. This is a local replay guard, not a global ledger: copying the
authorization to another private directory is outside its guarantee. A failed
or ambiguous attempt still consumes the nonce; do not retry it. Create a new
authorization only after reviewing the prior outcome and confirming that another
attempt is intended.

## Request and evidence bounds

The production client is constructed with one maximum attempt, the fixed host,
TLS verification, `trust_env=False`, redirects disabled, a 12-second client
timeout, a 12-second cooperative request-scope deadline, and an explicit no-op
raw-response cache. A request grant allows only source `ths`, model
`stock_daily`, operation `query`, and host `fuyao.aicubes.cn`. No caller-supplied
symbol, date, adjustment, host, retry, or byte-limit override is accepted.

The `65536`-byte limit is the maximum **application-cumulative response entity
bytes** accepted by the bounded transport. It is not a wire-byte limit or a hard
process-RSS bound: the HTTP transport can buffer an individual chunk before the
application checks it, and temporary copies may exist. Likewise, the timeout is
cooperative and does not promise a strict wall-clock cutoff for every transport
behavior.

The evidence directory is created new with mode `0700`; files are created
exclusively with private permissions and fsynced. `reservation.json` records the
fixed scope and request hash before transport; `local_budget_attempts_reserved`
is zero there because it records scope admission before the request budget is
used. `capture.json` records the number of local budget reservations after the
attempt. Both manifests set `physical_send_count` to null because the local
budget cannot observe physical socket sends or prove that Fuyao received or
processed a request. A recorded HTTP response status is evidence that the client
observed a response, not evidence about upstream processing. **No raw source response or raw
response digest is ever persisted**, including for HTTP errors and rejected
payloads. For an HTTP 200 response, the script strictly parses the bounded body,
checks its echoed symbol, source field types, dates, duplicate keys, finite OHLCV
values, positive OHLC prices, nonnegative volume/turnover, and OHLC relationships,
then writes only the selected source market fields and their values to
`observations.json`. It retains the source names
(`date_ms`, `open_price`, `high_price`, `low_price`, `close_price`, `volume`, and
`turnover`), the fixed symbol and unadjusted basis, and the timestamp semantics.
It does not store other response fields such as messages or request IDs. The
private `capture.json` records the HTTP status, row count, observed date bounds,
validation outcome, and completeness limits. A normal success marks
`validated_observations_persisted=true` and
`observations_artifact_state=complete_published_sample`; the source response body
itself remains unsaved. Non-success or invalid responses produce only a
sanitized failure summary.

The transport body is checked in memory for the active key in raw UTF-8, common
URL, Base64, hexadecimal, and JSON-escaped forms; a match fails closed without
persisting the response. This scan is an additional safeguard, not the storage
boundary: persistence is limited to fields that passed validation, so the tool
does not rely on enumerating every possible encoding. Credentials,
authorization nonce, headers, complete URL, response request ID, raw exception
text, and arbitrary response text are not written to logs or evidence files.

`failed_before_transport` means the local attempt budget was not reserved.
`transport_outcome_unknown` means the request may have reached Fuyao but no
definitive response was captured. `http_error_response_received` records an
observed non-success HTTP status. `response_rejected` means a response arrived
but failed a size, format, identity, date, or validation check. Treat an unknown
outcome as consumed and do not replay its nonce.

`source_response_persisted=false` means the raw provider response body was not
saved; it does not describe the separate whitelisted observations artifact.
The selected, validated observation file has separate status fields. If
writing `observations.json` fails, `capture.json` uses
`response_observed_not_persisted`, with `validated_observations_persisted=false`
and `observations_artifact_state=write_failed_may_be_partial`. The writer tries
to remove a partial file with an inode check, but that check and unlink are not
atomic; inspect the private evidence directory rather than assuming the file is
absent. This cleanup is best-effort against accidental or asynchronous path
replacement, not a defense against a malicious same-UID process.

If `observations.json` is fully written but the success `capture.json` cannot be
written, the complete whitelisted sample is intentionally retained for recovery.
The failure manifest uses `capture_state=validated_sample_unmanifested`,
`validated_observations_persisted=true`, and
`observations_artifact_state=complete_unpublished_sample`. The run is still a
failed/unpublished capture, not `sample_captured`; the file is not automatically
promoted or retried. An operator may inspect it for recovery, while treating the
nonce as consumed. In both persistence failures the manifest records that the
local client observed an HTTP response, not that Fuyao received or processed the
request; `response_body_saved=false` and `physical_send_count=null` remain true.

## Interpretation limits

The current THS contract labels volume in shares and turnover/amount in CNY; the
pilot records that contract label but does not independently calibrate provider
units. It validates the echoed symbol, unique in-window business dates, finite
numeric OHLCV values, positive OHLC prices, nonnegative volume/turnover, and
basic OHLC relationships. It does not use a separately
verified trading calendar, reconcile suspensions, listing boundaries, source
retention, or head/interior/tail gaps. A five-day sample is not evidence of
window completeness, current freshness, all-A-share coverage, or history from
inception. The output remains an observation artifact and must not be treated as
accepted production data by itself.

The offline tests use synthetic credentials, fake-client behavior tests, and a
real `FuyaoHttpClient` over HTTPX `MockTransport` to exercise request admission,
the bounded send, and the single-send ceiling. They do not read `.env`, access a
database, or contact Fuyao.
