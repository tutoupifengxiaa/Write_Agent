"""Cascade deletion of books and chapters, in foreign-key order."""

from __future__ import annotations

from sqlalchemy import delete, or_, select

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
from app.orchestrator.task_claims import utc_now


def has_active_work(session, chapter_ids: list[str]) -> bool:
    """True while a worker holds a live claim on a chapter or is uploading one of its publish jobs."""
    if not chapter_ids:
        return False
    claimed = session.scalar(select(Chapter.id).where(
        Chapter.id.in_(chapter_ids),
        Chapter.claim_token.is_not(None),
        or_(Chapter.lease_expires_at.is_(None), Chapter.lease_expires_at > utc_now()),
    ).limit(1))
    publishing = session.scalar(select(PublishJob.id).where(
        PublishJob.chapter_id.in_(chapter_ids), PublishJob.status == "publishing",
    ).limit(1))
    return claimed is not None or publishing is not None


def delete_chapters(session, chapter_ids: list[str]) -> None:
    """Delete chapters with their versions, events, snapshots, publish jobs and derived embeddings."""
    if not chapter_ids:
        return
    derived_ids = [
        select(ChapterVersion.id).where(ChapterVersion.chapter_id.in_(chapter_ids)),
        select(ContinuityEvent.id).where(ContinuityEvent.chapter_id.in_(chapter_ids)),
        select(CharacterSnapshot.id).where(CharacterSnapshot.chapter_id.in_(chapter_ids)),
    ]
    session.execute(delete(MemoryEmbedding).where(or_(*(MemoryEmbedding.source_id.in_(ids) for ids in derived_ids))))
    for model in (CharacterSnapshot, ContinuityEvent, PublishJob, ChapterVersion):
        session.execute(delete(model).where(model.chapter_id.in_(chapter_ids)))
    session.execute(delete(Chapter).where(Chapter.id.in_(chapter_ids)))


def delete_book(session, book_id: str) -> int:
    """Delete a book and everything that belongs to it; returns the number of chapters removed."""
    chapter_ids = list(session.scalars(select(Chapter.id).where(Chapter.book_id == book_id)))
    delete_chapters(session, chapter_ids)
    character_ids = select(Character.id).where(Character.book_id == book_id)
    session.execute(delete(CharacterSnapshot).where(CharacterSnapshot.character_id.in_(character_ids)))
    for model in (MemoryEmbedding, Character, StoryArc):
        session.execute(delete(model).where(model.book_id == book_id))
    session.execute(delete(Book).where(Book.id == book_id))
    return len(chapter_ids)
