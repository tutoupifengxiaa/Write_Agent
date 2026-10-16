from __future__ import annotations

import json
from typing import Annotated, Any, Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.agents.persister import persist_approved_chapter, persist_review_candidate
from app.db.repositories import ChapterRepository
from app.api.dependencies import get_session
from sqlalchemy.orm import Session

from app.ui.creation_page import render_creation_page
from app.ui.ops_page import render_ops_page


router = APIRouter()
DraftTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class CreationGenerateRequest(BaseModel):
    chapter_title: str
    chapter_goal: str
    must_include: str = ""
    must_avoid: str = ""
    style_rules: str = ""
    target_words: int = Field(default=2500, gt=0, le=50000)


class CreationGenerateResponse(BaseModel):
    title: str
    content: str
    summary: str
    state_deltas: list[dict[str, Any]]


class CreationSaveRequest(BaseModel):
    chapter_id: str
    content: str
    summary: str = ""
    review_score: float = 0.0
    state_deltas: list[dict[str, Any]] = []
    create_publish_job: bool = False
    title: DraftTitle | None = None


class CreationManualReviewRequest(BaseModel):
    chapter_id: str
    review_action: str
    note: str = ""
    content: str | None = None
    summary: str = ""
    title: DraftTitle | None = None
    state_deltas: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def nonempty_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("送审正文不能为空")
        return value


@router.get("/creation", response_class=HTMLResponse)
def creation_page() -> HTMLResponse:
    return HTMLResponse(render_creation_page())


@router.get("/ops", response_class=HTMLResponse)
def ops_page() -> HTMLResponse:
    return HTMLResponse(render_ops_page())


