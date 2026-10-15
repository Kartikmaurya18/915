"""Programmatic Alembic helpers (the CLI `alembic upgrade head` does the same)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from src.db.session import get_engine

ROOT = Path(__file__).resolve().parent.parent.parent


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False  # don't reconfigure the app's logging
    return cfg


def upgrade_to_head() -> None:
    command.upgrade(_config(), "head")


def downgrade_to_base() -> None:
    command.downgrade(_config(), "base")


def schema_status() -> tuple[bool, str]:
    """(is_at_head, human-readable detail). Never raises on an empty database."""
    head = ScriptDirectory.from_config(_config()).get_current_head()
    with get_engine().connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current == head, f"current={current} head={head}"
