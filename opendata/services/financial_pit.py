"""Transactional storage and bounded reads for financial PIT versions."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, SupportsIndex, SupportsInt
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from opendata.pipeline.financial_pit import (
    MAX_PIT_CANDIDATES,
    MAX_PIT_OFFSET,
    MAX_PIT_SYMBOLS,
    MAX_PIT_WINDOW_DAYS,
    MAX_VERSION_BATCH,
    FinancialCoverageInput,
    FinancialPitConflict,
    FinancialPitCoverageGap,
    FinancialPitError,
    FinancialPitQueryTooBroad,
    FinancialPitUnsupported,
    FinancialVersionInput,
    IngestResult,
    ParsedAsOf,
    PreparedFinancialVersion,
    _sha256,
    parse_as_of,
    prepare_financial_coverage,
    prepare_financial_version,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection, Engine, RowMapping


_VERSION_TABLE = "financial_pit_versions"
_COVERAGE_TABLE = "financial_pit_coverage"
_CONFLICT_TABLE = "financial_pit_conflicts"
_MAX_PIT_COVERAGE_CANDIDATES = 64
_VERSION_INSERT_COLUMNS = (
    "version_id, domain, source, market, symbol, report_period, announce_date, "
    "statement_type, item, indicator, source_revision, revision_order, "
    "public_time_precision, published_at_utc, published_on, visible_at_utc, "
    "source_timezone, observed_at_utc, provenance, payload_json, payload_digest"
)
_CONFLICT_INSERT_COLUMNS = (
    "conflict_id, version_id, domain, source, market, symbol, report_period, "
    "statement_type, item, indicator, source_revision, stored_digest, incoming_digest, "
    "stored_payload_json, incoming_payload_json, stored_provenance, incoming_provenance, "
    "detected_at_utc"
)


def ingest_financial_versions(
    engine: Engine,
    records: Sequence[FinancialVersionInput],
) -> IngestResult:
    """Persist a bounded source-normalized batch in one transaction.

    Replaying the same source revision and semantic content is idempotent.
    Conflicting content is committed to the conflicts table and returned in
    ``IngestResult.conflicts``; it is never replaced by the later arrival.
    """
    with engine.begin() as connection:
        result = ingest_financial_versions_in_transaction(connection, records)
    return result


def ingest_financial_versions_in_transaction(
    connection: Connection,
    records: Sequence[FinancialVersionInput] | Sequence[object],
) -> IngestResult:
    """Write PIT versions using a caller's open transaction.

    ``DwdWriter`` uses this function when its optional ``financial_versions``
    argument is supplied, keeping the legacy DWD upsert and PIT version batch
    on the same connection.  Conflicts are reported rather than raised here so
    the surrounding transaction can commit their evidence.
    """
    prepared = _prepare_batch(records)
    inserted = 0
    idempotent = 0
    conflicts: list[str] = []
    for version in prepared:
        values = dict(version.values)
        existing = _existing_financial_version(connection, str(values["version_id"]))
        if existing is not None:
            if existing["payload_digest"] == values["payload_digest"]:
                idempotent += 1
                continue
            conflict = _conflict_values(existing, values)
            _insert_ignore(
                connection,
                _CONFLICT_TABLE,
                _CONFLICT_INSERT_COLUMNS,
                conflict,
                conflict_column="conflict_id",
            )
            conflicts.append(str(values["version_id"]))
            continue

        inserted_row = _insert_ignore(
            connection,
            _VERSION_TABLE,
            _VERSION_INSERT_COLUMNS,
            values,
            conflict_column="version_id",
        )
        if inserted_row:
            inserted += 1
            continue

        # A concurrent insert won after the first SELECT.  Re-read it inside
        # this transaction and classify it with the same idempotency/conflict
        # rule rather than overwriting it.
        existing = _existing_financial_version(
            connection,
            str(values["version_id"]),
            current_read=connection.dialect.name == "mysql",
        )
        if existing is None:
            raise RuntimeError("PIT version insert was ignored without an existing version")
        if existing["payload_digest"] == values["payload_digest"]:
            idempotent += 1
        else:
            conflict = _conflict_values(existing, values)
            _insert_ignore(
                connection,
                _CONFLICT_TABLE,
                _CONFLICT_INSERT_COLUMNS,
                conflict,
                conflict_column="conflict_id",
            )
            conflicts.append(str(values["version_id"]))
    return IngestResult(
        inserted=inserted,
        idempotent=idempotent,
        conflicts=tuple(dict.fromkeys(conflicts)),
    )


def declare_financial_pit_coverage(
    engine: Engine,
    record: FinancialCoverageInput,
) -> str:
    """Persist one explicit per-source/symbol completeness declaration."""
    values = prepare_financial_coverage(record)
    _require_registered_financial_source(
        str(values["domain"]), str(values["source"]), str(values["market"])
    )
    columns = ", ".join(values)
    with engine.begin() as connection:
        _insert_ignore(
            connection,
            _COVERAGE_TABLE,
            columns,
            values,
            conflict_column="coverage_id",
        )
    return str(values["coverage_id"])


def query_financial_as_of(
    engine: Engine,
    *,
    domain: str,
    source: str,
    market: str,
    symbols: Sequence[str],
    report_period_start: date,
    report_period_end: date,
    as_of: str | date | datetime,
    selected_fields: Sequence[str],
    period: str | None = None,
    report_date: date | None = None,
    page: int = 1,
    page_size: int = 200,
) -> dict[str, object]:
    """Return the latest non-conflicted, proven version visible as of a time.

    Coverage is checked per exact source, market and symbol.  The request must
    name its market and symbols and may read at most 20 symbols, 10 years of
    report periods, 4,096 candidate versions and a 100,000-row offset.  Date
    ``as_of`` means end-of-day in the certified source timezone; a timestamp
    means that exact timezone-aware instant.
    """
    from opendata.pipeline.financial_pit import FINANCIAL_DOMAINS

    parsed_as_of = parse_as_of(as_of)
    _validate_query(
        domain=domain,
        source=source,
        market=market,
        symbols=symbols,
        report_period_start=report_period_start,
        report_period_end=report_period_end,
        page=page,
        page_size=page_size,
        selected_fields=selected_fields,
    )
    if domain not in FINANCIAL_DOMAINS:
        raise FinancialPitError("as_of is available only for financial domains")
    _require_registered_financial_source(domain, source, market)

    with engine.begin() as connection:
        _require_pit_tables(connection)
        coverage_by_symbol = _coverages_for_query(
            connection,
            domain=domain,
            source=source,
            market=market.upper(),
            symbols=symbols,
            period_start=report_period_start,
            period_end=report_period_end,
            as_of=parsed_as_of,
        )
        cutoff_by_symbol = {
            symbol: _query_cutoff(parsed_as_of, str(coverage["source_timezone"]))
            for symbol, coverage in coverage_by_symbol.items()
        }
        _raise_for_conflicts(
            connection,
            domain=domain,
            source=source,
            market=market.upper(),
            symbols=symbols,
            period_start=report_period_start,
            period_end=report_period_end,
            cutoff_by_symbol=cutoff_by_symbol,
            coverage_by_symbol=coverage_by_symbol,
        )
        candidates = _candidate_versions(
            connection,
            domain=domain,
            source=source,
            market=market.upper(),
            symbols=symbols,
            period_start=report_period_start,
            period_end=report_period_end,
        )

    for row in candidates:
        coverage = coverage_by_symbol.get(str(row["symbol"]))
        if coverage is not None and row["source_timezone"] != coverage["source_timezone"]:
            raise FinancialPitUnsupported(
                "version source_timezone differs from the explicitly certified coverage timezone"
            )
    unknown_time_rows = [row for row in candidates if row["public_time_precision"] == "unknown"]
    if unknown_time_rows:
        affected = sorted(
            {
                "|".join(
                    str(row[field] or "")
                    for field in ("symbol", "report_period", "statement_type", "item", "indicator")
                )
                for row in unknown_time_rows
            }
        )
        raise FinancialPitUnsupported(
            "PIT_UNSUPPORTED: matching source observations lack a proven public timestamp: "
            + ", ".join(affected[:32])
        )
    visible = [
        row
        for row in candidates
        if row["public_time_precision"] in {"timestamp", "date"}
        and row["visible_at_utc"] is not None
        and _coerce_datetime(row["visible_at_utc"]) <= cutoff_by_symbol[str(row["symbol"])]
        and (report_date is None or _coerce_date(row["announce_date"]) == report_date)
        and (period is None or _coerce_date(row["report_period"]) == _parse_period(period))
    ]
    selected = _latest_versions(visible, domain)
    projected = [_project_row(row, selected_fields) for row in selected]
    projected.sort(key=lambda row: _output_sort_key(row, domain))
    offset = (page - 1) * page_size
    if offset > MAX_PIT_OFFSET:
        raise FinancialPitQueryTooBroad(f"PIT offset exceeds {MAX_PIT_OFFSET}")
    page_rows = projected[offset : offset + page_size]
    return {
        "rows": page_rows,
        "columns": list(selected_fields),
        "page": page,
        "page_size": page_size,
        "count": len(page_rows),
        "pit": {
            "as_of": _serialize_as_of(parsed_as_of),
            "as_of_precision": parsed_as_of.kind,
            "market": market.upper(),
            "source": source,
            "coverage": [
                {
                    "symbol": symbol,
                    "coverage_id": item["coverage_id"],
                    "report_period_start": _coerce_date(item["report_period_start"]).isoformat(),
                    "report_period_end": _coerce_date(item["report_period_end"]).isoformat(),
                    "public_history_from_utc": _coerce_datetime(item["public_history_from_utc"])
                    .replace(tzinfo=timezone.utc)
                    .isoformat(),
                    "complete_through_utc": _coerce_datetime(item["complete_through_utc"])
                    .replace(tzinfo=timezone.utc)
                    .isoformat(),
                    "source_timezone": item["source_timezone"],
                    "evidence_uri": item["evidence_uri"],
                }
                for symbol, item in sorted(coverage_by_symbol.items())
            ],
        },
    }


def _prepare_batch(records: Sequence[object]) -> list[PreparedFinancialVersion]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise FinancialPitError("financial version batch must be a sequence")
    if len(records) > MAX_VERSION_BATCH:
        raise FinancialPitQueryTooBroad(f"financial version batch exceeds {MAX_VERSION_BATCH}")
    prepared = []
    for record in records:
        if not isinstance(record, FinancialVersionInput):
            raise FinancialPitError("every financial version must be FinancialVersionInput")
        version = prepare_financial_version(record)
        _require_registered_financial_source(
            str(version.values["domain"]),
            str(version.values["source"]),
            str(version.values["market"]),
        )
        prepared.append(version)
    return prepared


def _require_registered_financial_source(domain: str, source: str, market: str) -> None:
    """Require a runtime capability for the exact financial domain and market."""
    if not all(isinstance(value, str) and value for value in (domain, source, market)):
        raise FinancialPitUnsupported("PIT requires a registered financial source and market")
    from opendata.data.registry import get_registry

    registered = any(
        capability.domain == domain
        and capability.source == source
        and capability.market.upper() == market.upper()
        for capability in get_registry().capabilities()
    )
    if not registered:
        raise FinancialPitUnsupported(
            "PIT source/market is not a registered capability for the requested financial domain: "
            f"{domain}/{source}/{market}"
        )


def _insert_ignore(
    connection: Connection,
    table: str,
    columns: str,
    values: Mapping[str, object],
    *,
    conflict_column: str | None = None,
) -> bool:
    names = [item.strip() for item in columns.split(",")]
    if any(not name.isidentifier() for name in names) or not table.isidentifier():
        raise RuntimeError("internal PIT SQL identifier is invalid")
    if conflict_column is not None and (
        not isinstance(conflict_column, str)
        or not conflict_column
        or not conflict_column.isidentifier()
        or conflict_column not in names
    ):
        raise RuntimeError("internal PIT SQL conflict identifier is invalid")
    placeholders = ", ".join(f":{name}" for name in names)
    # Identifiers are fixed or validated with isidentifier above; caller data
    # remains in bound parameters.
    if connection.dialect.name == "mysql" and conflict_column:
        # B608: validated internal identifiers; values are bound.
        sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"  # noqa: S608 # nosec B608
        savepoint = connection.begin_nested()
        try:
            connection.execute(text(sql), dict(values))
        except IntegrityError as exc:
            savepoint.rollback()
            if _is_mysql_duplicate_key_error(exc):
                return False
            raise
        except BaseException:
            savepoint.rollback()
            raise
        else:
            savepoint.commit()
            # MySQL clients may enable FOUND_ROWS, so successful INSERT
            # classification must not depend on the reported rowcount.
            return True
    elif connection.dialect.name in {"sqlite", "postgresql"} and conflict_column:
        sql = (
            # B608: identifiers validated; conflict column is guarded.
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders}) "  # noqa: S608 # nosec B608
            f"ON CONFLICT ({conflict_column}) DO NOTHING"
        )
    else:
        # B608: validated internal identifiers; values are bound.
        sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"  # noqa: S608 # nosec B608
    result = connection.execute(text(sql), dict(values))
    return bool(result.rowcount)


def _is_mysql_duplicate_key_error(error: IntegrityError) -> bool:
    """Recognize only a driver's numeric MySQL duplicate-key error (1062)."""
    original = error.orig
    error_number = getattr(original, "errno", None)
    if error_number is None:
        original_args = getattr(original, "args", ())
        if original_args:
            error_number = original_args[0]
    return (
        isinstance(error_number, int)
        and not isinstance(error_number, bool)
        and error_number == 1062
    )


