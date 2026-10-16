from typing import Any

from sqlalchemy import select

from app.db.models import Chapter
from app.db.repositories import MemoryRepository
from app.services.embedding import EmbeddingClient
from app.services.memory_index import retrieve_memory_evidence


def build_chapter_context(
    world_state: dict[str, Any],
    character_states: list[dict[str, Any]],
    recent_summaries: list[str],
    chapter_goal: dict[str, Any],
) -> dict[str, Any]:
    return {
        "world_state": world_state,
        "character_states": character_states,
        "recent_summaries": recent_summaries,
        "chapter_goal": chapter_goal,
    }


def build_chapter_context_from_memory(
    *,
    session,
    chapter_id: str,
    embedding_client: EmbeddingClient | None = None,
    embedding_model_name: str = "keyword-hash",
    fallback_embedding_client: EmbeddingClient | None = None,
    embedding_fallback_mode: str = "keyword",
    summary_limit: int = 3,
) -> dict[str, Any]:
    chapter = session.scalar(select(Chapter).where(Chapter.id == chapter_id))
    if chapter is None:
        raise ValueError(f"Chapter not found: {chapter_id}")

    memory_repo = MemoryRepository(session)
    story_arcs = memory_repo.list_story_arcs(chapter.book_id)
    character_states = memory_repo.list_latest_character_states(
        chapter.book_id,
        before_chapter_number=chapter.chapter_number,
    )
    recent_summaries = memory_repo.list_recent_summaries(chapter_id)
    recent_summaries = recent_summaries[:summary_limit]
    chapter_goal = chapter.goal or {}
    retrieval = retrieve_memory_evidence(
        session=session,
        book_id=chapter.book_id,
        target_chapter_number=chapter.chapter_number,
        query=_chapter_goal_query(chapter_goal),
        embedding_client=embedding_client,
        model_name=embedding_model_name,
        fallback_client=fallback_embedding_client,
        embedding_fallback_mode=embedding_fallback_mode,
        limit=5,
    )

    context = build_chapter_context(
        world_state={
            "active_story_arcs": [
                {
                    "id": arc.id,
                    "arc_type": arc.arc_type,
                    "chapter_start": arc.chapter_start,
                    "chapter_end": arc.chapter_end,
                    "stage_goal": arc.stage_goal,
                    "outline": arc.outline or {},
                }
                for arc in story_arcs
            ]
        },
        character_states=character_states,
        recent_summaries=recent_summaries,
        chapter_goal=chapter_goal,
    )
    context["retrieval_evidence"] = retrieval["items"]
    context["retrieval_mode"] = retrieval["mode"]
    context["retrieval_degradation_reason"] = retrieval["degradation_reason"]
    return context


def _chapter_goal_query(chapter_goal: dict[str, Any]) -> str:
    objective = chapter_goal.get("objective") or chapter_goal.get("chapter_goal")
    if isinstance(objective, str) and objective.strip():
        return objective.strip()
    return str(chapter_goal)
