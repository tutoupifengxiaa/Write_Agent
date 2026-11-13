from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Book(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    platform: Mapped[str] = mapped_column(String(64))
    genre: Mapped[str] = mapped_column(String(128))
    logline: Mapped[str | None] = mapped_column(Text, nullable=True)
    book_outline: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    style_rules: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="draft", nullable=False)


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (Index("ix_chapters_status_next_attempt_at", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("chapter"),
    )
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), nullable=False, index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="draft_planned")
    current_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ChapterVersion(Base):
    __tablename__ = "chapter_versions"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("chapter_version"),
    )
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ContinuityEvent(Base):
    __tablename__ = "continuity_events"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("evt"),
    )
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    chapter_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, default="state_delta")
    delta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (Index("ix_publish_jobs_status_next_attempt_at", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("publish_job"),
    )
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    chapter_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chapter_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_draft_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True).evaluates_none(),
        default=lambda: datetime.now(timezone.utc),
        nullable=True,
    )


class StoryArc(Base):
    __tablename__ = "story_arcs"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("story_arc"),
    )
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), nullable=False, index=True)
    arc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    chapter_start: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    outline: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("character"),
    )
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str | None] = mapped_column(String(128), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_updated_chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CharacterSnapshot(Base):
    __tablename__ = "character_snapshots"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("character_snapshot"),
    )
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), nullable=False, index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), nullable=False, index=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("memory_embedding"),
    )
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TrendSnapshot(Base):
    __tablename__ = "trend_snapshots"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("trend_snapshot"),
    )
    capture_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: _new_id("scheduler_run"),
    )
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    reason_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="unspecified",
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
