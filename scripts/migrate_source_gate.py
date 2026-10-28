"""Explicitly upgrade one existing local source-attempt gate SQLite file."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config

from alembic import command

if TYPE_CHECKING:
    from collections.abc import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "alembic_source_gate.ini"


def build_parser() -> argparse.ArgumentParser:
    """Build the migration command parser with a mandatory local target."""
    parser = argparse.ArgumentParser(
        description=(
            "Adopt and upgrade an existing file-backed SQLite source-attempt gate. "
            "The command never creates a database or migrates warehouse tables."
        )
    )
    parser.add_argument(
        "--database-path",
        required=True,
        type=Path,
        help="absolute path to the existing local source-gate SQLite file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone Alembic chain against the explicitly named file."""
    arguments = build_parser().parse_args(argv)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    path_contract = importlib.import_module("alembic_source_gate.path_contract")
    database_path, file_identity = path_contract.inspect_existing_database_path(
        arguments.database_path
    )
    config = Config(str(CONFIG_PATH))
    config.attributes["source_gate_path"] = database_path
    config.attributes["source_gate_file_identity"] = file_identity
    command.upgrade(config, "head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
