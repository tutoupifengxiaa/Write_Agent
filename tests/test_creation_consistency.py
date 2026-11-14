import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes_books import get_session
from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import Book, Chapter, ChapterVersion, ContinuityEvent, PublishJob


@pytest.fixture
def workspace():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Book(id="book", title="Book", platform="fanqie", genre="urban"))
        session.flush()
        session.add(Chapter(id="chapter", book_id="book", chapter_number=1, title="Old title", status="persisted", current_version_id="old"))
        session.flush()
        session.add(ChapterVersion(id="old", chapter_id="chapter", content="old content", summary="old summary"))
        session.commit()
    app = create_fastapi_app()
    def session_dependency():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = session_dependency
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_review_saves_current_candidate_without_approving_or_publishing(workspace):
    client, engine = workspace
    delta = {"relationship": "advanced"}
    response = client.post("/api/creation/review", json={"chapter_id": "chapter", "review_action": "review_pending", "content": "edited content", "summary": "actual summary", "state_deltas": [delta], "title": "Edited title", "note": "check continuity"})
    assert response.status_code == 200
    with Session(engine) as session:
        chapter = session.get(Chapter, "chapter")
        candidate = session.get(ChapterVersion, chapter.current_version_id)
        assert candidate.content == "edited content"
        assert candidate.summary == "actual summary"
        assert candidate.source == "review_candidate"
        assert chapter.title == "Edited title"
        assert chapter.status == "review_pending"
        assert session.scalar(select(func.count()).select_from(ContinuityEvent)) == 0
        assert session.scalar(select(func.count()).select_from(PublishJob)) == 0
    assert client.get("/chapters/chapter/versions").json()[0]["state_deltas"] == [delta]
    approved = client.post("/api/review-center/approve", json={"chapter_id": "chapter", "version_id": response.json()["chapter_version_id"]})
    assert approved.status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ContinuityEvent)) == 1


def test_empty_review_does_not_replace_current_version(workspace):
    client, engine = workspace
    response = client.post("/api/creation/review", json={"chapter_id": "chapter", "review_action": "review_pending", "content": "  "})
    assert response.status_code == 422
    with Session(engine) as session:
        assert session.get(Chapter, "chapter").current_version_id == "old"


def test_save_preserves_edited_title(workspace):
    client, engine = workspace
    response = client.post("/api/creation/save", json={"chapter_id": "chapter", "content": "edited", "summary": "summary", "title": "Edited title"})
    assert response.status_code == 200
    with Session(engine) as session:
        assert session.get(Chapter, "chapter").title == "Edited title"


def test_gate_checks_existence_and_performs_real_approval(workspace):
    client, engine = workspace
    assert client.post("/chapters/missing/gate", json={"action": "approve"}).status_code == 404
    client.post("/api/creation/review", json={"chapter_id": "chapter", "review_action": "review_pending", "content": "candidate"})
    response = client.post("/chapters/chapter/gate", json={"action": "approve", "version_id": client.get("/chapters/chapter").json()["current_version_id"]})
    assert response.status_code == 200
    with Session(engine) as session:
        assert session.get(Chapter, "chapter").status == "persisted"


def test_chapter_input_rejects_invalid_number_and_blank_title(workspace):
    client, _ = workspace
    for number, title in [(0, "title"), (1, "   ")]:
        assert client.post("/chapters", json={"book_id": "book", "chapter_number": number, "title": title, "goal": {}}).status_code == 422


def test_draft_operations_reject_blank_title_without_new_version(workspace):
    client, engine = workspace
    for endpoint in ("save", "review"):
        response = client.post("/api/creation/" + endpoint, json={"chapter_id": "chapter", "content": "new content", "title": "   ", "review_action": "review_pending"})
        assert response.status_code == 422
    with Session(engine) as session:
        assert session.get(Chapter, "chapter").current_version_id == "old"
