"""batch watermark for the WS data subscription.

Revision ID: 0004_batch_watermark
Revises: 0003_ods_ths
Create Date: 2026-09-24

Design §10.2: the subscription protocol is "meta first" - a client is
notified that a batch landed and pulls the rows over REST when it wants
them. That makes the notification itself the thing that must survive a
disconnect, so every finished batch is recorded here and a client that
reconnects with ``since_batch_id`` gets the missed events replayed.

``seq`` (auto-increment) is the replay order, not ``created_at``: a
burst of batches finishes inside the same millisecond and the replay
contract needs a total order without ties. The unique key on
``batch_id`` makes recording idempotent, so a retried shard does not
produce a second event.
"""

from alembic import op

revision = "0004_batch_watermark"
down_revision = "0003_ods_ths"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the batch watermark table."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `batch_watermark` (
          `seq` bigint NOT NULL AUTO_INCREMENT,
          `batch_id` char(36) NOT NULL,
          `domain` varchar(64) NOT NULL,
          `source` varchar(32) NOT NULL,
          `layer` varchar(8) NOT NULL,
          `window_start` date NULL,
          `window_end` date NULL,
          `rows_written` int NOT NULL DEFAULT 0,
          `created_at` datetime NOT NULL,
          PRIMARY KEY (`seq`),
          UNIQUE KEY `uq_batch_watermark_batch` (`batch_id`),
          KEY `ix_batch_watermark_domain_seq` (`domain`, `seq`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def downgrade() -> None:
    """Drop the batch watermark table."""
    op.execute("DROP TABLE IF EXISTS `batch_watermark`")
