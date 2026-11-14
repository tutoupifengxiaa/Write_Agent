from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import Book, Chapter, Character, CharacterSnapshot, StoryArc, TrendSnapshot


@pytest.fixture
def setup():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    app = create_fastapi_app(initialize_database=False)
    app.state.session_factory = factory
    yield TestClient(app), factory
    engine.dispose()


def _snapshot(capture_id, provider, status, minute, items=None):
    return TrendSnapshot(capture_id=capture_id, provider=provider, source_url="https://source.example/rank",
                         status=status, items=items or [], error_message=None,
                         captured_at=datetime(2026, 11, 14, 8, minute, tzinfo=timezone.utc))


def test_capture_history_lists_newest_first_without_items(setup):
    client, factory = setup
    with factory() as session:
        session.add_all([
            _snapshot("old", "fanqie", "real", 0, [{"id": "fanqie:1", "title": "旧书"}]),
            _snapshot("new", "fanqie", "real", 30, [{"id": "fanqie:1", "title": "新书"}]),
            _snapshot("new", "qidian", "failed", 31),
            _snapshot("middle", "qimao", "degraded", 10),
        ])
        session.commit()

    rows = client.get("/trends/captures").json()

    assert [row["capture_id"] for row in rows] == ["new", "middle", "old"]
    assert rows[0]["providers"] == [{"provider": "fanqie", "status": "real"},
                                    {"provider": "qidian", "status": "failed"}]
    assert rows[0]["captured_at"].startswith("2026-11-14T08:31")
    assert "items" not in rows[0]
    assert [row["capture_id"] for row in client.get("/trends/captures?limit=1").json()] == ["new"]
    assert client.get("/trends/captures?limit=0").status_code == 422
    # An older capture can still be opened through the existing endpoints.
    saved = client.get("/trends/snapshots", params={"capture_id": "old"}).json()
    assert saved[0]["items"][0]["title"] == "旧书"


def test_capture_history_is_empty_without_captures(setup):
    client, _ = setup
    assert client.get("/trends/captures").json() == []


def _seed_book(session):
    session.add(Book(id="book", title="B", platform="fanqie", genre="urban"))
    session.flush()
    session.add_all([
        StoryArc(book_id="book", arc_type="act", chapter_start=1, chapter_end=10, stage_goal="拿到钥匙"),
        StoryArc(book_id="book", arc_type="act", chapter_start=11, chapter_end=None, stage_goal="打开密室"),
        Chapter(id="c1", book_id="book", chapter_number=1, title="一", status="persisted"),
        Chapter(id="c5", book_id="book", chapter_number=5, title="五", status="persisted"),
        Character(id="lin", book_id="book", name="林凡", profile={}),
    ])
    session.flush()
    session.add_all([
        CharacterSnapshot(character_id="lin", chapter_id="c1", state={"latest_change": "拿到钥匙"},
                          created_at=datetime(2026, 11, 1, tzinfo=timezone.utc)),
        CharacterSnapshot(character_id="lin", chapter_id="c5", state={"latest_change": "受了伤"},
                          created_at=datetime(2026, 11, 2, tzinfo=timezone.utc)),
    ])
    session.commit()


def _plan(client, chapter_number, **extra):
    return client.post("/chapters/plan", json={
        "book_id": "book", "chapter_number": chapter_number, "title": "新章",
        "chapter_goal": "推进剧情", **extra,
    })


def test_plan_uses_arcs_covering_the_chapter_and_earlier_character_states(setup):
    client, factory = setup
    with factory() as session:
        _seed_book(session)

    early = _plan(client, 3, must_include=["找到线索"], target_words=3000)
    late = _plan(client, 12)

    assert early.status_code == 201
    goal = early.json()["goal"]
    assert goal["context_signals"] == {"story_arc_goals": ["拿到钥匙"], "character_state_hints": ["林凡:拿到钥匙"]}
    assert goal["must_include"] == ["找到线索"] and goal["target_words"] == 3000
    assert late.json()["goal"]["context_signals"] == {
        "story_arc_goals": ["打开密室"], "character_state_hints": ["林凡:受了伤"],
    }
    with factory() as session:
        assert session.scalar(select(Chapter.status).where(Chapter.chapter_number == 12)) == "draft_planned"


def test_plan_rejects_missing_book_and_taken_chapter_number(setup):
    client, factory = setup
    with factory() as session:
        _seed_book(session)

    taken = _plan(client, 5)
    assert taken.status_code == 409 and "第 5 章已存在" in taken.json()["detail"]
    missing = client.post("/chapters/plan", json={
        "book_id": "nope", "chapter_number": 1, "title": "x", "chapter_goal": "y",
    })
    assert missing.status_code == 404
    with factory() as session:
        assert len(session.scalars(select(Chapter).where(Chapter.book_id == "book")).all()) == 2
