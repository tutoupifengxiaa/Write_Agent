from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.planner import plan_next_chapter
from app.agents.story_arcs import arcs_covering
from app.api.dependencies import get_session
from app.db.deletion import delete_chapters, has_active_work
from app.db.models import Chapter, ChapterVersion, PublishJob
from app.db.repositories import BookRepository, ChapterRepository, MemoryRepository, PublishJobRepository
from app.ui.chapter_detail_page import render_chapter_detail_page

router = APIRouter()
ChapterTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class ChapterGateRequest(BaseModel):
    action: str
    version_id: str | None = None


class ChapterCreateRequest(BaseModel):
    book_id: str
    chapter_number: int = Field(gt=0)
    title: ChapterTitle
    goal: dict


class ChapterPlanRequest(BaseModel):
    book_id: str
    chapter_number: int = Field(gt=0)
    title: ChapterTitle
    chapter_goal: str
    must_include: list[str] = []
    must_avoid: list[str] = []
    target_words: int = Field(default=2500, gt=0, le=50000)


def _ensure_chapter_slot_free(session: Session, book_id: str, chapter_number: int) -> None:
    """New chapters need an existing book and a chapter number not yet used in it."""
    if BookRepository(session).get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="book not found")
    if session.scalar(select(Chapter.id).where(
        Chapter.book_id == book_id, Chapter.chapter_number == chapter_number,
    ).limit(1)):
        raise HTTPException(status_code=409, detail=f"第 {chapter_number} 章已存在，请换一个章节号")


