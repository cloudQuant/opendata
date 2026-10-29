# Symbol-universe and trading-calendar error behavior

This page describes the error handling in the current symbol-universe and trading-calendar readers. It records what operators can infer from their return values, exceptions, and logs, and the limits of the focused acceptance run. These paths are not evidence that the warehouse contains a complete A-share universe.

## DWD symbol-universe reads

[`symbol_universe`](../opendata/pipeline/jobs.py#L503) reads distinct symbols from the registered `dwd_<domain>` table. A successful query with no rows returns an empty list. It does not turn a failed database query into an empty result.

The reader identifies database failures by exception type: SQLAlchemy `DBAPIError`, pool `TimeoutError`, `DisconnectionError`, SQLite errors, and PyMySQL errors. It does not parse exception messages to classify them. For these supported failures, it emits the fixed warning `symbol universe query failed`, then raises `UniverseQueryError("symbol universe query failed")`. The exception is raised outside the handler, so its `__context__` and `__cause__` do not retain the driver exception. The SQL, DSN, bound parameters, driver message, and traceback are not included in this warning or exception.

Exceptions outside those supported database types propagate unchanged. This includes errors from operations outside the guarded query, such as converting returned values into strings. A programming, validation, or encoding error should therefore remain distinguishable from the supported database failure instead of being disguised as an empty universe.

[`derive_universe`](../opendata/pipeline/jobs.py#L412) raises typed errors for its derived-universe outcomes:

| Condition | Result |
| --- | --- |
| A successful DWD query returns no rows | `EmptySymbolUniverseError`, a `ValueError` subclass |
| The instrument catalog filters out every queried symbol | `EmptySymbolUniverseError`, also a `ValueError` subclass |
| The derived list contains more than the requested cap | `UniverseLimitExceededError`, a `ValueError` subclass |
| The limit has an invalid type or cannot be safely probed | `ValueError` |
| A supported database/driver error occurs in the DWD symbol query | `UniverseQueryError`, a `RuntimeError` subclass |

The two empty outcomes share the same exception type and message; callers should not infer which occurred from the exception alone. Non-positive integer limits retain compatibility behavior and are treated as a cap of one. Booleans and non-integers are rejected, and a cap at or above `sys.maxsize` is rejected before the `cap + 1` probe.

A missing DWD table is not separately detected by name. When its driver reports a supported database exception, it follows the same fixed-log `UniverseQueryError` path as other query failures. No special “table missing” diagnosis is produced.

## Trading-calendar reads

[`warehouse_calendar`](../opendata/pipeline/trading_calendar.py#L227) catches ordinary `Exception`s raised while importing the reader dependency, connecting, querying, or validating rows against the calendar contract. It logs only `trading calendar contract unavailable; using weekday fallback` and returns a weekday-only calendar. The exception details and traceback are not logged or attached to a returned error. `resolve_calendar(None)` also returns the weekday calendar, without logging.

The fallback treats Monday through Friday as trading days and has no holiday knowledge. A fallback therefore keeps a run moving when the calendar table is unavailable, but can classify exchange holidays as open days. Check the reported calendar tier when interpreting freshness or incremental-window decisions. This wide fallback also means an ordinary programming or contract-validation exception in the guarded read path may be presented as calendar unavailability.

## Nearby paths and retry behavior

This change only narrows and sanitizes the DWD symbol-query error path and keeps the trading-calendar fallback warning fixed. It does not redact all nearby warehouse logs. For example, [`landed_instruments`](../opendata/pipeline/jobs.py#L329) still logs the caught exception text when the instrument catalog cannot be read, and [`_count_in_window`](../opendata/pipeline/jobs.py#L695) still logs exception text before returning zero. Pipeline runner and task-execution logs were not cleaned up by this change; review those paths separately before assuming their logs omit database details.

The generic scheduler retry policy was not changed. A scheduled task with retries enabled continues to use the existing retry settings; the new exception types are not yet classified as non-retryable. In particular, deterministic scope and empty-universe errors may still be retried by the generic task policy. Review retry classification separately from this safe-log change.

[`operations-universe-truncation-guard.md`](operations-universe-truncation-guard.md) now describes the same current behavior for operators diagnosing cap and empty-universe failures.

## Acceptance evidence and limits

The focused offline acceptance run recorded **132 passed, 21 warnings**. Combined statement-plus-branch coverage, with zero excluded lines, was:

| Changed module | Combined coverage |
| --- | ---: |
| `opendata/pipeline/jobs.py` | 94.15% |
| `opendata/pipeline/trading_calendar.py` | 97.41% |
| `opendata/pipeline/universe_errors.py` | 100% |

The run used CPython 3.13.5, pytest 8.3.4, pytest-asyncio 1.0.0, pytest-cov 6.2.1, and coverage 7.9.2. The repository's declared pins differ for pytest (8.3.3), pytest-asyncio (0.24.0), and pytest-cov (6.0.0). This focused selection does not establish the repository-wide A2 coverage or static-check gates; the required `docs/evidence/C100/quality-scope-audit/immutable-wave9-exact5055/classification.json` evidence is missing.

The tests used synthetic database failures and local/in-memory SQLite fixtures in an isolated, network-disabled sandbox. They did not contact a live provider, test production MySQL/PyMySQL error shapes against a real service, inspect a production database, exercise a production scheduler, or establish all-A-share scope or historical completeness. The durable acceptance archive is [`universe-errors-acceptance-20261029/README.md`](../var/reports/afi204/universe-errors-acceptance-20261029/README.md); its `SHA256SUMS` manifest passed verification. `acceptance/REPORT.md` is a pre-run preparation note and does not contain the result. The recorded pass count and coverage are in `acceptance/pytest.stdout.log` and `acceptance/coverage.json`. Scoped Ruff check/format, Bandit, and Gitleaks passed; mypy added no new diagnostics, with the same four baseline diagnostics remaining. These scoped static checks and the focused test selection do not establish the repository-wide A2 coverage or full quality gate.
