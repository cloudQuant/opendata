"""Execute one fixed, bounded Fuyao financial capture in the reviewed child.

The handler consumes deployment configuration only from the process executor's
closed environment. It does not discover settings, read dotenv files, create a
grant, register itself with a scheduler, or claim that caller-declared policy,
unit, retention, or source-attempt values establish legal rights or source
authenticity. It returns a bounded prepared-capture DTO, never raw response
bytes, credentials, paths, headers, or exception text.

There is no supported production activation or configuration-loading path in
this slice. The sole-instrument synthetic/reviewed binding is not production
approval. Attempt association is a local trace match marked unverified, and
there is no durable prepared-capture intent or crash-recovery workflow; raw
files may remain orphaned if the later warehouse transaction fails.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import suppress
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote, urlsplit

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from opendata.data.providers.ths.models.financial_observation_fetcher import (
    ThsFinancialNativeUnitProfile,
    ThsFinancialObservationFetcher,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    TransportAttempt,
)
from opendata.data.source_policy import (
    SourcePolicyDocument,
    SourceUsePolicy,
    build_source_policy_context,
    parse_source_policy_document,
)
from opendata.pipeline.financial_prepared_codec import encode_prepared_capture
from opendata.pipeline.fuyao_financial_bundle import (
    FuyaoFinancialBundle,
    FuyaoFinancialBundleError,
    _read_atomic_regular_file,
    load_fuyao_financial_bundle,
)
from opendata.pipeline.fuyao_observation_capture import (
    FuyaoCaptureProvenance,
    FuyaoSourceAttemptDeclaration,
    prepare_fuyao_observation_capture,
)
from opendata.pipeline.process_executor import (
    FuyaoProcessDeploymentBinding,
    JsonValue,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        FinancialObservationFetchResult,
    )
    from opendata.pipeline.fuyao_observation_capture import PreparedFuyaoObservationCapture

_StatementType = Literal["income", "balance", "cashflow"]
_Periodicity = Literal["annual", "quarterly"]
_SOURCE = "ths"
_CANONICAL_MODEL = "financial_statement"
_OPERATION = RequestOperation.QUERY
_ATTEMPT_OPERATION = "query"
_RAW_DATASET_ID = "ths_financial_statement"
_INSTRUMENT_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z", re.ASCII)
_RUN_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_POLICY_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
MAX_FUYAO_CAPTURE_PAYLOAD_BYTES = 5 * 1024 * 1024
_MAX_QUERY_WINDOW_DAYS = 3650
_POLICY_TIMEOUT_SECONDS = 30.0
_ERROR = "fuyao_financial_process_failed"
_RESULT_FIELDS = frozenset(
    {
        "attempt_association_status",
        "authorization_status",
        "capture_manifest_sha256",
        "coverage_assessment",
        "financial_bundle_sha256",
        "instrument_id",
        "observation_ids",
        "observation_count",
        "periodicity",
        "prepared_capture_b64",
        "quarantined_row_count",
        "request_summary_sha256",
        "retention_class",
        "source_history_limit",
        "source_policy_sha256",
        "source_request_id_match_status",
        "statement_type",
        "unit_mapping_declaration_status",
        "unit_mapping_sha256_declared",
        "unit_mapping_version_declared",
    }
)

__all__ = [
    "FuyaoFinancialProcessError",
    "MAX_FUYAO_CAPTURE_PAYLOAD_BYTES",
    "execute_fuyao_financial_request",
]


class FuyaoFinancialProcessError(RuntimeError):
    """Fixed, content-free refusal raised by the reviewed Fuyao process."""

    def __init__(self) -> None:
        """Create an exception whose message never includes user or source data."""
        super().__init__(_ERROR)


def execute_fuyao_financial_request(parameters: Mapping[str, JsonValue]) -> JsonValue:
    """Fetch, raw-capture, and prepare one explicitly bounded financial query.

    Args:
        parameters: The exact seven-key business request validated again at the
            child boundary. It contains no credentials, grant, profile, path,
            source policy, raw-root, retention, or history controls.

    Returns:
        A fixed-schema result containing the canonical JSON representation of
        one ``PreparedCapture`` and non-sensitive summary fields. The parent
        must decode it before opening the warehouse transaction and persist it
        only through the same fenced SQL transaction as the run ledger.

    Raises:
        FuyaoFinancialProcessError: Any invalid deployment, authorization,
            transport, raw-evidence, or capture preparation state fails closed
            with a fixed message.
    """
    engine = None
    fetcher: ThsFinancialObservationFetcher | None = None
    try:
        request = _validate_parameters(parameters)
        binding = FuyaoProcessDeploymentBinding.from_child_environment()
        bundle = load_fuyao_financial_bundle(
            path=binding.financial_bundle_path,
            expected_sha256=binding.financial_bundle_sha256,
        )
        if bundle.instrument_id != request.instrument_id:
            raise FuyaoFinancialProcessError()

        source_policy, policy = _load_exact_policy(binding)
        if (
            policy.decision is not GrantDecision.ALLOWED
            or policy.conditions
            or urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname not in policy.allowed_hosts
        ):
            raise FuyaoFinancialProcessError()
        policy_context = build_source_policy_context(
            source_policy,
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=_OPERATION,
            principal_user_id=policy.principal_user_id,
            timeout=_POLICY_TIMEOUT_SECONDS,
        )

        # `mode=rw` prevents SQLite from silently creating an empty gate file
        # when deployment configuration points at a missing database. The
        # encoded path keeps URI metacharacters from changing SQLite options.
        gate_uri_path = quote(binding.gate_database_path, safe="/")
        _validate_existing_gate_database(binding.gate_database_path)
        engine = create_engine(
            URL.create(
                "sqlite+pysqlite",
                database=f"file:{gate_uri_path}",
                query={"mode": "rw", "uri": "true"},
            ),
            connect_args={"timeout": 1.0},
        )
        gate = SourceAttemptGate(engine)
        credentials = FuyaoCredentials(binding.api_key)
        fetcher = _build_fetcher(
            credentials=credentials,
            source_attempt_gate=gate,
            bundle=bundle,
        )
        source_attempt_binding = SourceAttemptBinding(
            grant_id=binding.source_attempt_grant_id,
            expected_revision=binding.source_attempt_expected_revision,
            principal_scope=binding.source_attempt_principal_scope,
            run_key=request.run_key,
        )
        with request_execution_scope(
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=_OPERATION,
            budget=policy_context.request_budget,
            source_attempt_binding=source_attempt_binding,
        ):
            fetch_result = fetcher.fetch(
                instrument_id=request.instrument_id,
                statement_type=request.statement_type,
                periodicity=request.periodicity,
                start_date=request.start_date,
                end_date=request.end_date,
                limit=request.limit,
            )

        capture = _prepare_capture(
            fetch_result,
            bundle=bundle,
            binding=binding,
            credentials=credentials,
            run_key=request.run_key,
        )
        prepared_payload = encode_prepared_capture(capture.prepared)
        if len(prepared_payload) > MAX_FUYAO_CAPTURE_PAYLOAD_BYTES:
            raise FuyaoFinancialProcessError()
        _reject_secret_echo(prepared_payload, binding.api_key, credentials.api_key)
        prepared_capture_b64 = base64.b64encode(prepared_payload).decode("ascii")
        observation_ids: list[JsonValue] = [
            observation_id for observation_id, _values in capture.prepared._observation_values
        ]

        result: dict[str, JsonValue] = {
            "attempt_association_status": capture.attempt_association_status,
            "authorization_status": "operator_policy_allowed_unverified",
            "capture_manifest_sha256": capture.manifest_sha256,
            "coverage_assessment": fetch_result.coverage_assessment,
            "financial_bundle_sha256": bundle.bundle_sha256,
            "instrument_id": request.instrument_id,
            "observation_ids": observation_ids,
            "observation_count": len(capture.prepared._observation_values),
            "periodicity": request.periodicity,
            "prepared_capture_b64": prepared_capture_b64,
            "quarantined_row_count": len(capture.prepared._quarantine_values),
            "request_summary_sha256": fetch_result.request_summary_sha256,
            "retention_class": binding.retention_class,
            "source_history_limit": binding.source_history_limit,
            "source_policy_sha256": binding.source_policy_sha256,
            "source_request_id_match_status": capture.source_request_id_match_status,
            "statement_type": request.statement_type,
            "unit_mapping_declaration_status": "operator_declared_unverified",
            "unit_mapping_sha256_declared": fetch_result.unit_mapping_sha256_declared,
            "unit_mapping_version_declared": fetch_result.unit_mapping_version_declared,
        }
        # Check the final, exact result schema and known secret encodings before
        # returning to the worker. The worker performs its own exact-secret
        # check before serializing or sending this value over IPC.
        if set(result) != _RESULT_FIELDS:
            raise FuyaoFinancialProcessError()
        try:
            result_bytes = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        except (TypeError, UnicodeEncodeError, ValueError):
            raise FuyaoFinancialProcessError() from None
        _reject_secret_echo(result_bytes, binding.api_key, credentials.api_key)
        return result
    except FuyaoFinancialProcessError:
        raise
    except Exception:
        # Never serialize exception strings: HTTP libraries, SQLAlchemy, paths,
        # and provider errors are outside the process result contract.
        raise FuyaoFinancialProcessError() from None
    finally:
        if fetcher is not None:
            with suppress(Exception):
                fetcher.close()
        if engine is not None:
            with suppress(Exception):
                engine.dispose()


def _build_fetcher(
    *,
    credentials: FuyaoCredentials,
    source_attempt_gate: SourceAttemptGate,
    bundle: FuyaoFinancialBundle,
) -> ThsFinancialObservationFetcher:
    """Construct the exact fixed fetcher from the pinned bundle and gate."""
    return ThsFinancialObservationFetcher(
        credentials=credentials,
        source_attempt_gate=source_attempt_gate,
        instrument_registry=bundle.instrument_registry,
        unit_profiles=bundle.unit_profiles,
    )


def _load_exact_policy(
    binding: FuyaoProcessDeploymentBinding,
) -> tuple[SourcePolicyDocument, SourceUsePolicy]:
    """Load a separately pinned policy and require one exact query identity."""
    if _POLICY_SHA256_PATTERN.fullmatch(binding.source_policy_sha256) is None:
        raise FuyaoFinancialProcessError()
    try:
        body = _read_atomic_regular_file(binding.source_policy_path)
    except FuyaoFinancialBundleError:
        raise FuyaoFinancialProcessError() from None
    actual_digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_digest, binding.source_policy_sha256):
        raise FuyaoFinancialProcessError()
    try:
        document = parse_source_policy_document(body)
        matching = tuple(
            policy
            for policy in document.policies
            if policy.source == _SOURCE
            and policy.canonical_model == _CANONICAL_MODEL
            and policy.operation is _OPERATION
        )
    except Exception:
        raise FuyaoFinancialProcessError() from None
    if len(matching) != 1:
        raise FuyaoFinancialProcessError()
    return document, matching[0]


def _prepare_capture(
    fetch_result: FinancialObservationFetchResult,
    *,
    bundle: FuyaoFinancialBundle,
    binding: FuyaoProcessDeploymentBinding,
    credentials: FuyaoCredentials,
    run_key: str,
) -> PreparedFuyaoObservationCapture:
    """Write and re-read the bounded raw response before transaction time."""
    # Do not persist a provider response that reflects the credential into
    # the raw evidence store, even if later result checks would reject it.
    _reject_secret_echo(fetch_result.raw_body, binding.api_key, credentials.api_key)
    if not fetch_result.attempt_trace:
        # The production fetcher has a required persistent gate; a successful
        # response without its local trace is unexpected and cannot be silently
        # downgraded to a caller-only declaration.
        raise FuyaoFinancialProcessError()
    trace = fetch_result.attempt_trace[-1]
    if (
        trace.gate_status != ReservationStatus.RESERVED_NEW.value
        or trace.request_budget_reserved is not True
        or type(trace.reserved_at_us) is not int
    ):
        raise FuyaoFinancialProcessError()
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    if not host:
        raise FuyaoFinancialProcessError()
    attempt = TransportAttempt(
        attempt_id=trace.attempt_id,
        request_id=trace.transport_request_id,
        attempt_no=trace.attempt_no,
        grant_id=binding.source_attempt_grant_id,
        expected_revision=binding.source_attempt_expected_revision,
        source=_SOURCE,
        canonical_model=_CANONICAL_MODEL,
        operation=_ATTEMPT_OPERATION,
        principal_scope=binding.source_attempt_principal_scope,
        host=host,
        run_key=run_key,
    )
    declaration = FuyaoSourceAttemptDeclaration(
        attempt=attempt,
        reservation=AttemptReservation(
            status=ReservationStatus.RESERVED_NEW,
            reserved_at_us=trace.reserved_at_us,
        ),
    )
    raw_source = RawResponseSource(
        provider_id=_SOURCE,
        dataset_id=_RAW_DATASET_ID,
        operation_id=f"{fetch_result.statement_type}_{fetch_result.periodicity}",
    )
    provenance = FuyaoCaptureProvenance(
        raw_source=raw_source,
        retention_class=binding.retention_class,
        source_history_limit=binding.source_history_limit,
    )
    profile = bundle.unit_profiles.get(fetch_result.statement_type)
    if type(profile) is not ThsFinancialNativeUnitProfile:
        raise FuyaoFinancialProcessError()
    writer = RawResponseArtifactWriter(binding.raw_artifact_root)
    try:
        return prepare_fuyao_observation_capture(
            fetch_result,
            unit_profile=profile,
            provenance=provenance,
            source_attempt_declaration=declaration,
            writer=writer,
        )
    except Exception:
        raise FuyaoFinancialProcessError() from None


def _validate_parameters(parameters: object) -> _FuyaoRequest:
    """Revalidate the seven-key process payload with exact primitive types."""
    expected = {
        "instrument_id",
        "statement_type",
        "periodicity",
        "start_date",
        "end_date",
        "limit",
        "run_key",
    }
    if type(parameters) is not dict or set(parameters) != expected:
        raise FuyaoFinancialProcessError()
    instrument_id = parameters["instrument_id"]
    statement_type = parameters["statement_type"]
    periodicity = parameters["periodicity"]
    start_text = parameters["start_date"]
    end_text = parameters["end_date"]
    limit = parameters["limit"]
    run_key = parameters["run_key"]
    if (
        type(instrument_id) is not str
        or _INSTRUMENT_PATTERN.fullmatch(instrument_id) is None
        or type(statement_type) is not str
        or statement_type not in {"income", "balance", "cashflow"}
        or type(periodicity) is not str
        or periodicity not in {"annual", "quarterly"}
        or type(run_key) is not str
        or _RUN_KEY_PATTERN.fullmatch(run_key) is None
        or (start_text is None) != (end_text is None)
    ):
        raise FuyaoFinancialProcessError()
    if start_text is None:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise FuyaoFinancialProcessError()
        start_date = None
        end_date = None
    else:
        if (
            type(start_text) is not str
            or type(end_text) is not str
            or _DATE_PATTERN.fullmatch(start_text) is None
            or _DATE_PATTERN.fullmatch(end_text) is None
            or limit is not None
        ):
            raise FuyaoFinancialProcessError()
        try:
            start_date = date.fromisoformat(start_text)
            end_date = date.fromisoformat(end_text)
        except ValueError:
            raise FuyaoFinancialProcessError() from None
        if (
            start_date.isoformat() != start_text
            or end_date.isoformat() != end_text
            or start_date > end_date
            or end_date - start_date > timedelta(days=_MAX_QUERY_WINDOW_DAYS)
        ):
            raise FuyaoFinancialProcessError()
    return _FuyaoRequest(
        instrument_id=instrument_id,
        statement_type=cast("_StatementType", statement_type),
        periodicity=cast("_Periodicity", periodicity),
        start_date=start_date,
        end_date=end_date,
        limit=cast("int | None", limit),
        run_key=run_key,
    )


def _secret_byte_forms(*values: str) -> tuple[bytes, ...]:
    """Return bounded common encodings of the explicit child credential."""
    candidates: set[bytes] = set()
    for value in values:
        encoded = value.encode("utf-8", errors="strict")
        if not encoded:
            continue
        standard_base64 = base64.b64encode(encoded)
        urlsafe_base64 = base64.urlsafe_b64encode(encoded)
        hexadecimal = encoded.hex().encode("ascii")
        candidates.add(encoded)
        candidates.update(
            {
                standard_base64,
                standard_base64.rstrip(b"="),
                urlsafe_base64,
                urlsafe_base64.rstrip(b"="),
                hexadecimal,
                hexadecimal.upper(),
            }
        )
    return tuple(candidates)


def _reject_secret_echo(payload: bytes, *api_keys: str) -> None:
    """Fail closed when a bounded payload echoes a common API-key encoding."""
    if type(payload) is not bytes:
        raise FuyaoFinancialProcessError()
    if any(secret in payload for secret in _secret_byte_forms(*api_keys)):
        raise FuyaoFinancialProcessError()


def _validate_existing_gate_database(path: str) -> None:
    """Require one existing non-symlink SQLite file without creating it."""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_flags) or not hasattr(os, "pread"):
        raise FuyaoFinancialProcessError()
    components = path.split("/")[1:]
    if (
        not path.startswith("/")
        or not components
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise FuyaoFinancialProcessError()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_fds.append(os.open(os.sep, directory_flags))
        for component in components[:-1]:
            directory_fds.append(os.open(component, directory_flags, dir_fd=directory_fds[-1]))
        parent_fd = directory_fds[-1]
        file_name = components[-1]
        named_before = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(named_before.st_mode):
            raise FuyaoFinancialProcessError()
        file_fd = os.open(file_name, file_flags, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 16
            or (opened.st_dev, opened.st_ino) != (named_before.st_dev, named_before.st_ino)
            or os.pread(file_fd, 16, 0) != b"SQLite format 3\x00"
        ):
            raise FuyaoFinancialProcessError()
        named_after = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        after = os.fstat(file_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ) or (named_after.st_dev, named_after.st_ino) != (opened.st_dev, opened.st_ino):
            raise FuyaoFinancialProcessError()
    except FuyaoFinancialProcessError:
        raise
    except OSError:
        raise FuyaoFinancialProcessError() from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


class _FuyaoRequest:
    """Small immutable-by-convention validated child request value."""

    __slots__ = (
        "instrument_id",
        "statement_type",
        "periodicity",
        "start_date",
        "end_date",
        "limit",
        "run_key",
    )

    def __init__(
        self,
        *,
        instrument_id: str,
        statement_type: _StatementType,
        periodicity: _Periodicity,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
        run_key: str,
    ) -> None:
        self.instrument_id = instrument_id
        self.statement_type = statement_type
        self.periodicity = periodicity
        self.start_date = start_date
        self.end_date = end_date
        self.limit = limit
        self.run_key = run_key
