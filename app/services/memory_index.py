from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, select

from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    Character,
    CharacterSnapshot,
    ContinuityEvent,
    MemoryEmbedding,
    StoryArc,
)
from app.db.repositories import MemoryEmbeddingRepository
from app.services.embedding import cosine_similarity

SOURCE_WEIGHTS = {
    "chapter_summary": 1.0,
    "continuity_event": 0.95,
    "character_snapshot": 0.9,
    "story_arc": 0.85,
    "book_outline": 0.8,
}
RECENT_CHAPTER_WINDOW = 32
MAX_CANDIDATES_PER_TYPE = 64


@dataclass(frozen=True)
class MemorySource:
    book_id: str
    source_type: str
    source_id: str
    source_chapter_number: int | None
    text: str


def collect_memory_sources(
    *,
    session,
    book_id: str,
    target_chapter_number: int,
    for_retrieval: bool = False,
) -> list[MemorySource]:
    sources: list[MemorySource] = []
    book = session.scalar(select(Book).where(Book.id == book_id))
    if book is None:
        raise ValueError(f"Book not found: {book_id}")
    if book.book_outline:
        sources.append(
            MemorySource(
                book_id,
                "book_outline",
                book.id,
                None,
                json.dumps(book.book_outline, ensure_ascii=False, sort_keys=True),
            )
        )

    versions = (
        select(ChapterVersion, Chapter)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .where(Chapter.book_id == book_id)
        .where(Chapter.chapter_number < target_chapter_number)
        .where(ChapterVersion.id == Chapter.approved_version_id)
        .where(ChapterVersion.source == "approved")
        .where(ChapterVersion.summary.is_not(None))
    )
    if for_retrieval:
        versions = versions.where(Chapter.chapter_number >= target_chapter_number - RECENT_CHAPTER_WINDOW)
        versions = versions.order_by(Chapter.chapter_number.desc(), ChapterVersion.id).limit(MAX_CANDIDATES_PER_TYPE)
    else:
        versions = versions.order_by(Chapter.chapter_number, ChapterVersion.id)
    for version, chapter in session.execute(versions):
        sources.append(
            MemorySource(
                book_id,
                "chapter_summary",
                version.id,
                chapter.chapter_number,
                version.summary or "",
            )
        )

    arcs = select(StoryArc).where(StoryArc.book_id == book_id)
    if for_retrieval:
        arcs = arcs.order_by(StoryArc.chapter_start.desc(), StoryArc.id).limit(MAX_CANDIDATES_PER_TYPE)
    else:
        arcs = arcs.order_by(StoryArc.chapter_start, StoryArc.id)
    for arc in session.scalars(arcs):
        text = json.dumps(
            {"stage_goal": arc.stage_goal, "outline": arc.outline or {}},
            ensure_ascii=False,
            sort_keys=True,
        )
        sources.append(MemorySource(book_id, "story_arc", arc.id, None, text))

    snapshots = (
        select(CharacterSnapshot, Character, Chapter)
        .join(Character, Character.id == CharacterSnapshot.character_id)
        .join(Chapter, Chapter.id == CharacterSnapshot.chapter_id)
        .where(Character.book_id == book_id)
        .where(Chapter.book_id == book_id)
        .where(Chapter.chapter_number < target_chapter_number)
    )
    if for_retrieval:
        # Keep every character's latest state; a character absent for many chapters must not drop out.
        snapshot_rank = func.row_number().over(
            partition_by=CharacterSnapshot.character_id,
            order_by=(Chapter.chapter_number.desc(), CharacterSnapshot.id.desc()),
        ).label("snapshot_rank")
        latest = snapshots.with_only_columns(CharacterSnapshot.id, snapshot_rank).subquery()
        snapshots = (
            snapshots.join(latest, latest.c.id == CharacterSnapshot.id)
            .where(latest.c.snapshot_rank == 1)
            .order_by(Chapter.chapter_number.desc(), CharacterSnapshot.id)
        )
    else:
        snapshots = snapshots.order_by(Chapter.chapter_number, CharacterSnapshot.id)
    for snapshot, character, chapter in session.execute(snapshots):
        text = json.dumps(
            {"name": character.name, "state": snapshot.state},
            ensure_ascii=False,
            sort_keys=True,
        )
        sources.append(
            MemorySource(
                book_id,
                "character_snapshot",
                snapshot.id,
                chapter.chapter_number,
                text,
            )
        )

    events = (
        select(ContinuityEvent, Chapter)
        .join(Chapter, Chapter.id == ContinuityEvent.chapter_id)
        .join(ChapterVersion, ChapterVersion.id == Chapter.approved_version_id)
        .where(ChapterVersion.chapter_id == Chapter.id)
        .where(ChapterVersion.source == "approved")
        .where(ContinuityEvent.chapter_version_id == Chapter.approved_version_id)
        .where(Chapter.book_id == book_id)
        .where(Chapter.chapter_number < target_chapter_number)
    )
    if for_retrieval:
        events = events.where(Chapter.chapter_number >= target_chapter_number - RECENT_CHAPTER_WINDOW)
        events = events.order_by(Chapter.chapter_number.desc(), ContinuityEvent.id).limit(MAX_CANDIDATES_PER_TYPE)
    else:
        events = events.order_by(Chapter.chapter_number, ContinuityEvent.id)
    for event, chapter in session.execute(events):
        text = json.dumps(event.delta, ensure_ascii=False, sort_keys=True)
        sources.append(
            MemorySource(
                book_id,
                "continuity_event",
                event.id,
                chapter.chapter_number,
                text,
            )
        )

    return [source for source in sources if source.text.strip()]


