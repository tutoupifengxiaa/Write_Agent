from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import Book, Chapter
from app.orchestrator.task_claims import ready_task, utc_now


@pytest.fixture
def setup():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add(Book(id="book", title="B", platform="fanqie", genre="urban"))
        session.flush()
        session.add_all([
            Chapter(id="failed", book_id="book", chapter_number=1, title="1", status="generation_failed",
                    attempt_count=3, error_message="generation attempts exhausted",
                    next_attempt_at=utc_now() + timedelta(hours=1)),
            Chapter(id="claimed", book_id="book", chapter_number=2, title="2", status="generation_failed",
                    claim_token="worker-token", lease_expires_at=utc_now() + timedelta(minutes=5)),
            Chapter(id="done", book_id="book", chapter_number=3, title="3", status="persisted"),
        ])
        session.commit()
    app = create_fastapi_app(initialize_database=False)
    app.state.session_factory = factory
    return TestClient(app), factory


def test_failed_chapter_is_requeued_with_fresh_retry_budget(setup):
    client, factory = setup
    response = client.post("/chapters/failed/requeue-generation")
    assert response.status_code == 202
    assert response.json() == {"chapter_id": "failed", "status": "draft_planned"}
    with factory() as session:
        chapter = session.get(Chapter, "failed")
        assert (chapter.status, chapter.attempt_count, chapter.error_message, chapter.next_attempt_at) == (
            "draft_planned", 0, None, None)
        # The worker's own readiness filter must pick it up again.
        ready = session.scalars(select(Chapter.id).where(ready_task(Chapter, "draft_planned", utc_now()))).all()
        assert ready == ["failed"]


@pytest.mark.parametrize(("chapter_id", "status_code"), [("claimed", 409), ("done", 409), ("missing", 404)])
def test_only_unclaimed_failed_chapters_can_be_requeued(setup, chapter_id, status_code):
    client, factory = setup
    assert client.post(f"/chapters/{chapter_id}/requeue-generation").status_code == status_code
    with factory() as session:
        statuses = dict(session.execute(select(Chapter.id, Chapter.status)).all())
    assert statuses == {"failed": "generation_failed", "claimed": "generation_failed", "done": "persisted"}


def test_chapter_list_exposes_failure_reason(setup):
    client, _ = setup
    chapters = {item["id"]: item for item in client.get("/books/book/chapters").json()}
    assert chapters["failed"]["error_message"] == "generation attempts exhausted"
    assert chapters["done"]["error_message"] is None
