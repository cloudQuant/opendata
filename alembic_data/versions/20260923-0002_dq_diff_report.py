"""dq diff report.

Revision ID: 0002_dq_diff_report
Revises: 0001_ods_dwd_p0
Create Date: 2026-09-23

Design §8.2: the cross-check writes aggregate counters plus sampled
difference details into the warehouse. One row per sampled
difference, keyed by ``(batch_id, biz_key, field)`` so re-running a
batch updates the same rows; the full detail is exported separately.
No partitioning: retention is a delete of old batches (design §8.5).
"""

from alembic import op

revision = "0002_dq_diff_report"
down_revision = "0001_ods_dwd_p0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the diff report table."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `dq_diff_report` (
          `batch_id` char(64) NOT NULL,
          `domain` varchar(64) NOT NULL,
          `source_a` varchar(32) NOT NULL,
          `source_b` varchar(32) NOT NULL,
          `biz_key` varchar(255) NOT NULL,
          `field` varchar(64) NOT NULL,
          `value_a` varchar(255) NULL,
          `value_b` varchar(255) NULL,
          `deviation` double NULL,
          `verdict` varchar(16) NOT NULL,
          `checked_at` datetime NOT NULL,
          PRIMARY KEY (`batch_id`, `biz_key`, `field`),
          KEY `ix_dq_diff_report_domain_checked` (`domain`, `checked_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def downgrade() -> None:
    """Drop the diff report table."""
    op.execute("DROP TABLE IF EXISTS `dq_diff_report`")
