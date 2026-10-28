"""Schema for immutable publication-event receipt copies in the control database.

This table records only that a bounded event copy was persisted by a receiver.
Producer identity and contract evidence remain explicitly caller-provided and
unverified. The schema does not represent authorization, data readiness, source
availability, or a cross-database delivery acknowledgement.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import VARCHAR

__all__ = ["METADATA_PUBLICATION_RECEIPTS"]

_MAX_EVENT_BYTES = 8_192
_MAX_EVENT_VERSION = 1
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_RECORD_COUNT = 2_147_483_647
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_PRODUCER_EVIDENCE_KIND = "caller_supplied_external_pin_unverified"
_CONTRACT_EVIDENCE_ABSENT = "absent"
_CONTRACT_EVIDENCE_CALLER_SUPPLIED = "caller_supplied_unverified"

_BINARY_32 = String(length=32).with_variant(
    VARCHAR(length=32, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_64 = String(length=64).with_variant(
    VARCHAR(length=64, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_128 = String(length=128).with_variant(
    VARCHAR(length=128, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_40 = String(length=40).with_variant(
    VARCHAR(length=40, collation="utf8mb4_0900_bin"), "mysql"
)

METADATA_PUBLICATION_RECEIPTS = Table(
    "od_metadata_publication_receipts",
    MetaData(),
    Column("producer_identity", _BINARY_128, nullable=False),
    Column("producer_identity_evidence_kind", _BINARY_40, nullable=False),
    Column("publication_id", _BINARY_32, nullable=False),
    Column("dataset_id", _BINARY_128, nullable=False),
    Column("publication_sequence", BigInteger, nullable=False),
    Column("record_count", BigInteger, nullable=False),
    Column("payload_sha256", _BINARY_64, nullable=False),
    Column("contract_sha256", _BINARY_64, nullable=True),
    Column("contract_evidence_kind", _BINARY_40, nullable=False),
    Column("provenance_sha256", _BINARY_64, nullable=False),
    Column("schema_version", _BINARY_64, nullable=False),
    Column("event_version", Integer, nullable=False),
    Column(
        "event_json_utf8",
        LargeBinary(length=_MAX_EVENT_BYTES),
        nullable=False,
    ),
    Column("event_sha256", _BINARY_64, nullable=False),
    Column("published_at_us", BigInteger, nullable=False),
    Column("received_at_us", BigInteger, nullable=False),
    PrimaryKeyConstraint(
        "producer_identity",
        "publication_id",
        name="pk_od_metadata_publication_receipt",
    ),
    UniqueConstraint(
        "producer_identity",
        "dataset_id",
        "publication_sequence",
        name="uq_od_metadata_receipt_source_sequence",
    ),
    CheckConstraint(
        "length(producer_identity) BETWEEN 1 AND 128",
        name="ck_od_meta_receipt_producer_len",
    ),
    CheckConstraint(
        "producer_identity_evidence_kind = 'caller_supplied_external_pin_unverified'",
        name="ck_od_meta_receipt_producer_evidence",
    ),
    CheckConstraint("length(publication_id) = 32", name="ck_od_meta_receipt_pub_id_len"),
    CheckConstraint("length(dataset_id) BETWEEN 1 AND 128", name="ck_od_meta_receipt_dataset_len"),
    CheckConstraint("publication_sequence > 0", name="ck_od_meta_receipt_sequence"),
    CheckConstraint(
        f"record_count BETWEEN 0 AND {_MAX_RECORD_COUNT}",
        name="ck_od_meta_receipt_record_count",
    ),
    CheckConstraint("length(payload_sha256) = 64", name="ck_od_meta_receipt_payload_sha"),
    CheckConstraint(
        "contract_sha256 IS NULL OR length(contract_sha256) = 64",
        name="ck_od_meta_receipt_contract_sha",
    ),
    CheckConstraint(
        "(contract_evidence_kind = 'absent' AND contract_sha256 IS NULL) OR "
        "(contract_evidence_kind = 'caller_supplied_unverified' "
        "AND contract_sha256 IS NOT NULL)",
        name="ck_od_meta_receipt_contract_evidence",
    ),
    CheckConstraint("length(provenance_sha256) = 64", name="ck_od_meta_receipt_provenance_sha"),
    CheckConstraint("length(schema_version) BETWEEN 1 AND 64", name="ck_od_meta_receipt_schema"),
    CheckConstraint(
        f"event_version = {_MAX_EVENT_VERSION}",
        name="ck_od_meta_receipt_event_version",
    ),
    CheckConstraint(
        f"length(event_json_utf8) BETWEEN 2 AND {_MAX_EVENT_BYTES}",
        name="ck_od_meta_receipt_event_size",
    ),
    CheckConstraint("length(event_sha256) = 64", name="ck_od_meta_receipt_event_sha"),
    CheckConstraint(
        f"published_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
        name="ck_od_meta_receipt_published_at",
    ),
    CheckConstraint(
        f"received_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
        name="ck_od_meta_receipt_received_at",
    ),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)
