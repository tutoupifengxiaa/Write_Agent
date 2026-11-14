from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import Base
from app.db.base import engine as db_engine
from app.db.models import Book, Chapter
from app.db.repositories import (
    ChapterRepository,
    MemoryRepository,
    PublishJobRepository,
    SchedulerRunRepository,
    TrendSnapshotRepository,
)


def test_engine_uses_settings_database_url():
    matches_settings = db_engine.url == make_url(settings.database_url)
    assert matches_settings, "Engine URL differs from the configured database URL"


def test_file_sqlite_configures_each_connection_and_keeps_memory_journal(tmp_path):
    from types import SimpleNamespace

    from app.db.base import create_engine_from_settings

    file_settings = SimpleNamespace(database_url=f"sqlite+pysqlite:///{tmp_path / 'pragma.db'}")
    engine = create_engine_from_settings(file_settings)
    try:
        with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()

    memory = create_engine_from_settings(SimpleNamespace(database_url="sqlite+pysqlite:///:memory:"))
    try:
        with memory.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "memory"
    finally:
        memory.dispose()


def test_create_chapter_and_set_current_version():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        book = Book(id="book_001", title="Test", platform="fanqie", genre="urban")
        session.add(book)
        session.commit()

        repo = ChapterRepository(session)
        chapter = repo.create_planned_chapter(
            book_id="book_001",
            chapter_number=1,
            title="Chapter 1",
            goal={"objective": "Opening"},
        )

        assert isinstance(chapter, Chapter)
        assert chapter.status == "draft_planned"
        assert chapter.current_version_id is None


def test_create_planned_chapter_does_not_commit_transaction():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        book = Book(id="book_001", title="Test", platform="fanqie", genre="urban")
        session.add(book)
        session.commit()

        repo = ChapterRepository(session)
        chapter = repo.create_planned_chapter(
            book_id="book_001",
            chapter_number=1,
            title="Chapter 1",
            goal={"objective": "Opening"},
        )

        found_before_rollback = session.scalar(
            select(Chapter).where(Chapter.id == chapter.id)
        )
        assert found_before_rollback is not None

        session.rollback()

        found_after_rollback = session.scalar(
            select(Chapter).where(Chapter.id == chapter.id)
        )
        assert found_after_rollback is None


def test_create_and_list_publish_jobs():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.flush()
        chapter = Chapter(
            book_id="book_001",
            chapter_number=1,
            title="One",
            goal={"objective": "Start"},
            status="persisted",
        )
        session.add(chapter)
        session.commit()

        repo = PublishJobRepository(session)
        repo.create_job(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
        )

        jobs = repo.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].status == "publish_pending"


def test_publish_job_repository_supports_status_filter():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.flush()
        chapter = Chapter(
            book_id="book_001",
            chapter_number=1,
            title="One",
            goal={"objective": "Start"},
            status="persisted",
        )
        session.add(chapter)
        session.commit()

        repo = PublishJobRepository(session)
        repo.create_job(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_failed",
            error_message="fatal publish crash",
        )
        repo.create_job(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="draft_uploaded",
        )
        session.commit()

        jobs = repo.list_jobs(status="publish_failed")

        assert len(jobs) == 1
        assert jobs[0].status == "publish_failed"


def test_memory_repository_returns_story_arcs_and_character_snapshots():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        book = Book(id="book_001", title="Test", platform="fanqie", genre="urban")
        session.add(book)
        session.flush()
        chapter = Chapter(
            id="ch_0001",
            book_id="book_001",
            chapter_number=1,
            title="One",
            goal={"objective": "Start"},
            status="persisted",
        )
        session.add(chapter)

        from app.db.models import Character, CharacterSnapshot, StoryArc

        session.add(
            StoryArc(
                book_id="book_001",
                arc_type="main",
                chapter_start=1,
                chapter_end=20,
                stage_goal="hero rises",
                outline={"beats": ["setup", "conflict"]},
            )
        )
        character = Character(
            book_id="book_001",
            name="Lin Fan",
            role="lead",
            age=18,
            profile={"trait": "calm"},
            last_updated_chapter_number=1,
        )
        session.add(character)
        session.flush()
        session.add(
            CharacterSnapshot(
                character_id=character.id,
                chapter_id="ch_0001",
                state={"mood": "focused"},
            )
        )
        session.commit()

        repo = MemoryRepository(session)
        arcs = repo.list_story_arcs("book_001")
        snapshots = repo.list_latest_character_states("book_001")

        assert len(arcs) == 1
        assert arcs[0].stage_goal == "hero rises"
        assert len(snapshots) == 1
        assert snapshots[0]["name"] == "Lin Fan"
        assert snapshots[0]["state"]["mood"] == "focused"


