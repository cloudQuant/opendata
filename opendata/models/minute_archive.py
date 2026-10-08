"""Main-database index for immutable local minute-data shards."""

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Date, DateTime, Index, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base

_TIMESTAMP_TYPE = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class MinuteArchiveShard(Base):
    """Metadata pointer to one symbol/source/period/day Parquet shard.

    Attributes:
        domain: Registered Bar domain.
        symbol: Validated market symbol.
        source: Explicit import/source identifier.
        period: Minute bar interval.
        day: UTC calendar day partition.
        relative_path: POSIX path beneath the configured archive root.
        row_count: Number of records in the immutable Parquet file.
        min_timestamp: Earliest UTC record timestamp.
        max_timestamp: Latest UTC record timestamp.
        sha256: Content digest checked before query reads.
        created_at: First metadata publication time.
        updated_at: Latest metadata publication time.
    """

    __tablename__ = "minute_archive_shards"
    __table_args__ = (  # type: ignore[assignment]  # Declarative accepts tuple constraints/indexes
        Index(
            "ix_minute_archive_lookup",
            "domain",
            "source",
            "period",
            "day",
            "symbol",
        ),
    )

    domain: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[str] = mapped_column(String(4), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_timestamp: Mapped[datetime] = mapped_column(_TIMESTAMP_TYPE, nullable=False)
    max_timestamp: Mapped[datetime] = mapped_column(_TIMESTAMP_TYPE, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
