from __future__ import annotations

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.story_arcs import sync_story_arcs
from app.api.dependencies import get_session
from app.db.repositories import BookRepository, TrendSnapshotRepository

router = APIRouter()


class BookCreateRequest(BaseModel):
    id: str
    title: str
    platform: str
    genre: str
    logline: str | None = None
    book_outline: dict | None = None
    style_rules: dict | None = None


class BookOutlineUpdateRequest(BaseModel):
    book_outline: dict | None = None
    logline: str | None = None
    style_rules: dict | None = None


@router.get("/books")
def list_books(session: Session = Depends(get_session)) -> list[dict]:
    repo = BookRepository(session)
    books = repo.list_books()
    return [
        {
            "id": book.id,
            "title": book.title,
            "platform": book.platform,
            "genre": book.genre,
            "status": book.status,
        }
        for book in books
    ]


@router.post("/books", status_code=201)
def create_book(
    payload: BookCreateRequest,
    session: Session = Depends(get_session),
) -> dict:
    repo = BookRepository(session)
    values = payload.model_dump()
    values["book_outline"] = _canonicalize_outline_direction_source(
        session=session,
        book_outline=payload.book_outline,
    )
    book = repo.create_book(**values)
    sync_story_arcs(session, book)
    session.commit()
    return {
        "id": book.id,
        "title": book.title,
        "platform": book.platform,
        "genre": book.genre,
        "status": book.status,
    }


@router.get("/books/{book_id}")
def get_book(book_id: str, session: Session = Depends(get_session)) -> dict:
    repo = BookRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return {
        "id": book.id,
        "title": book.title,
        "platform": book.platform,
        "genre": book.genre,
        "logline": book.logline,
        "book_outline": book.book_outline,
        "style_rules": book.style_rules,
        "status": book.status,
    }


@router.put("/books/{book_id}/outline")
def update_book_outline(
    book_id: str,
    payload: BookOutlineUpdateRequest,
    session: Session = Depends(get_session),
) -> dict:
    repo = BookRepository(session)
    if repo.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="book not found")
    book = repo.update_outline(
        book_id,
        book_outline=_canonicalize_outline_direction_source(
            session=session,
            book_outline=payload.book_outline,
        ),
        logline=payload.logline,
        style_rules=payload.style_rules,
    )
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    sync_story_arcs(session, book)
    session.commit()
    return {
        "id": book.id,
        "title": book.title,
        "platform": book.platform,
        "genre": book.genre,
        "logline": book.logline,
        "book_outline": book.book_outline,
        "style_rules": book.style_rules,
        "status": book.status,
    }


def _canonicalize_outline_direction_source(
    *,
    session: Session,
    book_outline: dict | None,
) -> dict | None:
    if book_outline is None or "direction_source" not in book_outline:
        return book_outline

    outline = deepcopy(book_outline)
    raw_trace = outline["direction_source"]
    if not isinstance(raw_trace, dict):
        raise HTTPException(status_code=400, detail="invalid direction_source")

    capture_id = raw_trace.get("capture_id")
    selected_item_ids = raw_trace.get("selected_item_ids", [])
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise HTTPException(status_code=400, detail="invalid direction_source capture_id")
    capture_id = capture_id.strip()
    if (
        not isinstance(selected_item_ids, list)
        or any(
            not isinstance(item_id, str) or not item_id.strip()
            for item_id in selected_item_ids
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="invalid direction_source selected_item_ids",
        )
    selected_item_ids = [item_id.strip() for item_id in selected_item_ids]
    if len(set(selected_item_ids)) != len(selected_item_ids):
        raise HTTPException(
            status_code=400,
            detail="invalid direction_source selected_item_ids",
        )

    snapshots = TrendSnapshotRepository(session).list_snapshots(capture_id=capture_id)
    if not snapshots:
        raise HTTPException(status_code=400, detail="trend capture not found")

    real_item_ids = {
        item["id"]
        for snapshot in snapshots
        for item in snapshot.items
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].strip()
    }
    unknown_item_ids = [
        item_id for item_id in selected_item_ids if item_id not in real_item_ids
    ]
    if unknown_item_ids:
        raise HTTPException(
            status_code=400,
            detail=f"selected trend item not found in capture: {unknown_item_ids[0]}",
        )

    captured_at = max(
        (snapshot.captured_at for snapshot in snapshots),
        default=None,
    )
    outline["direction_source"] = {
        "capture_id": capture_id,
        "snapshot_ids": [snapshot.id for snapshot in snapshots],
        "providers": [snapshot.provider for snapshot in snapshots],
        "selected_item_ids": selected_item_ids,
        "captured_at": captured_at.isoformat() if captured_at else None,
    }
    return outline