@router.post("/chapters", status_code=201)
def create_chapter(
    payload: ChapterCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    _ensure_chapter_slot_free(session, payload.book_id, payload.chapter_number)
    repo = ChapterRepository(session)
    chapter = repo.create_planned_chapter(**payload.model_dump())
    session.commit()
    session.refresh(chapter)
    return {
        "id": chapter.id,
        "book_id": chapter.book_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "status": chapter.status,
    }


@router.post("/chapters/plan", status_code=201)
def plan_chapter(
    payload: ChapterPlanRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    _ensure_chapter_slot_free(session, payload.book_id, payload.chapter_number)
    chapter_repo = ChapterRepository(session)
    # Same view the writer gets: arcs covering this chapter, character states from earlier chapters.
    memory_repo = MemoryRepository(session)
    story_arcs = arcs_covering(memory_repo.list_story_arcs(payload.book_id), payload.chapter_number)
    goal = plan_next_chapter(
        chapter_number=payload.chapter_number,
        chapter_goal=payload.chapter_goal,
        must_include=payload.must_include,
        must_avoid=payload.must_avoid,
        target_words=payload.target_words,
        story_arcs=[{"arc_type": arc.arc_type, "stage_goal": arc.stage_goal} for arc in story_arcs],
        character_states=memory_repo.list_latest_character_states(
            payload.book_id, before_chapter_number=payload.chapter_number,
        ),
    )
    chapter = chapter_repo.create_planned_chapter(
        book_id=payload.book_id,
        chapter_number=payload.chapter_number,
        title=payload.title,
        goal=goal,
    )
    session.commit()
    session.refresh(chapter)
    return {
        "id": chapter.id,
        "book_id": chapter.book_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "status": chapter.status,
        "goal": chapter.goal,
    }


@router.post("/books/{book_id}/chapters/from-outline", status_code=201)
def create_chapters_from_outline(
    book_id: str,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    book_repo = BookRepository(session)
    chapter_repo = ChapterRepository(session)

    book = book_repo.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")

    outline = book.book_outline or {}
    if not outline:
        raise HTTPException(status_code=400, detail="book outline not found")

    seed_plan = outline.get("chapter_seed_plan")
    if not isinstance(seed_plan, list) or not seed_plan:
        raise HTTPException(status_code=400, detail="chapter seed plan not found")

    context = book_repo.get_next_chapter_context(book_id)
    # Running this again must not duplicate chapters: numbers already in the book are skipped.
    taken_numbers = set(session.scalars(select(Chapter.chapter_number).where(Chapter.book_id == book_id)))
    created_items: list[dict[str, object]] = []
    skipped_numbers: list[int] = []
    for seed in seed_plan:
        chapter_number = seed.get("chapter")
        chapter_goal = seed.get("goal")
        if not isinstance(chapter_number, int) or not isinstance(chapter_goal, str) or not chapter_goal.strip():
            continue
        if chapter_number in taken_numbers:
            skipped_numbers.append(chapter_number)
            continue
        taken_numbers.add(chapter_number)

        title = seed.get("title")
        if not isinstance(title, str) or not title.strip():
            title = f"第{chapter_number}章"

        must_include = seed.get("must_include")
        if not isinstance(must_include, list):
            must_include = []

        must_avoid = seed.get("must_avoid")
        if not isinstance(must_avoid, list):
            must_avoid = []

        target_words = seed.get("target_words")
        if not isinstance(target_words, int):
            target_words = 2500

        goal = plan_next_chapter(
            chapter_number=chapter_number,
            chapter_goal=chapter_goal,
            must_include=[item for item in must_include if isinstance(item, str)],
            must_avoid=[item for item in must_avoid if isinstance(item, str)],
            target_words=target_words,
            story_arcs=context["story_arcs"],
            character_states=context["character_states"],
        )
        goal["planning_trace"] = {
            "direction_source": outline.get("direction_source"),
            "outline_seed": seed,
        }
        chapter = chapter_repo.create_planned_chapter(
            book_id=book_id,
            chapter_number=chapter_number,
            title=title,
            goal=goal,
        )
        created_items.append(
            {
                "id": chapter.id,
                "book_id": chapter.book_id,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "status": chapter.status,
                "goal": chapter.goal,
            }
        )

    if not created_items:
        if skipped_numbers:
            raise HTTPException(status_code=409, detail="大纲里的章节都已存在，没有新的章节需要创建")
        raise HTTPException(status_code=400, detail="chapter seed plan not found")

    session.commit()
    return {
        "book_id": book_id,
        "created_count": len(created_items),
        "items": created_items,
        "skipped_chapter_numbers": skipped_numbers,
    }


@router.get("/books/{book_id}/chapters")
def list_book_chapters(
    book_id: str,
    session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    repo = ChapterRepository(session)
    chapters = repo.list_book_chapters(book_id)
    return [
        {
            "id": chapter.id,
            "book_id": chapter.book_id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status,
            "current_version_id": chapter.current_version_id,
            "error_message": chapter.error_message,
        }
        for chapter in chapters
    ]


@router.get("/chapters/manual-review-queue")
def list_manual_review_queue(
    session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    repo = ChapterRepository(session)
    return repo.list_manual_review_queue()


@router.get("/chapters/{chapter_id}/detail", response_class=HTMLResponse)
def chapter_detail_page(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    chapter_repo = ChapterRepository(session)
    book_repo = BookRepository(session)
    publish_repo = PublishJobRepository(session)

    chapter = chapter_repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")

    book = book_repo.get_book(chapter.book_id)
    latest_job = publish_repo.get_latest_for_chapter(chapter_id)
    versions = chapter_repo.list_chapter_versions(chapter_id)
    latest_version = versions[0] if versions else None
    publish_jobs = publish_repo.list_jobs(chapter_id=chapter_id)

    detail = {
        "chapter": {
            "id": chapter.id,
            "book_id": chapter.book_id,
            "book_title": book.title if book else chapter.book_id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status,
            "goal": chapter.goal or {},
        },
        "latest_job": None
        if latest_job is None
        else {
            "id": latest_job.id,
            "status": latest_job.status,
            "platform": latest_job.platform,
            "error_message": latest_job.error_message,
        },
        "versions": [
            {
                "id": version.id,
                "summary": version.summary,
                "content": version.content,
                "review_score": version.review_score,
                "generation_metadata": version.generation_metadata or {},
                "created_at": version.created_at.isoformat() if version.created_at is not None else None,
            }
            for version in versions
        ],
        "publish_jobs": [
            {
                "id": job.id,
                "status": job.status,
                "platform": job.platform,
                "mode": job.mode,
                "error_message": job.error_message,
            }
            for job in publish_jobs
        ],
        "latest_version": None
        if latest_version is None
        else {
            "summary": latest_version.summary,
            "content": latest_version.content,
            "review_score": latest_version.review_score,
            "generation_metadata": latest_version.generation_metadata or {},
        },
    }
    return HTMLResponse(render_chapter_detail_page(detail))


@router.get("/chapters/{chapter_id}")
def get_chapter(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    repo = ChapterRepository(session)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return {
        "id": chapter.id,
        "book_id": chapter.book_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "goal": chapter.goal,
        "status": chapter.status,
        "current_version_id": chapter.current_version_id,
    }


@router.get("/chapters/{chapter_id}/versions")
def list_chapter_versions(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> list[dict[str, object]]:
    repo = ChapterRepository(session)
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")

    versions = repo.list_chapter_versions(chapter_id)
    if chapter.current_version_id:
        versions.sort(key=lambda item: item.id != chapter.current_version_id)
    return [
        {
            "id": version.id,
            "chapter_id": version.chapter_id,
            "summary": version.summary,
            "content": version.content,
            "review_score": version.review_score,
            "generation_metadata": version.generation_metadata or {},
            "state_deltas": (version.generation_metadata or {}).get("pending_state_deltas", []),
            "created_at": version.created_at.isoformat() if version.created_at is not None else None,
        }
        for version in versions
    ]


@router.post("/chapters/{chapter_id}/gate")
def gate_chapter(
    chapter_id: str,
    payload: ChapterGateRequest,
    session: Session = Depends(get_session),
) -> dict:
    from app.api.routes_review_center import (
        ReviewCenterApproveRequest,
        ReviewCenterReviseRequest,
        approve_manual_review_chapter,
        return_chapter_to_revision_pending,
    )

    if payload.action == "approve":
        result = approve_manual_review_chapter(ReviewCenterApproveRequest(chapter_id=chapter_id, version_id=payload.version_id), session)
    elif payload.action == "revise":
        result = return_chapter_to_revision_pending(ReviewCenterReviseRequest(chapter_id=chapter_id), session)
    else:
        raise HTTPException(status_code=400, detail="invalid gate action")
    return {**result, "action": payload.action}


@router.post("/chapters/{chapter_id}/retry-publish", status_code=202)
def retry_publish(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    repo = PublishJobRepository(session)
    job = repo.get_latest_for_chapter(chapter_id)
    if job is None:
        raise HTTPException(status_code=404, detail="publish job not found")
    version = session.get(ChapterVersion, job.chapter_version_id) if job.chapter_version_id else None
    if version is None or version.chapter_id != chapter_id or version.source != "approved" or not job.chapter_title:
        raise HTTPException(status_code=409, detail="发布任务未绑定有效审核版本，请重新审核并创建任务")
    result = session.execute(update(PublishJob).where(
        PublishJob.id == job.id, PublishJob.status == "publish_failed",
        PublishJob.claim_token.is_(None),
    ).values(status="publish_pending", error_message=None, attempt_count=0,
             next_attempt_at=None, lease_expires_at=None))
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="只有失败且未执行中的发布任务可以重试")
    session.commit()
    return {"chapter_id": chapter_id, "job_id": job.id, "status": job.status}


class ChapterUpdateRequest(BaseModel):
    title: ChapterTitle | None = None
    chapter_number: int | None = Field(default=None, gt=0)


@router.patch("/chapters/{chapter_id}")
def update_chapter(
    chapter_id: str,
    payload: ChapterUpdateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    chapter = ChapterRepository(session).get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if has_active_work(session, [chapter_id]):
        raise HTTPException(status_code=409, detail="章节正在后台生成或发布，请稍后再修改")
    number = payload.chapter_number
    if number is not None and number != chapter.chapter_number and session.scalar(
        select(Chapter.id).where(Chapter.book_id == chapter.book_id, Chapter.chapter_number == number).limit(1)
    ):
        raise HTTPException(status_code=409, detail=f"第 {number} 章已存在，请先调整那一章的章节号")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(chapter, field, value)
    session.commit()
    return {"id": chapter.id, "book_id": chapter.book_id, "chapter_number": chapter.chapter_number,
            "title": chapter.title, "status": chapter.status}


@router.delete("/chapters/{chapter_id}")
def remove_chapter(chapter_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    if ChapterRepository(session).get_chapter(chapter_id) is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if has_active_work(session, [chapter_id]):
        raise HTTPException(status_code=409, detail="章节正在后台生成或发布，请稍后再删除")
    delete_chapters(session, [chapter_id])
    session.commit()
    return {"chapter_id": chapter_id, "status": "deleted"}


@router.post("/chapters/{chapter_id}/requeue-generation", status_code=202)
def requeue_generation(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    if ChapterRepository(session).get_chapter(chapter_id) is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    # Only a failed chapter that no worker holds may go back; reset its retry budget.
    result = session.execute(update(Chapter).where(
        Chapter.id == chapter_id, Chapter.status == "generation_failed",
        Chapter.claim_token.is_(None),
    ).values(status="draft_planned", error_message=None, attempt_count=0,
             next_attempt_at=None, lease_expires_at=None))
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="只有生成失败且未执行中的章节可以重新排队")
    session.commit()
    return {"chapter_id": chapter_id, "status": "draft_planned"}


@router.post("/chapters/{chapter_id}/archive-publish-failure", status_code=202)
def archive_publish_failure(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    repo = PublishJobRepository(session)
    job = repo.get_latest_for_chapter(chapter_id)
    if job is None:
        raise HTTPException(status_code=404, detail="publish job not found")
    result = session.execute(update(PublishJob).where(
        PublishJob.id == job.id, PublishJob.status == "publish_failed",
        PublishJob.claim_token.is_(None),
    ).values(status="archived"))
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="只有失败且未执行中的发布任务可以归档")
    session.commit()
    return {"chapter_id": chapter_id, "job_id": job.id, "status": job.status}
