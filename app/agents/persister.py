from __future__ import annotations

from sqlalchemy import select

from app.db.models import Chapter, ChapterVersion, ContinuityEvent
from app.db.repositories import PublishJobRepository


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
