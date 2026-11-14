from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.context_builder import build_chapter_context, build_chapter_context_from_memory
from app.agents.review_issues import normalize_review_issues
from app.agents.reviewer import review_chapter
from app.agents.reviser import revise_chapter
from app.agents.writer import write_chapter
from app.db.base import Base
from app.db.models import Book, Chapter, ChapterVersion, Character, CharacterSnapshot, StoryArc
from app.schemas.tasks import AgentEnvelope
from app.services.embedding import KeywordEmbeddingClient


def _approve_fixture_history(session):
    session.flush()
    for version in session.scalars(select(ChapterVersion).where(ChapterVersion.source == "approved")):
        session.get(Chapter, version.chapter_id).approved_version_id = version.id


def test_build_context_contains_goal_and_recent_summaries():
    payload = build_chapter_context(
        world_state={"setting": "urban"},
        character_states=[{"name": "Lin Fan", "age": 18}],
        recent_summaries=["hero gets a system"],
        chapter_goal={"objective": "test the livestream room"},
    )

    assert payload["chapter_goal"]["objective"] == "test the livestream room"
    assert payload["recent_summaries"] == ["hero gets a system"]


def test_build_context_from_memory_uses_story_and_character_state():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        chapter = Chapter(
            id="ch_0001",
            book_id="book_001",
            chapter_number=3,
            title="Three",
            goal={"objective": "push the mystery forward"},
            status="draft_planned",
        )
        session.add(chapter)
        session.add(
            StoryArc(
                book_id="book_001",
                arc_type="main",
                chapter_start=1,
                chapter_end=10,
                stage_goal="solve the first case",
                outline={"beats": ["clue", "suspect"]},
            )
        )
        character = Character(
            book_id="book_001",
            name="Lin Fan",
            role="lead",
            age=18,
            profile={"trait": "calm"},
            last_updated_chapter_number=2,
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
        session.add(Chapter(id="history", book_id="book_001", chapter_number=2, title="History"))
        session.add(
            ChapterVersion(
                chapter_id="history",
                source="approved",
                content="body",
                summary="latest summary",
                review_score=95,
            )
        )
        _approve_fixture_history(session)
        session.commit()

        payload = build_chapter_context_from_memory(session=session, chapter_id="ch_0001")

        assert payload["chapter_goal"]["objective"] == "push the mystery forward"
        assert payload["world_state"]["active_story_arcs"][0]["stage_goal"] == "solve the first case"
        assert payload["character_states"][0]["name"] == "Lin Fan"
        assert payload["recent_summaries"] == ["latest summary"]


def test_build_context_from_memory_excludes_future_character_snapshots():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.add_all(
            [
                Chapter(
                    id="ch_0001",
                    book_id="book_001",
                    chapter_number=1,
                    title="One",
                    status="persisted",
                ),
                Chapter(
                    id="ch_0002",
                    book_id="book_001",
                    chapter_number=2,
                    title="Two",
                    status="draft_planned",
                ),
                Chapter(
                    id="ch_0003",
                    book_id="book_001",
                    chapter_number=3,
                    title="Three",
                    status="persisted",
                ),
            ]
        )
        character = Character(book_id="book_001", name="Lin Fan", role="lead")
        session.add(character)
        session.flush()
        session.add_all(
            [
                CharacterSnapshot(
                    id="snapshot_chapter_1",
                    character_id=character.id,
                    chapter_id="ch_0001",
                    state={"location": "old station"},
                    created_at=datetime(2026, 1, 1),
                ),
                CharacterSnapshot(
                    id="snapshot_chapter_3",
                    character_id=character.id,
                    chapter_id="ch_0003",
                    state={"location": "future tower"},
                    created_at=datetime(2026, 1, 2),
                ),
            ]
        )
        session.commit()

        payload = build_chapter_context_from_memory(
            session=session,
            chapter_id="ch_0002",
        )

        assert payload["character_states"][0]["state"] == {
            "location": "old station"
        }


def test_build_context_from_memory_excludes_target_chapter_summary():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.add(
            Chapter(
                id="ch_0001",
                book_id="book_001",
                chapter_number=3,
                title="Three",
                goal={"objective": "find the clue"},
                status="draft_planned",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id="ch_0001",
                source="approved",
                content="body",
                summary="the hero follows a clue into the warehouse",
                review_score=95,
            )
        )
        session.add(
            Chapter(
                id="ch_0000",
                book_id="book_001",
                chapter_number=2,
                title="Two",
                goal={"objective": "rest"},
                status="persisted",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id="ch_0000",
                source="approved",
                content="body",
                summary="the hero takes a quiet day off",
                review_score=90,
            )
        )
        _approve_fixture_history(session)
        session.commit()

        payload = build_chapter_context_from_memory(
            session=session,
            chapter_id="ch_0001",
        )

        assert payload["recent_summaries"] == ["the hero takes a quiet day off"]


def test_build_context_from_memory_limits_recent_summaries_by_database_recency():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.add(
            Chapter(
                id="ch_0001",
                book_id="book_001",
                chapter_number=4,
                title="Four",
                goal={"objective": "find the clue"},
                status="draft_planned",
            )
        )
        session.add(
            Chapter(
                id="ch_0000",
                book_id="book_001",
                chapter_number=3,
                title="Three",
                goal={"objective": "previous"},
                status="persisted",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id="ch_0000",
                source="approved",
                content="body",
                summary="the hero finds a clue near the warehouse",
                review_score=95,
            )
        )
        session.add(
            Chapter(
                id="ch_0002",
                book_id="book_001",
                chapter_number=2,
                title="Two",
                goal={"objective": "previous"},
                status="persisted",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id="ch_0002",
                source="approved",
                content="body",
                summary="the hero takes a quiet rest after dinner",
                review_score=90,
            )
        )
        session.add(
            Chapter(
                id="ch_0003",
                book_id="book_001",
                chapter_number=1,
                title="One",
                goal={"objective": "previous"},
                status="persisted",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id="ch_0003",
                source="approved",
                content="body",
                summary="a clue appears in a late-night call",
                review_score=91,
            )
        )
        _approve_fixture_history(session)
        session.commit()

        payload = build_chapter_context_from_memory(
            session=session,
            chapter_id="ch_0001",
            summary_limit=2,
        )

        assert len(payload["recent_summaries"]) == 2
        assert "clue" in payload["recent_summaries"][0]


def test_build_context_exposes_retrieval_evidence_without_reordering_recent_summaries():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Book(id="book_001", title="Test", platform="fanqie", genre="urban"))
        session.add_all(
            [
                Chapter(
                    id="ch_0001",
                    book_id="book_001",
                    chapter_number=1,
                    title="One",
                    status="persisted",
                ),
                Chapter(
                    id="ch_0002",
                    book_id="book_001",
                    chapter_number=2,
                    title="Two",
                    status="persisted",
                ),
                Chapter(
                    id="ch_0003",
                    book_id="book_001",
                    chapter_number=3,
                    title="Three",
                    goal={"objective": "warehouse clue"},
                    status="draft_planned",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ChapterVersion(
                    id="matching_summary",
                    chapter_id="ch_0001",
                    source="approved",
                    summary="warehouse clue",
                ),
                ChapterVersion(
                    id="latest_summary",
                    chapter_id="ch_0002",
                    source="approved",
                    summary="quiet dinner",
                ),
            ]
        )
        _approve_fixture_history(session)
        session.commit()

        payload = build_chapter_context_from_memory(
            session=session,
            chapter_id="ch_0003",
            embedding_client=None,
            embedding_model_name="keyword-hash",
            fallback_embedding_client=KeywordEmbeddingClient(),
            embedding_fallback_mode="keyword",
        )

        assert payload["recent_summaries"] == ["quiet dinner", "warehouse clue"]
        assert payload["retrieval_evidence"][0]["source_id"] == "matching_summary"
        assert payload["retrieval_mode"] == "keyword"
        assert (
            payload["retrieval_degradation_reason"]
            == "embedding API key is not configured"
        )


def test_agent_envelope_defaults_are_empty_or_none():
    envelope = AgentEnvelope(
        book_id="book_001",
        chapter_id="chapter_001",
        task_id="task_001",
        trace_id="trace_001",
        input={"chapter_goal": {"objective": "test"}},
    )

    assert envelope.output is None
    assert envelope.status == "pending"
    assert envelope.error is None
    assert envelope.meta == {}


def test_agent_envelope_rejects_invalid_status():
    with pytest.raises(ValidationError):
        AgentEnvelope(
            book_id="book_001",
            chapter_id="chapter_001",
            task_id="task_001",
            trace_id="trace_001",
            input={"chapter_goal": {"objective": "test"}},
            status="running",
        )


def test_writer_calls_generate_json_with_prompt_fields():
    class StubClient:
        def __init__(self) -> None:
            self.prompt = None

        def generate_json(self, prompt: str) -> dict:
            self.prompt = prompt
            return {"content": "chapter body"}

    client = StubClient()
    context = {
        "world_state": {"setting": "urban"},
        "character_states": [{"name": "Lin Fan"}],
        "recent_summaries": ["hero gets a system"],
        "chapter_goal": {"objective": "test the livestream room"},
    }

    result = write_chapter(client=client, context=context)

    assert result == {"content": "chapter body"}
    assert '"objective"' in client.prompt
    assert "test the livestream room" in client.prompt
    assert "recent_summaries" in client.prompt


def test_reviewer_calls_generate_json_with_prompt_fields():
    class StubClient:
        def __init__(self) -> None:
            self.prompt = None

        def generate_json(self, prompt: str) -> dict:
            self.prompt = prompt
            return {"approved": True}

    client = StubClient()

    result = review_chapter(
        client=client,
        content="chapter body",
        canonical_facts={"setting": "urban"},
    )

    assert result["approved"] is True
    assert result["issues"] == []
    assert result["revision_priorities"] == []
    assert "chapter body" in client.prompt
    assert "canonical_facts" in client.prompt
    assert "urban" in client.prompt
    assert "revision_priorities" in client.prompt


def test_reviewer_prompt_includes_story_arcs_and_character_states():
    class StubClient:
        def __init__(self) -> None:
            self.prompt = None

        def generate_json(self, prompt: str) -> dict:
            self.prompt = prompt
            return {"approved": True, "review_score": 95}

    client = StubClient()

    result = review_chapter(
        client=client,
        content="chapter body",
        canonical_facts={
            "chapter_goal": {"objective": "solve the case"},
            "world_state": {
                "active_story_arcs": [
                    {"stage_goal": "solve the first case"},
                ]
            },
            "character_states": [
                {"name": "Lin Fan", "state": {"mood": "focused"}},
            ],
            "recent_summaries": ["hero finds a clue"],
        },
    )

    assert result["approved"] is True
    assert "solve the first case" in client.prompt
    assert "Lin Fan" in client.prompt
    assert "hero finds a clue" in client.prompt


def test_review_chapter_adds_rule_based_issues_for_missing_must_include_and_banned_phrase():
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": True,
                "review_score": 92,
                "summary": "Base review",
                "issues": [],
                "state_deltas": [],
            }

    result = review_chapter(
        client=StubClient(),
        content="主角重复解释系统设定，但没有发现核心线索。",
        canonical_facts={
            "chapter_goal": {
                "objective": "推进调查",
                "must_include": ["地下仓库坐标", "地下仓库坐标"],
                "must_avoid": ["重复解释系统设定", "重复解释系统设定"],
                "target_words": 2000,
            }
        },
    )

    assert result["approved"] is False
    assert result["review_score"] < 92
    assert any(issue["type"] == "coverage" for issue in result["issues"])
    assert any(issue["type"] == "constraint" for issue in result["issues"])
    assert "补齐必需要素" in result["revision_priorities"]
    assert (
        [issue["type"] for issue in result["issues"]].count("coverage"),
        [issue["type"] for issue in result["issues"]].count("constraint"),
        result["review_score"],
    ) == (1, 1, 60)


