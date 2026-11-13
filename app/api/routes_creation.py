from __future__ import annotations

import json
from typing import Annotated, Any, Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.agents.persister import persist_draft_chapter, persist_review_candidate
from app.agents.context_builder import build_chapter_context_from_memory
from app.db.repositories import ChapterRepository
from app.api.dependencies import get_session
from app.services.llm_client import LLMConfigurationError
from sqlalchemy.orm import Session

from app.ui.creation_page import render_creation_page
from app.ui.ops_page import render_ops_page


router = APIRouter()
DraftTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class CreationGenerateRequest(BaseModel):
    chapter_id: str | None = None
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
    if payload.create_publish_job:
        raise HTTPException(status_code=409, detail="请先提交审核，批准后创建发布任务")
    try:
        result = persist_draft_chapter(
            session=session,
            chapter_id=payload.chapter_id,
            content=payload.content,
            summary=payload.summary,
            state_deltas=payload.state_deltas,
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
        "status": "draft_saved",
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
    session: Session = Depends(get_session),
) -> CreationGenerateResponse:
    writer = getattr(request.app.state, "creation_writer", None)
    if writer is None:
        raise HTTPException(status_code=503, detail="creation writer is not configured")

    try:
        result = writer(_build_writer_context(payload, session=session, request=request))
    except Exception as exc:
        mapped = _map_llm_error(exc)
        if mapped is None:
            raise
        status_code, detail = mapped
        raise HTTPException(status_code=status_code, detail=detail) from exc

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
    session: Session = Depends(get_session),
) -> StreamingResponse:
    writer = getattr(request.app.state, "creation_writer", None)
    stream_writer = getattr(request.app.state, "creation_stream_writer", None)
    if writer is None and stream_writer is None:
        raise HTTPException(status_code=503, detail="creation writer is not configured")

    return StreamingResponse(
        _stream_chapter_events(
            writer=writer,
            stream_writer=stream_writer,
            context=_build_writer_context(payload, session=session, request=request),
            fallback_title=payload.chapter_title,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _build_writer_context(payload: CreationGenerateRequest, *, session=None, request=None) -> dict:
    context = {
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
    if payload.chapter_id is not None:
        if session is None or ChapterRepository(session).get_chapter(payload.chapter_id) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        overrides = dict(context["chapter_goal"])
        if "style_rules" not in payload.model_fields_set:
            overrides.pop("style_rules")
        options = getattr(request.app.state, "creation_context_options", {}) if request else {}
        context = build_chapter_context_from_memory(
            session=session, chapter_id=payload.chapter_id,
            chapter_goal_overrides=overrides, **options,
        )
        context["chapter_title"] = payload.chapter_title
        # Index changes must not hold a write lock while the model is running.
        session.commit()
    return context


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
            yield _sse_event("title", {"value": fallback_title})
            yield _sse_event("status", {"stage": "writing"})
            for item in stream_writer(context):
                event = item["event"]
                yield _sse_event(event, item["data"])
                if event in {"done", "error"}:
                    return
            yield _sse_event("error", {"detail": "模型输出提前结束", "status_code": 502})
            return
        result = writer(context)
        yield _sse_event("title", {"value": result.get("title") or fallback_title})
        yield _sse_event("status", {"stage": "writing"})
        for chunk in _iter_content_chunks(result.get("content") or ""):
            yield _sse_event("delta", {"value": chunk})
        yield _sse_event("summary", {"value": result.get("summary") or ""})
        yield _sse_event("done", {"ok": True, "state_deltas": result.get("state_deltas") or []})
    except Exception as exc:
        mapped = _map_llm_error(exc)
        if mapped is None:
            raise
        status_code, detail = mapped
        yield _sse_event("error", {"detail": detail, "status_code": status_code})


def _map_llm_error(exc: Exception) -> tuple[int, str] | None:
    if isinstance(exc, LLMConfigurationError):
        return 503, str(exc)
    if isinstance(exc, httpx.ReadTimeout):
        return 504, "模型服务响应超时，请稍后重试"
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return 502, "模型服务暂时不可用，请稍后重试"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if 500 <= status_code < 600:
            return 502, f"模型服务暂时不可用：{status_code} {exc.response.reason_phrase}"
        return 502, f"模型服务请求失败：{status_code} {exc.response.reason_phrase}"
    return None


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