def _existing_financial_version(
    connection: Connection,
    version_id: str,
    *,
    current_read: bool = False,
) -> Mapping[str, object] | None:
    """Read an existing version, using a locking current read after MySQL races."""
    locking_clause = " FOR UPDATE" if current_read and connection.dialect.name == "mysql" else ""
    row = (
        connection.execute(
            text(
                # B608: lock clause is fixed; value is bound.
                "SELECT payload_digest, payload_json, provenance "  # nosec B608
                "FROM financial_pit_versions WHERE version_id = :version_id" + locking_clause
            ),
            {"version_id": version_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else _mapping_from_row(row)


def _mapping_from_row(row: RowMapping) -> dict[str, object]:
    """Materialize SQLAlchemy's untyped row mapping at the database boundary."""
    return {str(key): value for key, value in row.items()}


def _conflict_values(
    existing: Mapping[str, object], incoming: Mapping[str, object]
) -> dict[str, object]:
    conflict_identity = {
        "version_id": incoming["version_id"],
        "stored_digest": existing["payload_digest"],
        "incoming_digest": incoming["payload_digest"],
    }
    return {
        "conflict_id": _sha256(conflict_identity),
        "version_id": incoming["version_id"],
        "domain": incoming["domain"],
        "source": incoming["source"],
        "market": incoming["market"],
        "symbol": incoming["symbol"],
        "report_period": incoming["report_period"],
        "statement_type": incoming["statement_type"],
        "item": incoming["item"],
        "indicator": incoming["indicator"],
        "source_revision": incoming["source_revision"],
        "stored_digest": existing["payload_digest"],
        "incoming_digest": incoming["payload_digest"],
        "stored_payload_json": existing["payload_json"],
        "incoming_payload_json": incoming["payload_json"],
        "stored_provenance": existing["provenance"],
        "incoming_provenance": incoming["provenance"],
        "detected_at_utc": incoming["observed_at_utc"],
    }


def _require_pit_tables(connection: Connection) -> None:
    inspector = inspect(connection)
    missing = [
        table
        for table in (_VERSION_TABLE, _COVERAGE_TABLE, _CONFLICT_TABLE)
        if not inspector.has_table(table)
    ]
    if missing:
        raise FinancialPitUnsupported(f"PIT storage is not migrated; missing tables {missing}")


def _coverages_for_query(
    connection: Connection,
    *,
    domain: str,
    source: str,
    market: str,
    symbols: Sequence[str],
    period_start: date,
    period_end: date,
    as_of: ParsedAsOf,
) -> dict[str, Mapping[str, object]]:
    statement = text(
        "SELECT coverage_id, report_period_start, report_period_end, "
        "public_history_from_utc, complete_through_utc, source_timezone, evidence_uri "
        "FROM financial_pit_coverage WHERE domain = :domain AND source = :source "
        "AND market = :market AND symbol = :symbol "
        "AND report_period_start <= :period_start AND report_period_end >= :period_end "
        "ORDER BY public_history_from_utc DESC, complete_through_utc ASC, coverage_id ASC "
        "LIMIT :coverage_limit"
    )
    by_symbol: dict[str, Mapping[str, object]] = {}
    for symbol in symbols:
        rows = (
            connection.execute(
                statement,
                {
                    "domain": domain,
                    "source": source,
                    "market": market,
                    "symbol": symbol,
                    "period_start": period_start,
                    "period_end": period_end,
                    "coverage_limit": _MAX_PIT_COVERAGE_CANDIDATES + 1,
                },
            )
            .mappings()
            .all()
        )
        if len(rows) > _MAX_PIT_COVERAGE_CANDIDATES:
            raise FinancialPitQueryTooBroad(
                "PIT coverage candidates exceed "
                f"{_MAX_PIT_COVERAGE_CANDIDATES} for {domain}/{source}/{market}/{symbol}"
            )
        for row in rows:
            cutoff = _query_cutoff(as_of, str(row["source_timezone"]))
            if _coerce_datetime(
                row["public_history_from_utc"]
            ) <= cutoff and cutoff <= _coerce_datetime(row["complete_through_utc"]):
                by_symbol[symbol] = _mapping_from_row(row)
                break
        if symbol not in by_symbol:
            raise FinancialPitCoverageGap(
                f"PIT coverage is insufficient for {domain}/{source}/{market}/{symbol} "
                f"and report periods {period_start}..{period_end} at {_serialize_as_of(as_of)}"
            )
    return by_symbol


def _raise_for_conflicts(
    connection: Connection,
    *,
    domain: str,
    source: str,
    market: str,
    symbols: Sequence[str],
    period_start: date,
    period_end: date,
    cutoff_by_symbol: Mapping[str, datetime],
    coverage_by_symbol: Mapping[str, Mapping[str, object]],
) -> None:
    params: dict[str, object] = {
        "domain": domain,
        "source": source,
        "market": market,
        "period_start": period_start,
        "period_end": period_end,
        "conflict_limit": _MAX_PIT_COVERAGE_CANDIDATES + 1,
    }
    symbol_binds = []
    for index, symbol in enumerate(symbols):
        name = f"symbol_{index}"
        params[name] = symbol
        symbol_binds.append(f":{name}")
    rows = (
        connection.execute(
            text(
                # B608: indexed placeholders only; values are bound.
                "SELECT DISTINCT c.symbol, c.statement_type, c.item, c.indicator, "  # nosec B608
                "v.version_id AS linked_version_id, "
                "c.report_period, v.public_time_precision, v.visible_at_utc, v.source_timezone "
                "FROM financial_pit_conflicts AS c LEFT JOIN financial_pit_versions AS v "
                "ON v.version_id = c.version_id WHERE c.domain = :domain AND c.source = :source "
                "AND c.market = :market AND c.symbol IN ("
                + ", ".join(symbol_binds)
                + ") AND c.report_period >= :period_start AND c.report_period <= :period_end "
                "ORDER BY c.symbol, c.report_period, c.statement_type, c.item, c.indicator "
                "LIMIT :conflict_limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    if len(rows) > _MAX_PIT_COVERAGE_CANDIDATES:
        raise FinancialPitQueryTooBroad(
            f"PIT conflict set exceeds {_MAX_PIT_COVERAGE_CANDIDATES}; narrow the query"
        )
    affected = []
    for row in rows:
        symbol = str(row["symbol"])
        coverage = coverage_by_symbol.get(symbol)
        if coverage is None:
            raise FinancialPitUnsupported(
                "PIT conflict cannot be evaluated without matching source coverage"
            )
        if row["linked_version_id"] is None:
            raise FinancialPitUnsupported("PIT conflict has no linked source version")
        if row["public_time_precision"] == "unknown" or row["visible_at_utc"] is None:
            raise FinancialPitUnsupported(
                "PIT conflict visibility is unproven for a matching source revision"
            )
        if row["source_timezone"] != coverage["source_timezone"]:
            raise FinancialPitUnsupported(
                "conflicted version source_timezone differs from certified coverage"
            )
        cutoff = cutoff_by_symbol.get(symbol)
        if cutoff is None:
            raise FinancialPitUnsupported(
                "PIT conflict has no source-time cutoff for the requested symbol"
            )
        if _coerce_datetime(row["visible_at_utc"]) <= cutoff:
            affected.append(
                "|".join(
                    str(row[field] or "")
                    for field in ("symbol", "report_period", "statement_type", "item", "indicator")
                )
            )
    if affected:
        raise FinancialPitConflict(
            "PIT refuses source revisions with persisted content conflicts: "
            + ", ".join(affected[:32])
        )


def _candidate_versions(
    connection: Connection,
    *,
    domain: str,
    source: str,
    market: str,
    symbols: Sequence[str],
    period_start: date,
    period_end: date,
) -> list[Mapping[str, object]]:
    params: dict[str, object] = {
        "domain": domain,
        "source": source,
        "market": market,
        "period_start": period_start,
        "period_end": period_end,
        "candidate_limit": MAX_PIT_CANDIDATES + 1,
    }
    symbol_binds = []
    for index, symbol in enumerate(symbols):
        name = f"symbol_{index}"
        params[name] = symbol
        symbol_binds.append(f":{name}")
    rows = (
        connection.execute(
            text(
                # B608: indexed placeholders only; values are bound.
                "SELECT version_id, source, symbol, report_period, announce_date, "  # nosec B608
                "statement_type, item, indicator, revision_order, public_time_precision, "
                "visible_at_utc, source_timezone, observed_at_utc, payload_json "
                "FROM financial_pit_versions WHERE domain = :domain AND source = :source "
                "AND market = :market AND symbol IN ("
                + ", ".join(symbol_binds)
                + ") AND report_period >= :period_start AND report_period <= :period_end "
                "ORDER BY symbol, report_period, revision_order, visible_at_utc "
                "LIMIT :candidate_limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    if len(rows) > MAX_PIT_CANDIDATES:
        raise FinancialPitQueryTooBroad(
            f"PIT candidate set exceeds {MAX_PIT_CANDIDATES}; narrow symbols or report periods"
        )
    return [_mapping_from_row(row) for row in rows]


def _latest_versions(
    rows: Sequence[Mapping[str, object]], domain: str
) -> list[Mapping[str, object]]:
    groups: dict[tuple[object, ...], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        key: tuple[object, ...]
        if domain == "financial_statement":
            key = (
                row["source"],
                row["symbol"],
                row["report_period"],
                row["statement_type"],
                row["item"],
            )
        else:
            key = (row["source"], row["symbol"], row["report_period"], row["indicator"])
        groups[key].append(row)
    chosen = []
    for key, versions in groups.items():
        versions.sort(
            key=lambda row: (
                _coerce_datetime(row["visible_at_utc"]),
                _coerce_revision_order(row["revision_order"]),
                str(row["version_id"]),
            )
        )
        latest_order = (
            _coerce_datetime(versions[-1]["visible_at_utc"]),
            _coerce_revision_order(versions[-1]["revision_order"]),
        )
        tied = [
            item
            for item in versions
            if (
                _coerce_datetime(item["visible_at_utc"]),
                _coerce_revision_order(item["revision_order"]),
            )
            == latest_order
        ]
        if len({str(item["version_id"]) for item in tied}) > 1:
            raise FinancialPitConflict(
                f"PIT has ambiguous versions at the same publication rank: {key}"
            )
        chosen.append(versions[-1])
    return chosen


def _coerce_revision_order(value: object) -> int:
    """Keep Python's ``int`` conversion for database integer-like values."""
    if isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
        return int(value)
    type_name = type(value).__name__
    raise TypeError(
        f"int() argument must be a string, a bytes-like object or a real number, not '{type_name}'"
    )


def _project_row(row: Mapping[str, object], selected_fields: Sequence[str]) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise FinancialPitUnsupported("stored PIT payload is not valid normalized JSON") from exc
    payload["source"] = row["source"]
    payload["_merged_at"] = _coerce_datetime(row["observed_at_utc"])
    payload["_diff_flag"] = 0
    # Existing DWD _as_of is a merge trace stamp, not a publication fact.
    # Version queries leave it null and report the requested as_of separately.
    payload["_as_of"] = None
    return {field: payload.get(field) for field in selected_fields}


def _output_sort_key(row: Mapping[str, object], domain: str) -> tuple[str, ...]:
    if domain == "financial_statement":
        return (
            str(row.get("symbol")),
            str(row.get("report_period")),
            str(row.get("statement_type")),
            str(row.get("item")),
        )
    return (
        str(row.get("symbol")),
        str(row.get("report_period")),
        str(row.get("indicator")),
    )


def _validate_query(
    *,
    domain: str,
    source: str,
    market: str,
    symbols: Sequence[str],
    report_period_start: date,
    report_period_end: date,
    page: int,
    page_size: int,
    selected_fields: Sequence[str],
) -> None:
    if domain not in {"financial_statement", "financial_indicator"}:
        raise FinancialPitError("as_of is available only for financial domains")
    if not isinstance(source, str) or not source or source == "auto":
        raise FinancialPitUnsupported(
            "PIT requires an explicit source; auto has no version authority"
        )
    if not isinstance(market, str) or not market or len(market) > 32:
        raise FinancialPitError("PIT requires an explicit market")
    if not symbols or len(symbols) > MAX_PIT_SYMBOLS:
        raise FinancialPitQueryTooBroad(f"PIT requires 1-{MAX_PIT_SYMBOLS} explicit symbols")
    if any(not isinstance(symbol, str) or not symbol or len(symbol) > 128 for symbol in symbols):
        raise FinancialPitError("PIT symbols must be unique, non-empty and at most 128 characters")
    if len(set(symbols)) != len(symbols):
        raise FinancialPitError("PIT symbols must be unique, non-empty and at most 128 characters")
    if report_period_start > report_period_end:
        raise FinancialPitError("report-period start must not be after end")
    if (report_period_end - report_period_start).days > MAX_PIT_WINDOW_DAYS:
        raise FinancialPitQueryTooBroad(
            f"PIT report-period window exceeds {MAX_PIT_WINDOW_DAYS} days"
        )
    if (
        type(page) is not int
        or page < 1
        or type(page_size) is not int
        or not 1 <= page_size <= 1_000
    ):
        raise FinancialPitError("PIT page must be >=1 and page_size must be 1-1000")
    if (page - 1) * page_size > MAX_PIT_OFFSET:
        raise FinancialPitQueryTooBroad(f"PIT offset exceeds {MAX_PIT_OFFSET}")
    allowed = {
        "symbol",
        "report_period",
        "announce_date",
        "value",
        "revision",
        "source",
        "_merged_at",
        "_diff_flag",
        "_as_of",
    }
    if domain == "financial_statement":
        allowed.add("statement_type")
        allowed.add("item")
    else:
        allowed.add("indicator")
        allowed.add("unit")
    unknown = sorted(set(selected_fields) - allowed)
    if unknown:
        raise FinancialPitError(
            f"fields are unavailable in the versioned financial view: {unknown}"
        )


def _query_cutoff(parsed: ParsedAsOf, source_timezone: str) -> datetime:
    if parsed.kind == "timestamp":
        if not isinstance(parsed.value, datetime):
            raise FinancialPitError("timestamp selector has invalid parsed value")
        return parsed.value.astimezone(timezone.utc).replace(tzinfo=None)
    if not isinstance(parsed.value, date):
        raise FinancialPitError("date selector has invalid parsed value")
    return (
        datetime.combine(
            parsed.value,
            time.max,
            tzinfo=ZoneInfo(source_timezone),
        )
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    raise FinancialPitUnsupported(f"stored PIT timestamp has invalid type {type(value).__name__}")


def _coerce_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise FinancialPitUnsupported(f"stored PIT date has invalid type {type(value).__name__}")


def _serialize_as_of(parsed: ParsedAsOf) -> str:
    return parsed.value.isoformat()


def _parse_period(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FinancialPitError("period must be an ISO report-period date (YYYY-MM-DD)") from exc