def test_review_chapter_adds_pacing_issue_when_far_below_target_words():
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": True,
                "review_score": 95,
                "summary": "Base review",
                "issues": [],
                "state_deltas": [],
            }

    result = review_chapter(
        client=StubClient(),
        content="太短了。",
        canonical_facts={
            "chapter_goal": {
                "objective": "推进调查",
                "must_include": [],
                "must_avoid": [],
                "target_words": 1200,
            }
        },
    )

    assert result["approved"] is False
    assert any(issue["type"] == "pacing" for issue in result["issues"])
    assert "补足正文篇幅" in result["revision_priorities"]


def test_normalize_review_issues_deduplicates_and_validates_fields():
    issues = normalize_review_issues(
        [
            {"type": "logic", "severity": "urgent", "message": "Door   WAS locked"},
            {
                "type": "logic",
                "severity": "high",
                "message": "  door was LOCKED  ",
                "evidence": "上一章结尾明确写明门锁损坏",
                "source": "rule",
            },
            {"type": "unknown", "message": "语气不统一"},
            {"type": "logic", "severity": "low", "message": "   "},
        ]
    )

    assert len(issues) == 2
    assert issues[0] == {
        "id": issues[0]["id"],
        "type": "logic",
        "severity": "high",
        "message": "Door   WAS locked",
        "evidence": "上一章结尾明确写明门锁损坏",
        "source": "rule",
    }
    assert issues[0]["id"].startswith("issue_")
    assert len(issues[0]["id"]) == len("issue_") + 12
    assert issues[0]["id"] == normalize_review_issues(
        [{"type": "logic", "severity": "low", "message": "door was locked"}]
    )[0]["id"]
    assert issues[1]["type"] == "style"
    assert issues[1]["severity"] == "medium"


