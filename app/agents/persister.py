from __future__ import annotations

from sqlalchemy import delete, select

from app.agents.state_deltas import character_changes
from app.db.models import Chapter, ChapterVersion, Character, CharacterSnapshot, ContinuityEvent
from app.db.repositories import PublishJobRepository

CHARACTER_HISTORY_LIMIT = 10


def persist_approved_chapter(
    *,
    session,
    chapter_id: str,
    content: str,
    summary: str,
    review_score: float,
    state_deltas: list[dict],
    generation_metadata: dict | None = None,
    create_publish_job: bool = False,
) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.id == chapter_id))
    if chapter is None:
        raise ValueError(f"Chapter not found: {chapter_id}")

    version = ChapterVersion(
        chapter_id=chapter_id,
        source="approved",
        content=content,
        summary=summary,
        review_score=review_score,
        generation_metadata=generation_metadata or {},
    )
    session.add(version)
    session.flush()

    chapter.current_version_id = version.id
    chapter.approved_version_id = version.id
    chapter.status = "persisted"

    for delta in state_deltas:
        session.add(
            ContinuityEvent(
                chapter_id=chapter_id,
                chapter_version_id=version.id,
                event_type="state_delta",
                delta=delta,
            )
        )
    _record_character_states(session, chapter, state_deltas)

    publish_job_id = None
    if create_publish_job:
        publish_repo = PublishJobRepository(session)
        job = publish_repo.create_job(
            chapter_id=chapter_id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
            chapter_version_id=version.id,
            chapter_title=chapter.title,
        )
        publish_job_id = job.id

    session.flush()
    session.refresh(version)
    return {
        "chapter_version_id": version.id,
        "db_saved": True,
        "publish_job_id": publish_job_id,
    }


def _record_character_states(session, chapter: Chapter, state_deltas: list[dict]) -> None:
    """Keep one cumulative snapshot per character that changed in this approved chapter."""
    # Re-approving a chapter replaces its earlier snapshots instead of stacking duplicates.
    session.execute(delete(CharacterSnapshot).where(CharacterSnapshot.chapter_id == chapter.id))
    for name, changes in character_changes(state_deltas).items():
        character = session.scalar(
            select(Character).where(Character.book_id == chapter.book_id, Character.name == name)
        )
        if character is None:
            character = Character(book_id=chapter.book_id, name=name)
            session.add(character)
            session.flush()
        previous = session.scalar(
            select(CharacterSnapshot.state)
            .join(Chapter, Chapter.id == CharacterSnapshot.chapter_id)
            .where(
                CharacterSnapshot.character_id == character.id,
                Chapter.chapter_number < chapter.chapter_number,
            )
            .order_by(Chapter.chapter_number.desc(), CharacterSnapshot.created_at.desc())
            .limit(1)
        )
        earlier = previous.get("changes", []) if isinstance(previous, dict) else []
        history = [item for item in earlier if isinstance(item, dict)]
        history += [{"chapter": chapter.chapter_number, "change": change} for change in changes]
        session.add(
            CharacterSnapshot(
                character_id=character.id,
                chapter_id=chapter.id,
                state={"latest_change": changes[-1], "changes": history[-CHARACTER_HISTORY_LIMIT:]},
            )
        )
        if (character.last_updated_chapter_number or 0) < chapter.chapter_number:
            character.last_updated_chapter_number = chapter.chapter_number


def persist_review_candidate(
    *,
    session,
    chapter_id: str,
    content: str,
    summary: str,
    review_score: float,
    generation_metadata: dict | None = None,
) -> dict:
    return _persist_unapproved_chapter(
        session=session, chapter_id=chapter_id, content=content, summary=summary,
        review_score=review_score, generation_metadata=generation_metadata,
        source="review_candidate", status="revision_pending",
    )


def persist_draft_chapter(
    *, session, chapter_id: str, content: str, summary: str,
    state_deltas: list[dict],
) -> dict:
    return _persist_unapproved_chapter(
        session=session, chapter_id=chapter_id, content=content, summary=summary,
        review_score=0.0, generation_metadata={"pending_state_deltas": state_deltas},
        source="draft", status="draft_saved",
    )


def _persist_unapproved_chapter(
    *, session, chapter_id: str, content: str, summary: str, review_score: float,
    generation_metadata: dict | None, source: str, status: str,
) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.id == chapter_id))
    if chapter is None:
        raise ValueError(f"Chapter not found: {chapter_id}")

    version = ChapterVersion(
        chapter_id=chapter_id,
        source=source,
        content=content,
        summary=summary,
        review_score=review_score,
        generation_metadata=generation_metadata or {},
    )
    session.add(version)
    session.flush()

    chapter.current_version_id = version.id
    chapter.status = status

    session.flush()
    session.refresh(version)
    return {
        "chapter_version_id": version.id,
        "db_saved": True,
        "publish_job_id": None,
    }
