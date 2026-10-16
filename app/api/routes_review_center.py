from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.persister import persist_approved_chapter
from app.api.dependencies import get_session
from app.db.models import ChapterVersion
from app.db.repositories import ChapterRepository
from app.ui.review_center_page import render_review_center_page


router = APIRouter()


class ReviewCenterApproveRequest(BaseModel):
    chapter_id: str
    note: str = ""
    create_publish_job: bool = False


class ReviewCenterReviseRequest(BaseModel):
    chapter_id: str
    note: str = ""


@router.get("/review-center", response_class=HTMLResponse)
def review_center_page() -> HTMLResponse:
    return HTMLResponse(render_review_center_page())


@router.post("/api/review-center/approve")
def approve_manual_review_chapter(
    payload: ReviewCenterApproveRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    repo = ChapterRepository(session)
    chapter = repo.get_chapter(payload.chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if chapter.status not in {"review_pending", "revision_pending"}:
        raise HTTPException(
            status_code=409,
            detail="chapter is not pending manual review",
        )

    latest_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.id == chapter.current_version_id)
        .where(ChapterVersion.chapter_id == payload.chapter_id)
    )
    if latest_version is None or not latest_version.content:
        raise HTTPException(status_code=400, detail="chapter has no saved draft")

    repo.update_goal_fields(
        payload.chapter_id,
        fields={
            "manual_review_note": payload.note.strip(),
            "manual_review_action": "approved",
        },
        status="approved",
    )

    metadata = dict(latest_version.generation_metadata or {})
    pending_deltas = metadata.pop("pending_state_deltas", [])
    result = persist_approved_chapter(
        session=session,
        chapter_id=payload.chapter_id,
        content=latest_version.content or "",
        summary=latest_version.summary or "",
        review_score=latest_version.review_score or 0.0,
        state_deltas=pending_deltas,
        generation_metadata={
            **metadata,
            "manual_review": {
                "action": "approved",
                "note": payload.note.strip(),
            },
        },
        create_publish_job=payload.create_publish_job,
    )
    session.commit()
    return {
        "chapter_id": payload.chapter_id,
        "chapter_version_id": result["chapter_version_id"],
        "publish_job_id": result["publish_job_id"],
        "status": "persisted",
    }


@router.post("/api/review-center/revise")
def return_chapter_to_revision_pending(
    payload: ReviewCenterReviseRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    repo = ChapterRepository(session)
    chapter = repo.get_chapter(payload.chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if chapter.status not in {"review_pending", "revision_pending"}:
        raise HTTPException(
            status_code=409,
            detail="chapter is not pending manual review",
        )

    chapter = repo.update_goal_fields(
        payload.chapter_id,
        fields={
            "manual_review_note": payload.note.strip(),
            "manual_review_action": "revision_pending",
        },
        status="revision_pending",
    )
    session.commit()
    return {
        "chapter_id": chapter.id,
        "status": chapter.status,
        "note": (chapter.goal or {}).get("manual_review_note", ""),
    }