@pytest.mark.parametrize(
    ("severity", "expected_approved"),
    [("low", True), ("medium", False), ("high", False)],
)
def test_review_chapter_severity_gate(severity: str, expected_approved: bool):
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": True,
                "review_score": 95,
                "issues": [
                    {
                        "type": "logic",
                        "severity": severity,
                        "message": "门锁状态前后不一致",
                        "evidence": "" if severity == "low" else "上一章结尾写明门锁损坏",
                    }
                ],
                "revision_priorities": ["核对门锁", "核对门锁"],
            }

    result = review_chapter(
        client=StubClient(),
        content="足够长的章节正文",
        canonical_facts={"chapter_goal": {}},
    )

    assert result["approved"] is expected_approved
    assert result["revision_priorities"] == ["核对门锁"]


@pytest.mark.parametrize(
    ("issues", "approved", "review_score", "expected_issue_count", "expected_approved", "expected_score"),
    [
        ("not-a-list", True, None, 0, True, 0),
        (
            [
                None,
                "not-a-dict",
                {
                    "type": "logic",
                    "severity": "low",
                    "message": "有效问题",
                    "evidence": "正文中的具体矛盾句",
                },
            ],
            "true",
            "not-a-number",
            1,
            False,
            0,
        ),
        ([], True, 150, 0, True, 100),
        ([], True, -10, 0, True, 0),
    ],
)
def test_review_chapter_validates_model_output(
    issues,
    approved,
    review_score,
    expected_issue_count: int,
    expected_approved: bool,
    expected_score: float,
):
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": approved,
                "review_score": review_score,
                "issues": issues,
            }

    result = review_chapter(
        client=StubClient(),
        content="章节正文",
        canonical_facts={"chapter_goal": {}},
    )

    assert len(result["issues"]) == expected_issue_count
    assert result["approved"] is expected_approved
    assert result["review_score"] == expected_score