def refresh_memory_index(
    *,
    session,
    book_id: str,
    target_chapter_number: int,
    embedding_client,
    model_name: str,
    expected_dimensions: int | None = None,
    for_retrieval: bool = False,
) -> list[MemoryEmbedding]:
    sources = collect_memory_sources(
        session=session,
        book_id=book_id,
        target_chapter_number=target_chapter_number,
        for_retrieval=for_retrieval,
    )
    repo = MemoryEmbeddingRepository(session)
    source_texts = {(source.source_type, source.source_id): source.text for source in sources}
    source_keys = list(source_texts) if for_retrieval else None

    def current_candidate(row: MemoryEmbedding) -> bool:
        return (
            row.source_text == source_texts[(row.source_type, row.source_id)]
            and row.model == model_name
            and (expected_dimensions is None or row.dimensions == expected_dimensions)
        )

    cached_rows = {
        (row.source_type, row.source_id): row
        for row in repo.list_for_book(book_id, source_keys=source_keys)
        if not for_retrieval or current_candidate(row)
    }
    changed_sources = [
        source
        for source in sources
        if repo.needs_refresh(
            book_id=source.book_id,
            source_type=source.source_type,
            source_id=source.source_id,
            source_text=source.text,
            model=model_name,
        )
        or (
            expected_dimensions is not None
            and (
                cached_rows.get((source.source_type, source.source_id)) is None
                or cached_rows[(source.source_type, source.source_id)].dimensions
                != expected_dimensions
            )
        )
    ]
    vectors = (
        embedding_client.embed_texts([source.text for source in changed_sources])
        if changed_sources
        else []
    )
    if len(vectors) != len(changed_sources):
        raise ValueError("embedding response count does not match memory source count")
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or not vector
            or any(type(value) not in (int, float) for value in vector)
        ):
            raise ValueError(
                "embedding response vector must be a non-empty numeric list"
            )
    if len({len(vector) for vector in vectors}) > 1:
        raise ValueError("embedding response vector dimensions must match")
    if expected_dimensions is not None and any(
        len(vector) != expected_dimensions for vector in vectors
    ):
        raise ValueError("embedding response vector dimensions must match query vector")
    for source, vector in zip(changed_sources, vectors, strict=True):
        repo.upsert(
            book_id=source.book_id,
            source_type=source.source_type,
            source_id=source.source_id,
            source_chapter_number=source.source_chapter_number,
            source_text=source.text,
            model=model_name,
            vector=vector,
        )
    session.flush()
    rows = repo.list_for_book(book_id, source_keys=source_keys)
    if for_retrieval:
        # Previous concurrent refreshes may have left several rows for a source.
        distinct = {(row.source_type, row.source_id): row for row in rows if current_candidate(row)}
        return list(distinct.values())
    return [row for row in rows if (row.source_type, row.source_id) in source_texts]


