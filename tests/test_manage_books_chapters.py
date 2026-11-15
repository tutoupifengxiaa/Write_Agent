from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.persister import persist_approved_chapter
from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    Character,
    CharacterSnapshot,
    ContinuityEvent,
    MemoryEmbedding,
    PublishJob,
    StoryArc,
)
from app.db.repositories import MemoryEmbeddingRepository
from app.orchestrator.task_claims import utc_now

MODELS = (Book, Chapter, ChapterVersion, ContinuityEvent, Character, CharacterSnapshot, PublishJob,
          StoryArc, MemoryEmbedding)


@pytest.fixture
def setup():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    # Enforce foreign keys so a wrong deletion order fails loudly.
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        for book_id in ("keep", "drop"):
            session.add(Book(id=book_id, title=book_id, platform="fanqie", genre="urban"))
            session.flush()
            session.add(StoryArc(book_id=book_id, arc_type="act", chapter_start=1, stage_goal="goal"))
            for number in (1, 2):
                session.add(Chapter(id=f"{book_id}{number}", book_id=book_id, chapter_number=number,
                                    title=f"{book_id}{number}"))
            session.flush()
            for number in (1, 2):
                result = persist_approved_chapter(
                    session=session, chapter_id=f"{book_id}{number}", content="正文", summary="摘要",
                    review_score=90, create_publish_job=True,
                    state_deltas=[{"type": "character", "target": "林川", "change": f"变化{number}"}],
                )
                MemoryEmbeddingRepository(session).upsert(
                    book_id=book_id, source_type="chapter_summary", source_id=result["chapter_version_id"],
                    source_text="摘要", model="fake", vector=[1.0])
        session.commit()
    app = create_fastapi_app(initialize_database=False)
    app.state.session_factory = factory
    return TestClient(app), factory


def counts(factory, book_id):
    """Rows per table that belong to the given book."""
    with factory() as session:
        chapter_ids = select(Chapter.id).where(Chapter.book_id == book_id)
        by_chapter = lambda model: session.scalar(  # noqa: E731
            select(func.count()).select_from(model).where(model.chapter_id.in_(chapter_ids)))
        by_book = lambda model: session.scalar(  # noqa: E731
            select(func.count()).select_from(model).where(model.book_id == book_id))
        return {
            "book": session.scalar(select(func.count()).select_from(Book).where(Book.id == book_id)),
            "chapters": by_book(Chapter), "versions": by_chapter(ChapterVersion),
            "events": by_chapter(ContinuityEvent), "snapshots": by_chapter(CharacterSnapshot),
            "jobs": by_chapter(PublishJob), "characters": by_book(Character), "arcs": by_book(StoryArc),
            "embeddings": by_book(MemoryEmbedding),
        }


FULL = {"book": 1, "chapters": 2, "versions": 2, "events": 2, "snapshots": 2, "jobs": 2, "characters": 1,
        "arcs": 1, "embeddings": 2}


def test_edit_book_metadata(setup):
    client, factory = setup
    response = client.patch("/books/keep", json={"title": " 新书名 ", "genre": "悬疑"})
    assert response.status_code == 200
    assert (response.json()["title"], response.json()["genre"], response.json()["platform"]) == ("新书名", "悬疑", "fanqie")
    assert client.patch("/books/keep", json={"title": "  "}).status_code == 422
    assert client.patch("/books/missing", json={"title": "x"}).status_code == 404


def test_delete_book_removes_everything_it_owns_and_nothing_else(setup):
    client, factory = setup
    response = client.delete("/books/drop")
    assert response.status_code == 200 and response.json() == {"book_id": "drop", "deleted_chapters": 2}
    assert set(counts(factory, "drop").values()) == {0}
    assert counts(factory, "keep") == FULL
    assert client.delete("/books/drop").status_code == 404


@pytest.mark.parametrize("busy", ["claimed_chapter", "uploading_job"])
def test_delete_is_refused_while_work_is_running(setup, busy):
    client, factory = setup
    with factory() as session:
        if busy == "claimed_chapter":
            chapter = session.get(Chapter, "drop1")
            chapter.claim_token, chapter.lease_expires_at = "worker", utc_now() + timedelta(minutes=5)
        else:
            session.scalars(select(PublishJob).where(PublishJob.chapter_id == "drop2")).first().status = "publishing"
        session.commit()
    assert client.delete("/books/drop").status_code == 409
    assert client.delete("/chapters/drop1" if busy == "claimed_chapter" else "/chapters/drop2").status_code == 409
    assert counts(factory, "drop") == FULL


def test_expired_claim_does_not_block_deletion(setup):
    client, factory = setup
    with factory() as session:
        chapter = session.get(Chapter, "drop1")
        chapter.claim_token, chapter.lease_expires_at = "worker", utc_now() - timedelta(minutes=5)
        session.commit()
    assert client.delete("/books/drop").status_code == 200


def test_edit_chapter_title_and_number(setup):
    client, factory = setup
    response = client.patch("/chapters/keep2", json={"title": "新标题", "chapter_number": 5})
    assert response.status_code == 200
    assert (response.json()["title"], response.json()["chapter_number"]) == ("新标题", 5)
    conflict = client.patch("/chapters/keep2", json={"chapter_number": 1})
    assert conflict.status_code == 409 and "第 1 章已存在" in conflict.json()["detail"]
    assert client.patch("/chapters/keep2", json={"chapter_number": 5}).status_code == 200
    assert client.patch("/chapters/keep2", json={"chapter_number": 0}).status_code == 422


def test_delete_chapter_removes_only_that_chapters_data(setup):
    client, factory = setup
    assert client.delete("/chapters/keep1").status_code == 200
    after = counts(factory, "keep")
    assert after == {**FULL, "chapters": 1, "versions": 1, "events": 1, "snapshots": 1, "jobs": 1,
                     "embeddings": 1}
    assert counts(factory, "drop") == FULL
    assert client.delete("/chapters/keep1").status_code == 404


def test_new_chapter_refuses_a_taken_number_or_missing_book(setup):
    client, factory = setup
    new = {"book_id": "keep", "title": "新章", "goal": {"objective": "推进"}}
    taken = client.post("/chapters", json={**new, "chapter_number": 2})
    assert taken.status_code == 409 and "第 2 章已存在" in taken.json()["detail"]
    assert client.post("/chapters", json={**new, "book_id": "nope", "chapter_number": 9}).status_code == 404
    # The same number is still free in another book, and a fresh number works.
    assert client.post("/chapters", json={**new, "book_id": "drop", "chapter_number": 3}).status_code == 201
    assert client.post("/chapters", json={**new, "chapter_number": 3}).status_code == 201
    assert counts(factory, "keep")["chapters"] == 3