def test_create_and_query_scheduler_runs():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repo = SchedulerRunRepository(session)
        run = repo.create_run(
            cycle_number=1,
            processed_count=2,
            status="processed",
            outcomes=[{"final_status": "persisted", "item_type": "chapter", "item_id": "chapter_1"}],
        )
        session.commit()

        runs = repo.list_runs()
        fetched = repo.get_run(run.id)

        assert len(runs) == 1
        assert runs[0].id == run.id
        assert runs[0].processed_count == 2
        assert fetched is not None
        assert fetched.status == "processed"


def test_scheduler_run_persists_reason_and_duration():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repo = SchedulerRunRepository(session)
        run = repo.create_run(
            cycle_number=1,
            processed_count=0,
            status="idle",
            outcomes=[],
            outcome_counts={"idle": 1},
            reason_code="no_pending_work",
            duration_ms=17,
        )
        session.commit()

        fetched = repo.get_run(run.id)

        assert fetched is not None
        assert fetched.reason_code == "no_pending_work"
        assert fetched.duration_ms == 17
        assert fetched.outcome_counts == {"idle": 1}


def test_scheduler_run_repository_supports_status_filter_and_paging():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repo = SchedulerRunRepository(session)
        repo.create_run(cycle_number=1, processed_count=1, status="processed", outcomes=[])
        repo.create_run(cycle_number=2, processed_count=0, status="idle", outcomes=[])
        repo.create_run(cycle_number=3, processed_count=2, status="processed", outcomes=[])
        repo.create_run(
            cycle_number=0,
            processed_count=1,
            status="attention_required",
            outcomes=[],
        )
        repo.create_run(cycle_number=-1, processed_count=0, status="failed", outcomes=[])
        session.commit()

        filtered = repo.list_runs(status="processed")
        paged = repo.list_runs(limit=1, offset=1)
        summary = repo.get_summary()

        assert [run.cycle_number for run in filtered] == [3, 1]
        assert len(paged) == 1
        assert paged[0].cycle_number == 2
        assert summary["total_runs"] == 5
        assert summary["processed_runs"] == 2
        assert summary["idle_runs"] == 1
        assert summary["attention_required_runs"] == 1
        assert summary["failed_runs"] == 1


def test_create_and_query_trend_snapshots():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repo = TrendSnapshotRepository(session)
        first = repo.create_snapshot(
            capture_id="capture_1",
            provider="fanqie",
            source_url="https://fanqienovel.com/rank",
            status="real",
            items=[{"id": "fanqie:1", "provider": "fanqie", "rank": 1, "title": "逆风翻盘"}],
            error_message=None,
        )
        repo.create_snapshot(
            capture_id="capture_2",
            provider="qimao",
            source_url="https://www.qimao.com/paihang",
            status="failed",
            items=[],
            error_message="unavailable",
        )
        session.commit()

        fetched = repo.get_snapshot(first.id)

        assert fetched is not None
        assert fetched.items[0]["title"] == "逆风翻盘"
        assert [row.id for row in repo.list_snapshots(provider="fanqie")] == [first.id]
        assert [row.id for row in repo.list_snapshots(capture_id="capture_1")] == [first.id]


def test_failure_clusters_apply_default_limit_in_sql():
    from sqlalchemy import event

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    queries = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_query(connection, cursor, statement, parameters, context, executemany):
        if "FROM scheduler_runs" in statement:
            queries.append((statement.upper(), parameters))

    with Session(engine) as session:
        assert SchedulerRunRepository(session).get_failure_clusters() == []

    assert len(queries) == 1
    statement, parameters = queries[0]
    assert "LIMIT ?" in statement
    assert parameters[0] == 500


def test_failure_clusters_use_recent_runs_across_worker_restarts():
    from datetime import datetime

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = SchedulerRunRepository(session)
        for minute, cycle, error, item in [
            (1, 88, "network", "old_network"),
            (2, 89, "platform", "old_platform"),
            (3, 90, "platform", "latest_platform"),
            (4, 1, "network", "latest_network"),
        ]:
            run = repo.create_run(
                cycle_number=cycle,
                processed_count=1,
                status="failed",
                outcomes=[{
                    "item_type": "publish_job",
                    "final_status": "publish_failed",
                    "error_type": error,
                    "item_id": item,
                }],
            )
            run.created_at = datetime(2026, 1, 1, 8, minute)
        session.commit()

        clusters = repo.get_failure_clusters()
        assert [(cluster["error_type"], cluster["count"]) for cluster in clusters] == [
            ("network", 2), ("platform", 2),
        ]
        assert clusters[0]["latest_cycle_number"] == 1
        assert clusters[0]["sample_item_id"] == "latest_network"
        assert datetime.fromisoformat(clusters[0]["latest_at"]) == datetime(2026, 1, 1, 8, 4)

        recent = repo.get_failure_clusters(limit=2)
        assert [(cluster["sample_item_id"], cluster["count"]) for cluster in recent] == [
            ("latest_network", 1), ("latest_platform", 1),
        ]
