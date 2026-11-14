from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    inspect,
    select,
)
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base
from app.db.models import ChapterVersion, SchedulerRun
from app.db.schema_migrations import upgrade_database


def _create_legacy_tables(engine) -> None:
    metadata = MetaData()
    scheduler_runs = Table(
        "scheduler_runs",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("cycle_number", Integer, nullable=False),
        Column("processed_count", Integer, nullable=False, default=0),
        Column("status", String(64), nullable=False),
        Column("outcomes", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    chapter_versions = Table(
        "chapter_versions",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("chapter_id", String(64), nullable=False),
        Column("source", String(64), nullable=False),
        Column("content", Text, nullable=True),
        Column("summary", Text, nullable=True),
        Column("review_score", Float, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    now = datetime(2026, 8, 26)
    with engine.begin() as connection:
        connection.execute(
            scheduler_runs.insert(),
            {
                "id": "run_old",
                "cycle_number": 1,
                "processed_count": 0,
                "status": "idle",
                "outcomes": [],
                "created_at": now,
            },
        )
        connection.execute(
            chapter_versions.insert(),
            {
                "id": "version_old",
                "chapter_id": "chapter_old",
                "source": "manual",
                "content": "legacy content",
                "summary": "legacy summary",
                "review_score": 80.0,
                "created_at": now,
            },
        )


def test_upgrade_database_adds_business_gap_schema_and_preserves_old_rows(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _create_legacy_tables(engine)

    pre_upgrade = inspect(engine)
    assert "reason_code" not in {
        column["name"] for column in pre_upgrade.get_columns("scheduler_runs")
    }
    assert "generation_metadata" not in {
        column["name"] for column in pre_upgrade.get_columns("chapter_versions")
    }
    upgrade_database(engine)

    inspector = inspect(engine)
    column_names = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in ("scheduler_runs", "chapter_versions")
    }
    assert {"reason_code", "duration_ms", "outcome_counts"} <= column_names[
        "scheduler_runs"
    ]
    assert "generation_metadata" in column_names["chapter_versions"]
    assert {"memory_embeddings", "trend_snapshots"} <= set(
        inspector.get_table_names()
    )

    with Session(engine) as session:
        old_run = session.get(SchedulerRun, "run_old")
        old_version = session.get(ChapterVersion, "version_old")
        assert old_run is not None
        assert old_run.reason_code == "unspecified"
        assert old_run.duration_ms == 0
        assert old_run.outcome_counts == {}
        assert old_version is not None
        assert old_version.generation_metadata == {}

    upgrade_database(engine)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SchedulerRun)) == 1
        assert session.scalar(select(func.count()).select_from(ChapterVersion)) == 1


def test_revision_uses_alembic_batch_operations_without_raw_sqlite_alter() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260826_01_business_gap_schema.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert "op.batch_alter_table" in source
    assert "ALTER TABLE" not in source.upper()


def test_application_upgrades_global_engine_before_session_and_clients(
    monkeypatch,
) -> None:
    import app.application as application_module
    from app.db import base as db_base

    events: list[str] = []
    sentinel_engine = create_engine("sqlite+pysqlite:///:memory:")

    class FakeClient:
        def close(self) -> None:
            pass

    # The global engine is created lazily in app.db.base; patch the cached default it hands out.
    monkeypatch.setattr(db_base, "_default_engine", sentinel_engine)
    monkeypatch.setattr(db_base, "_default_session_factory", None)
    monkeypatch.setattr(
        Base.metadata,
        "create_all",
        lambda *, bind: events.append(f"create_all:{bind is sentinel_engine}"),
    )
    monkeypatch.setattr(
        application_module,
        "upgrade_database",
        lambda engine: events.append(f"upgrade:{engine is sentinel_engine}"),
    )
    monkeypatch.setattr(
        application_module.OpenAIResponsesLLMClient,
        "from_settings",
        classmethod(
            lambda cls, settings, review=False: events.append("client") or FakeClient()
        ),
    )

    settings = SimpleNamespace(
        openai_api_key="",
        openai_base_url="",
        embedding_api_key=SecretStr(""),
        embedding_base_url="",
        embedding_fallback_mode="keyword",
        publish_mode="",
    )

    application_module.WriteAgentApplication.from_settings(
        settings,
        session_factory=lambda: events.append("session") or object(),
    )

    assert events[:3] == ["upgrade:True", "session", "client"]
    assert not any(event.startswith("create_all:") for event in events)


def test_queue_indexes_on_new_and_upgraded_databases(tmp_path) -> None:
    expected = {
        "chapters": [("status", "next_attempt_at"), ("book_id",)],
        "publish_jobs": [("status", "next_attempt_at"), ("chapter_id",)],
        "chapter_versions": [("chapter_id",)],
        "continuity_events": [("chapter_id",)],
        "memory_embeddings": [("book_id",)],
        "character_snapshots": [("chapter_id",), ("character_id",)],
        "story_arcs": [("book_id",)],
        "characters": [("book_id",)],
    }

    def assert_queue_indexes(engine) -> None:
        inspector = inspect(engine)
        for table, column_groups in expected.items():
            indexes = {
                index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(table)
            }
            for columns in column_groups:
                assert indexes[f"ix_{table}_{'_'.join(columns)}"] == columns

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'queue_indexes.db'}")
    Base.metadata.create_all(engine)
    assert_queue_indexes(engine)

    # Leave one index in place to exercise an older database with a partial index set.
    for table, column_groups in expected.items():
        names = {f"ix_{table}_{'_'.join(columns)}" for columns in column_groups}
        for index in Base.metadata.tables[table].indexes:
            if index.name in names and index.name != "ix_chapters_book_id":
                index.drop(engine)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = engine
    command.upgrade(config, "20261016_01")
    old_indexes = {index["name"] for index in inspect(engine).get_indexes("chapters")}
    assert "ix_chapters_book_id" in old_indexes
    assert "ix_chapters_status_next_attempt_at" not in old_indexes

    upgrade_database(engine)
    assert_queue_indexes(engine)
    upgrade_database(engine)
    assert_queue_indexes(engine)
    engine.dispose()


@pytest.mark.parametrize("previous_schema", [False, True], ids=["empty", "partial"])
def test_base_tables_upgrade_matches_metadata_and_is_repeatable(tmp_path, previous_schema):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'base_tables.db'}")
    try:
        if previous_schema:
            books = Base.metadata.tables["books"]
            chapters = Base.metadata.tables["chapters"]
            books.create(engine)
            chapters.create(engine)
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = engine
            command.upgrade(config, "20261113_01")
            index = next(i for i in chapters.indexes if i.name == "ix_chapters_book_id")
            index.drop(engine)
            with engine.begin() as connection:
                connection.execute(books.insert().values(
                    id="legacy_book", title="Legacy", platform="local", genre="fiction"
                ))
                connection.execute(chapters.insert().values(
                    id="legacy_chapter", book_id="legacy_book", title="Opening", chapter_number=1
                ))

        for _ in range(2):
            upgrade_database(engine)
            with engine.connect() as connection:
                context = MigrationContext.configure(connection, opts={"compare_type": True})
                assert compare_metadata(context, Base.metadata) == []
                if previous_schema:
                    assert connection.execute(select(books.c.title)).scalar_one() == "Legacy"
                    assert connection.execute(select(chapters.c.book_id)).scalar_one() == "legacy_book"
    finally:
        engine.dispose()
