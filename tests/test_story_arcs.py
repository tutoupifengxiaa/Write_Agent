from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from app.agents.context_builder import build_chapter_context_from_memory
from app.agents.story_arcs import arcs_covering, build_story_arcs
from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import Book, Chapter, StoryArc
from app.db.schema_migrations import upgrade_database
from app.services.embedding import KeywordEmbeddingClient


def ranges(arcs):
    return [(arc["outline"]["name"], arc["chapter_start"], arc["chapter_end"]) for arc in arcs]


def test_explicit_act_ranges_are_used():
    arcs = build_story_arcs({"acts": [
        {"name": "入局", "goal": "拿到钥匙", "chapter_start": 1, "chapter_end": 10},
        {"name": "破局", "goal": "揭开真相", "chapter_start": "11", "chapter_end": 30},
    ]})
    assert ranges(arcs) == [("入局", 1, 10), ("破局", 11, 30)]
    assert arcs[0]["stage_goal"] == "拿到钥匙" and arcs[0]["arc_type"] == "act"


def test_string_acts_get_ranges_from_seed_plan():
    outline = {"acts": ["入局", "受压", "破局"],
               "chapter_seed_plan": [{"chapter": n} for n in (1, 2, 3, 4, 5, 6)]}
    assert ranges(build_story_arcs(outline)) == [("入局", 1, 2), ("受压", 3, 4), ("破局", 5, None)]


def test_more_acts_than_seeds_share_starts_and_stay_valid():
    outline = {"acts": ["一", "二", "三"], "chapter_seed_plan": [{"chapter": 1}, {"chapter": 2}]}
    assert ranges(build_story_arcs(outline)) == [("一", 1, 1), ("二", 2, None), ("三", 2, None)]


def test_acts_without_any_range_cover_the_whole_book_and_bad_input_is_ignored():
    assert ranges(build_story_arcs({"acts": ["入局", "", 5, {"name": "破局", "chapter_start": 0}]})) == [
        ("入局", 1, None), ("破局", 1, None)]
    assert build_story_arcs({"acts": [{"name": "倒序", "chapter_start": 9, "chapter_end": 3}]})[0]["chapter_end"] is None
    assert build_story_arcs(None) == [] and build_story_arcs({"acts": "x"}) == []


@pytest.fixture
def client_and_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    app = create_fastapi_app(initialize_database=False)
    app.state.session_factory = factory
    return TestClient(app), factory


def act_rows(factory, book_id="book"):
    with factory() as session:
        return session.execute(
            select(StoryArc.arc_type, StoryArc.stage_goal, StoryArc.chapter_start, StoryArc.chapter_end)
            .where(StoryArc.book_id == book_id).order_by(StoryArc.arc_type, StoryArc.chapter_start)
        ).all()


def test_creating_and_saving_outline_syncs_act_arcs_but_keeps_other_arcs(client_and_factory):
    client, factory = client_and_factory
    outline = {"acts": [{"name": "入局", "goal": "拿到钥匙", "chapter_start": 1, "chapter_end": 5}]}
    assert client.post("/books", json={"id": "book", "title": "T", "platform": "fanqie", "genre": "urban",
                                       "book_outline": outline}).status_code == 201
    assert act_rows(factory) == [("act", "拿到钥匙", 1, 5)]
    with factory() as session:
        session.add(StoryArc(book_id="book", arc_type="romance", chapter_start=3, stage_goal="感情线"))
        session.commit()

    new_outline = {"acts": ["受压", "破局"], "chapter_seed_plan": [{"chapter": 1}, {"chapter": 4}]}
    assert client.put("/books/book/outline", json={"book_outline": new_outline}).status_code == 200
    assert act_rows(factory) == [("act", "受压", 1, 3), ("act", "破局", 4, None), ("romance", "感情线", 3, None)]


def test_writing_context_only_gets_the_act_covering_the_chapter():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Book(id="book", title="T", platform="fanqie", genre="urban"))
        session.flush()
        session.add_all([
            StoryArc(book_id="book", arc_type="act", chapter_start=1, chapter_end=5, stage_goal="拿到钥匙"),
            StoryArc(book_id="book", arc_type="act", chapter_start=6, stage_goal="揭开真相"),
            Chapter(id="c7", book_id="book", chapter_number=7, title="7"),
        ])
        session.flush()
        context = build_chapter_context_from_memory(
            session=session, chapter_id="c7", fallback_embedding_client=KeywordEmbeddingClient(),
        )
        goals = [arc["stage_goal"] for arc in context["world_state"]["active_story_arcs"]]
        assert goals == ["揭开真相"]
        # When nothing covers the chapter, the whole arc list is kept.
        assert len(arcs_covering(session.scalars(select(StoryArc)).all(), 0)) == 2


def test_migration_backfills_act_arcs_for_existing_outlines(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'arcs.db'}")
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = engine
        command.upgrade(config, "20261113_02")
        with engine.begin() as connection:
            books = Base.metadata.tables["books"]
            connection.execute(books.insert(), [
                {"id": "with_acts", "title": "A", "platform": "p", "genre": "g", "status": "planning",
                 "book_outline": {"acts": ["入局", "破局"], "chapter_seed_plan": [{"chapter": 1}, {"chapter": 3}]}},
                {"id": "no_outline", "title": "B", "platform": "p", "genre": "g", "status": "planning",
                 "book_outline": None},
            ])
        upgrade_database(engine)
        upgrade_database(engine)
        with engine.connect() as connection:
            rows = connection.execute(
                select(StoryArc.book_id, StoryArc.chapter_start, StoryArc.chapter_end).order_by(StoryArc.chapter_start)
            ).all()
        assert rows == [("with_acts", 1, 2), ("with_acts", 3, None)]
    finally:
        engine.dispose()
