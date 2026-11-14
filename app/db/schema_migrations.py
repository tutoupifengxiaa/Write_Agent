from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from alembic import command


def upgrade_database(engine: Engine | Connection) -> None:
    config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    config = Config(str(config_path))
    config.attributes["connection"] = engine
    command.upgrade(config, "head")

