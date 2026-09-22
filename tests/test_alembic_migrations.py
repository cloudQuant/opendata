"""Structural tests for the Alembic revision graph.

A broken chain (a dangling ``down_revision`` or more than one head) makes
``alembic upgrade head`` fail at runtime, and that surfaces far too late: the
application historically created its tables directly through
``Base.metadata.create_all``, so nobody noticed the migrations were unusable.

These tests validate the graph itself, which needs no database and fails fast.
"""

import importlib.util
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def load_migrations() -> dict[str, tuple[str | None, str | None]]:
    """Return ``{filename: (revision, down_revision)}`` for every migration."""
    migrations: dict[str, tuple[str | None, str | None]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"alembic_migration_{path.stem}", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revision = getattr(module, "revision", None)
        down_revision = getattr(module, "down_revision", None)
        migrations[path.name] = (revision, down_revision)
    return migrations


class TestMigrationGraph:
    """The revision graph must be resolvable and have a single head."""

    def test_migration_scripts_exist(self) -> None:
        """Guard against the directory being emptied or renamed."""
        assert load_migrations(), f"no migration scripts found under {VERSIONS_DIR}"

    def test_revision_ids_are_unique(self) -> None:
        """Duplicate ids would make the graph ambiguous."""
        revisions = [revision for revision, _ in load_migrations().values()]
        duplicates = {rev for rev in revisions if revisions.count(rev) > 1}
        assert duplicates == set(), f"duplicate revision ids: {sorted(duplicates)}"

    def test_every_down_revision_resolves(self) -> None:
        """Every parent id must exist, otherwise alembic raises KeyError."""
        migrations = load_migrations()
        known = {revision for revision, _ in migrations.values()}
        dangling = {
            name: down
            for name, (_, down) in migrations.items()
            if down is not None and down not in known
        }
        assert dangling == {}, (
            f"dangling down_revision {dangling}; known revisions: {sorted(known)}"
        )

    def test_has_exactly_one_head(self) -> None:
        """More than one head means the chain is split and `upgrade head` is ambiguous."""
        migrations = load_migrations()
        known = {revision for revision, _ in migrations.values()}
        referenced = {down for _, down in migrations.values() if down is not None}
        heads = sorted(known - referenced)
        assert len(heads) == 1, f"expected exactly one head, found {heads}"

    def test_chain_walks_from_base_to_head(self) -> None:
        """Walking parents from the head must reach the base revision."""
        migrations = load_migrations()
        parents = dict(migrations.values())
        referenced = {down for _, down in migrations.values() if down is not None}
        heads = [rev for rev in parents if rev not in referenced]
        assert len(heads) == 1

        seen: list[str | None] = []
        current: str | None = heads[0]
        while current is not None:
            assert current not in seen, f"cycle detected at {current}: {seen}"
            seen.append(current)
            current = parents[current]

        assert seen[0] == heads[0]
        assert seen[-1] is not None
        assert parents[seen[-1]] is None, "chain must terminate at a base revision"