@router.post("/api/creation/save")
def save_chapter_draft(
    payload: CreationSaveRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        result = persist_approved_chapter(
            session=session,
            chapter_id=payload.chapter_id,
            content=payload.content,
            summary=payload.summary,
            review_score=payload.review_score,
            state_deltas=payload.state_deltas,
            create_publish_job=payload.create_publish_job,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.title is not None and payload.title.strip():
        ChapterRepository(session).get_chapter(payload.chapter_id).title = payload.title.strip()
    session.commit()
    return {
        "chapter_id": payload.chapter_id,
        "chapter_version_id": result["chapter_version_id"],
        "publish_job_id": result["publish_job_id"],
        "status": "persisted",
    }


@router.post("/api/creation/review")
def mark_chapter_for_manual_review(
    payload: CreationManualReviewRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if payload.review_action not in {"review_pending", "revision_pending"}:
        raise HTTPException(status_code=400, detail="invalid review action")

    repo = ChapterRepository(session)
    chapter = repo.get_chapter(payload.chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if payload.content is not None:
        persist_review_candidate(
            session=session,
            chapter_id=payload.chapter_id,
            content=payload.content,
            summary=payload.summary,
            review_score=0.0,
            generation_metadata={"pending_state_deltas": payload.state_deltas},
        )
    if payload.title is not None and payload.title.strip():
        chapter.title = payload.title.strip()
    chapter = repo.update_manual_review(
        payload.chapter_id,
        status=payload.review_action,
        note=payload.note.strip(),
    )
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")

    session.commit()
    return {
        "chapter_id": chapter.id,
        "status": chapter.status,
        "note": (chapter.goal or {}).get("manual_review_note", ""),
        "chapter_version_id": chapter.current_version_id,
    }


@router.post("/api/creation/generate", response_model=CreationGenerateResponse)
def generate_chapter(
    payload: CreationGenerateRequest,
    request: Request,
) -> CreationGenerateResponse:
    writer = getattr(request.app.state, "creation_writer", None)
    if writer is None:
        raise HTTPException(status_code=503, detail="creation writer is not configured")

    try:
        result = writer(_build_writer_context(payload))
    except ValueError as exc:
        if str(exc) == "OPENAI_API_KEY is required":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if 500 <= status_code < 600:
            raise HTTPException(
                status_code=502,
                detail=f"模型服务暂时不可用：{status_code} {exc.response.reason_phrase}",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"模型服务请求失败：{status_code} {exc.response.reason_phrase}",
        ) from exc
    except httpx.ReadTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="模型服务响应超时，请稍后重试",
        ) from exc

    return CreationGenerateResponse(
        title=result.get("title") or payload.chapter_title,
        content=result["content"],
        summary=result.get("summary") or "",
        state_deltas=result.get("state_deltas") or [],
    )


@router.post("/api/creation/generate-stream")
def generate_chapter_stream(
    payload: CreationGenerateRequest,
    request: Request,
) -> StreamingResponse:
    writer = getattr(request.app.state, "creation_writer", None)
    stream_writer = getattr(request.app.state, "creation_stream_writer", None)
    if writer is None:
        raise HTTPException(status_code=503, detail="creation writer is not configured")

    return StreamingResponse(
        _stream_chapter_events(
            writer=writer,
            stream_writer=stream_writer,
            context=_build_writer_context(payload),
            fallback_title=payload.chapter_title,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _build_writer_context(payload: CreationGenerateRequest) -> dict:
    return {
        "chapter_title": payload.chapter_title,
        "chapter_goal": {
            "objective": payload.chapter_goal,
            "must_include": _split_lines(payload.must_include),
            "must_avoid": _split_lines(payload.must_avoid),
            "target_words": payload.target_words,
            "style_rules": payload.style_rules,
        },
        "world_state": {},
        "character_states": [],
        "recent_summaries": [],
    }


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _stream_chapter_events(
    *,
    writer: Any,
    stream_writer: Any,
    context: dict,
    fallback_title: str,
) -> Iterable[str]:
    yield _sse_event("status", {"stage": "started"})
    try:
        if stream_writer is not None:
            yielded_done = False
            yielded_title = False
            yielded_summary = False
            yield _sse_event("title", {"value": fallback_title})
            yielded_title = True
            yield _sse_event("status", {"stage": "writing"})
            for item in stream_writer(context):
                event = item.get("event")
                data = item.get("data", {})
                if event == "title":
                    yielded_title = True
                if event == "summary":
                    yielded_summary = True
                if event == "done":
                    yielded_done = True
                yield _sse_event(event, data)
            if not yielded_summary:
                yield _sse_event("summary", {"value": ""})
            if not yielded_done:
                yield _sse_event("done", {"ok": True, "state_deltas": []})
            return
        result = writer(context)
        yield _sse_event("title", {"value": result.get("title") or fallback_title})
        yield _sse_event("status", {"stage": "writing"})
        for chunk in _iter_content_chunks(result.get("content") or ""):
            yield _sse_event("delta", {"value": chunk})
        yield _sse_event("summary", {"value": result.get("summary") or ""})
        yield _sse_event("done", {"ok": True, "state_deltas": result.get("state_deltas") or []})
    except ValueError as exc:
        detail = str(exc)
        if detail == "OPENAI_API_KEY is required":
            yield _sse_event("error", {"detail": detail, "status_code": 503})
            return
        raise
    except httpx.ReadTimeout:
        yield _sse_event("error", {"detail": "模型服务响应超时，请稍后重试", "status_code": 504})
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if 500 <= status_code < 600:
            detail = f"模型服务暂时不可用：{status_code} {exc.response.reason_phrase}"
        else:
            detail = f"模型服务请求失败：{status_code} {exc.response.reason_phrase}"
        yield _sse_event("error", {"detail": detail, "status_code": 502})


def _iter_content_chunks(content: str) -> Iterable[str]:
    paragraphs = content.split("\n\n")
    for index, paragraph in enumerate(paragraphs):
        if not paragraph:
            continue
        suffix = "\n\n" if index < len(paragraphs) - 1 else ""
        yield f"{paragraph}{suffix}"


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n"
