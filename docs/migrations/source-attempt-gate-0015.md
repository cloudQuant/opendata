# Source attempt gate revision 0015

Revision `0015_source_attempt_gate` retains its original ID and parent
`0014_sina_fzb_slot_reconciliation`. It creates the source-attempt reservation,
grant, policy, and quota tables only on SQLite, where the current gate runtime
is supported.

On MySQL 8.0.16 through 8.x, both `upgrade` and `downgrade` are explicit no-ops.
Alembic still advances or rewinds `alembic_version_data`; revision 0015 itself
issues no SQL and creates no gate tables, triggers, indexes, or rows on MySQL.
This keeps the warehouse schema and existing business data unchanged. It does
not make the source-attempt gate available to MySQL callers.

The revision fails closed for MySQL versions below 8.0.16, MySQL 9.x, MariaDB,
unknown MySQL versions, and all other dialects. Those targets are unsupported
or unverified. In particular, no compatibility claim is made for MySQL 9.x.
The minimum matches the warehouse migration chain's existing CHECK-constraint
requirement.

## Scoped MySQL 8.4 acceptance

A one-time online acceptance completed on an isolated MySQL 8.4 instance using
the Unix socket transport only; TCP was not tested. It used a synthetic
`0013_sina_fzb_fanout_groups` version marker as its starting boundary;
migrations `0001` through `0013` were not executed in this run. The actual
Alembic upgrade from that marker through `0015_source_attempt_gate` completed.
The check confirmed the full revision value was recorded, the 0014 tables and
expected index existed, the `VARCHAR(32)` marker was widened to `VARCHAR(64)`,
the InnoDB sentinel row remained unchanged, and 0015 created no source-gate
tables or other schema objects on MySQL. Downgrade to 0014 preserved the full
33-character marker and width 64; the subsequent actual 0014 downgrade to
0013 ran only with empty 0014 tables and left the width at 64.

The isolated regression run reported 33 tests passed. The recorded coverage
was 0014: 87%, 0015: 100%. These results cover the selected migration tests and
the synthetic 0013 boundary only. They do not establish execution of the full
migration chain, production endpoint behavior, downgrade of nonempty 0014
tables, or compatibility with MySQL 9.x. Evidence is retained under
`/home/yun/Documents/opendata/var/reports/afi204/2026-10-28-pending-warehouse-revision/mysql-8.4-scoped/`.

The offline regression tests use a real temporary SQLite database to pin the
existing gate tables/triggers and preservation of unrelated data. Simulated
dialect tests verify MySQL 8 no-op behavior and fail-closed handling without
opening a MySQL connection. The one-time MySQL 8.4 run above additionally
confirms the scoped real Alembic version-row and no-op behavior, but does not
broaden the supported target beyond MySQL 8.x.
