import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.context_builder import build_chapter_context_from_memory
from app.agents.persister import persist_approved_chapter
from app.agents.reviewer import render_reviewer_prompt
from app.agents.state_deltas import STATE_DELTA_RULE, character_changes
from app.agents.writer import render_writer_prompt
from app.db.base import Base
from app.db.models import Book, Chapter, Character, CharacterSnapshot
from app.services.embedding import KeywordEmbeddingClient


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        database_session.add(Book(id="book", title="B", platform="fanqie", genre="urban"))
        for number in (1, 2, 3):
            database_session.add(Chapter(id=f"c{number}", book_id="book", chapter_number=number, title=str(number)))
        database_session.flush()
        yield database_session


def approve(session, chapter_id, deltas):
    persist_approved_chapter(session=session, chapter_id=chapter_id, content="正文", summary="摘要",
                             review_score=90, state_deltas=deltas)
    session.flush()


def snapshots(session):
    rows = session.execute(
        select(Character.name, CharacterSnapshot.chapter_id, CharacterSnapshot.state)
        .join(Character, Character.id == CharacterSnapshot.character_id)
        .order_by(CharacterSnapshot.chapter_id, Character.name)
    )
    return [tuple(row) for row in rows]


def test_character_changes_groups_by_name_and_ignores_other_deltas():
    deltas = [
        {"type": "character", "target": " 林川 ", "change": "获得旧钥匙"},
        {"type": "Character_State", "target": "林川", "change": "左手受伤"},
        {"type": "角色", "target": "苏晴", "change": "开始怀疑林川"},
        {"type": "item", "target": "旧钥匙", "change": "开始发烫"},
        {"type": "character", "target": "", "change": "没有名字"},
        {"type": "character", "target": "王五", "change": ""},
        "not a dict",
    ]
    assert character_changes(deltas) == {"林川": ["获得旧钥匙", "左手受伤"], "苏晴": ["开始怀疑林川"]}


def test_approval_creates_characters_and_snapshots_only_for_character_deltas(session):
    approve(session, "c1", [
        {"type": "character", "target": "林川", "change": "获得旧钥匙"},
        {"type": "world", "target": "城市", "change": "开始下雨"},
    ])
    assert [c.name for c in session.scalars(select(Character))] == ["林川"]
    assert snapshots(session) == [
        ("林川", "c1", {"latest_change": "获得旧钥匙", "changes": [{"chapter": 1, "change": "获得旧钥匙"}]}),
    ]
    assert session.scalar(select(Character)).last_updated_chapter_number == 1


def test_snapshots_accumulate_across_chapters(session):
    approve(session, "c1", [{"type": "character", "target": "林川", "change": "获得旧钥匙"}])
    approve(session, "c2", [{"type": "character", "target": "林川", "change": "左手受伤"}])
    latest = snapshots(session)[-1]
    assert latest[1] == "c2"
    assert latest[2] == {
        "latest_change": "左手受伤",
        "changes": [{"chapter": 1, "change": "获得旧钥匙"}, {"chapter": 2, "change": "左手受伤"}],
    }
    assert session.scalar(select(Character)).last_updated_chapter_number == 2


def test_reapproving_a_chapter_replaces_its_snapshots(session):
    approve(session, "c1", [{"type": "character", "target": "林川", "change": "获得旧钥匙"},
                            {"type": "character", "target": "苏晴", "change": "出场"}])
    approve(session, "c1", [{"type": "character", "target": "林川", "change": "捡到旧钥匙"}])
    assert snapshots(session) == [
        ("林川", "c1", {"latest_change": "捡到旧钥匙", "changes": [{"chapter": 1, "change": "捡到旧钥匙"}]}),
    ]
    # The character record itself is kept; it simply has no state from this chapter any more.
    assert sorted(c.name for c in session.scalars(select(Character))) == ["林川", "苏晴"]


def test_later_chapter_context_sees_character_state(session):
    approve(session, "c1", [{"type": "character", "target": "林川", "change": "获得旧钥匙"}])
    approve(session, "c2", [{"type": "character", "target": "苏晴", "change": "开始怀疑林川"}])

    context = build_chapter_context_from_memory(
        session=session, chapter_id="c3", fallback_embedding_client=KeywordEmbeddingClient(),
    )

    states = {item["name"]: item["state"] for item in context["character_states"]}
    assert states["林川"]["latest_change"] == "获得旧钥匙"
    assert states["苏晴"]["latest_change"] == "开始怀疑林川"
    # Chapter 2's own context must not see chapter 2's changes.
    context_c2 = build_chapter_context_from_memory(
        session=session, chapter_id="c2", fallback_embedding_client=KeywordEmbeddingClient(),
    )
    assert {item["name"]: item["state"] for item in context_c2["character_states"]}["苏晴"] == {}


def test_writer_and_reviewer_prompts_ask_for_named_character_deltas():
    writer = json.loads(render_writer_prompt({"chapter_title": "t", "chapter_goal": {}}))
    reviewer = json.loads(render_reviewer_prompt("正文", {}))
    assert writer["requirements"]["state_deltas"] == STATE_DELTA_RULE
    assert STATE_DELTA_RULE in reviewer["instructions"]
    for schema in (writer["output_schema"], reviewer["output_schema"]):
        assert "character" in schema["state_deltas"][0]["type"]
        assert "name" in schema["state_deltas"][0]["target"]