def test_review_chapter_rejects_model_rejection_without_structured_issues():
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": False,
                "review_score": 80,
                "issues": [{"type": "logic", "message": "   "}],
            }

    with pytest.raises(ValueError, match="reviewer rejected chapter without structured issues"):
        review_chapter(
            client=StubClient(),
            content="章节正文",
            canonical_facts={"chapter_goal": {}},
        )


@pytest.mark.parametrize(
    ("model_output", "expected_priorities", "expected_error"),
    [
        ([], None, "reviewer output must be an object"),
        (
            {"approved": True, "review_score": 90, "issues": [], "revision_priorities": {}},
            [],
            None,
        ),
        (
            {
                "approved": True,
                "review_score": 90,
                "issues": [],
                "revision_priorities": [None, {}, " ", " 修复逻辑 ", "修复逻辑"],
            },
            ["修复逻辑"],
            None,
        ),
    ],
)
def test_review_chapter_validates_output_container_and_revision_priorities(
    model_output,
    expected_priorities,
    expected_error,
):
    class StubClient:
        def generate_json(self, prompt: str):
            return model_output

    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            review_chapter(
                client=StubClient(),
                content="章节正文",
                canonical_facts={"chapter_goal": {}},
            )
        return

    result = review_chapter(
        client=StubClient(),
        content="章节正文",
        canonical_facts={"chapter_goal": {}},
    )

    assert result["revision_priorities"] == expected_priorities


@pytest.mark.parametrize("severity", ["medium", "high"])
def test_review_chapter_rejects_blocking_issue_without_evidence(severity: str):
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": True,
                "review_score": 90,
                "issues": [
                    {
                        "type": "logic",
                        "severity": severity,
                        "message": "门锁状态前后不一致",
                        "evidence": "   ",
                    }
                ],
            }

    with pytest.raises(ValueError, match="blocking review issue missing evidence"):
        review_chapter(
            client=StubClient(),
            content="章节正文",
            canonical_facts={"chapter_goal": {}},
        )


def test_review_chapter_rejects_unevidenced_low_only_model_rejection():
    class StubClient:
        def generate_json(self, prompt: str) -> dict:
            return {
                "approved": False,
                "review_score": 90,
                "issues": [
                    {
                        "type": "style",
                        "severity": "low",
                        "message": "语气略有波动",
                    }
                ],
            }

    with pytest.raises(ValueError, match="reviewer rejected chapter without evidenced issue"):
        review_chapter(
            client=StubClient(),
            content="章节正文",
            canonical_facts={"chapter_goal": {}},
        )


def test_reviser_prompt_includes_structured_revision_issues():
    class StubClient:
        def __init__(self) -> None:
            self.prompt = None

        def generate_json(self, prompt: str) -> dict:
            self.prompt = prompt
            return {"revised_content": "revised"}

    client = StubClient()

    result = revise_chapter(
        client=client,
        original_content="chapter body",
        revision_instructions={
            "issues": [
                {
                    "type": "continuity",
                    "message": "character mood changed too abruptly",
                }
            ],
            "must_preserve": ["core clue", "ending beat"],
        },
    )

    assert result == {"revised_content": "revised"}
    assert "continuity" in client.prompt
    assert "character mood changed too abruptly" in client.prompt
    assert "core clue" in client.prompt
    assert "change_summary" in client.prompt