def recency_weight(
    *,
    source_chapter_number: int | None,
    target_chapter_number: int,
) -> float:
    if source_chapter_number is None:
        return 0.85
    distance = max(1, target_chapter_number - source_chapter_number)
    return max(0.70, 1.0 - 0.02 * (distance - 1))


def retrieve_memory_evidence(
    *,
    session,
    book_id: str,
    target_chapter_number: int,
    query: str,
    embedding_client,
    model_name: str,
    fallback_client,
    embedding_fallback_mode: str,
    limit: int = 5,
) -> dict:
    degradation_reason: str | None = None
    if embedding_client is not None:
        try:
            query_vector = _embed_query(embedding_client, query)
            rows = refresh_memory_index(
                session=session,
                book_id=book_id,
                target_chapter_number=target_chapter_number,
                embedding_client=embedding_client,
                model_name=model_name,
                expected_dimensions=len(query_vector),
                for_retrieval=True,
            )
            return _rank_memory_rows(
                rows=rows,
                query_vector=query_vector,
                target_chapter_number=target_chapter_number,
                mode="semantic",
                degradation_reason=None,
                limit=limit,
            )
        except Exception as exc:
            degradation_reason = str(exc) or exc.__class__.__name__
    else:
        degradation_reason = "embedding API key is not configured"

    if embedding_fallback_mode != "keyword" or fallback_client is None:
        return {
            "mode": "none",
            "degradation_reason": degradation_reason,
            "items": [],
        }

    query_vector = _embed_query(fallback_client, query)
    rows = refresh_memory_index(
        session=session,
        book_id=book_id,
        target_chapter_number=target_chapter_number,
        embedding_client=fallback_client,
        model_name="keyword-hash",
        expected_dimensions=len(query_vector),
        for_retrieval=True,
    )
    return _rank_memory_rows(
        rows=rows,
        query_vector=query_vector,
        target_chapter_number=target_chapter_number,
        mode="keyword",
        degradation_reason=degradation_reason,
        limit=limit,
    )


def _embed_query(embedding_client, query: str) -> list[float]:
    vectors = embedding_client.embed_texts([query])
    if len(vectors) != 1:
        raise ValueError("embedding response count does not match query count")
    vector = vectors[0]
    if (
        not isinstance(vector, list)
        or not vector
        or any(type(value) not in (int, float) for value in vector)
    ):
        raise ValueError("embedding query vector must be a non-empty numeric list")
    return vector


def _rank_memory_rows(
    *,
    rows: list[MemoryEmbedding],
    query_vector: list[float],
    target_chapter_number: int,
    mode: str,
    degradation_reason: str | None,
    limit: int,
) -> dict:
    scored_rows: list[tuple[float, MemoryEmbedding]] = []
    for row in rows:
        if row.dimensions != len(query_vector) or len(row.vector) != len(query_vector):
            raise ValueError("memory embedding dimensions must match query vector")
        score = (
            cosine_similarity(query_vector, row.vector)
            * SOURCE_WEIGHTS[row.source_type]
            * recency_weight(
                source_chapter_number=row.source_chapter_number,
                target_chapter_number=target_chapter_number,
            )
        )
        scored_rows.append((round(score, 6), row))

    scored_rows.sort(
        key=lambda item: (
            -item[0],
            -(item[1].source_chapter_number or -1),
            item[1].source_type,
            item[1].source_id,
        )
    )
    return {
        "mode": mode,
        "degradation_reason": degradation_reason,
        "items": [
            {
                "book_id": row.book_id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "score": score,
                "excerpt": row.source_text[:240],
                "selection_reason": (
                    "semantic similarity + source weight + recency weight"
                ),
            }
            for score, row in scored_rows[: max(0, limit)]
        ],
    }
