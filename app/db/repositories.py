import hashlib

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    Character,
    CharacterSnapshot,
    MemoryEmbedding,
    PublishJob,
    SchedulerRun,
    StoryArc,
    TrendSnapshot,
)


class BookRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_book(
        self,
        *,
        id: str,
        title: str,
        platform: str,
        genre: str,
        logline: str | None = None,
        book_outline: dict | None = None,
        style_rules: dict | None = None,
    ) -> Book:
        book = Book(
            id=id,
            title=title,
            platform=platform,
            genre=genre,
            logline=logline,
            book_outline=book_outline,
            style_rules=style_rules,
            status="draft",
        )
        self.session.add(book)
        self.session.flush()
        self.session.refresh(book)
        return book

    def list_books(self) -> list[Book]:
        return list(self.session.scalars(select(Book).order_by(Book.title.asc())))

    def get_book(self, book_id: str) -> Book | None:
        return self.session.scalar(select(Book).where(Book.id == book_id))

    def update_outline(
        self,
        book_id: str,
        *,
        book_outline: dict | None,
        logline: str | None,
        style_rules: dict | None,
    ) -> Book | None:
        book = self.get_book(book_id)
        if book is None:
            return None
        book.book_outline = book_outline
        book.logline = logline
        book.style_rules = style_rules
        self.session.flush()
        self.session.refresh(book)
        return book

    def get_next_chapter_context(self, book_id: str) -> dict:
        memory_repo = MemoryRepository(self.session)
        return {
            "story_arcs": [
                {
                    "arc_type": arc.arc_type,
                    "stage_goal": arc.stage_goal,
                }
                for arc in memory_repo.list_story_arcs(book_id)
            ],
            "character_states": memory_repo.list_latest_character_states(book_id),
        }


class ChapterRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_planned_chapter(
        self,
        *,
        book_id: str,
        chapter_number: int,
        title: str,
        goal: dict,
    ) -> Chapter:
        chapter = Chapter(
            book_id=book_id,
            chapter_number=chapter_number,
            title=title,
            goal=goal,
            status="draft_planned",
            current_version_id=None,
        )
        self.session.add(chapter)
        self.session.flush()
        self.session.refresh(chapter)
        return chapter

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        return self.session.scalar(select(Chapter).where(Chapter.id == chapter_id))

    def list_book_chapters(self, book_id: str) -> list[Chapter]:
        statement = (
            select(Chapter)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.chapter_number.asc())
        )
        return list(self.session.scalars(statement))

    def list_chapter_versions(self, chapter_id: str) -> list[ChapterVersion]:
        statement = (
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.created_at.desc(), ChapterVersion.id.asc())
        )
        return list(self.session.scalars(statement))

    def list_manual_review_queue(self) -> list[dict]:
        statement = (
            select(Chapter, Book, ChapterVersion)
            .join(Book, Book.id == Chapter.book_id)
            .outerjoin(
                ChapterVersion,
                (ChapterVersion.id == Chapter.current_version_id)
                & (ChapterVersion.chapter_id == Chapter.id),
            )
            .where(Chapter.status.in_(("review_pending", "revision_pending")))
            .order_by(Chapter.created_at.asc(), Chapter.chapter_number.asc())
        )
        rows = self.session.execute(statement).all()
        return [
            {
                "id": chapter.id,
                "book_id": chapter.book_id,
                "book_title": book.title,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "status": chapter.status,
                "manual_review_note": (chapter.goal or {}).get("manual_review_note", ""),
                "manual_review_action": (chapter.goal or {}).get("manual_review_action", ""),
                "goal": chapter.goal or {},
                "candidate_version": None
                if version is None
                else {
                    "id": version.id,
                    "source": version.source,
                    "content": version.content,
                    "generation_metadata": version.generation_metadata or {},
                },
            }
            for chapter, book, version in rows
        ]

    def update_manual_review(
        self,
        chapter_id: str,
        *,
        status: str,
        note: str,
    ) -> Chapter | None:
        chapter = self.get_chapter(chapter_id)
        if chapter is None:
            return None

        next_goal = dict(chapter.goal or {})
        next_goal["manual_review_note"] = note
        next_goal["manual_review_action"] = status
        chapter.goal = next_goal
        chapter.status = status
        self.session.flush()
        self.session.refresh(chapter)
        return chapter

    def update_goal_fields(
        self,
        chapter_id: str,
        *,
        fields: dict,
        status: str | None = None,
    ) -> Chapter | None:
        chapter = self.get_chapter(chapter_id)
        if chapter is None:
            return None

        next_goal = dict(chapter.goal or {})
        next_goal.update(fields)
        chapter.goal = next_goal
        if status is not None:
            chapter.status = status
        self.session.flush()
        self.session.refresh(chapter)
        return chapter


class PublishJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(
        self,
        *,
        chapter_id: str,
        platform: str,
        mode: str,
        status: str,
        chapter_version_id: str | None = None,
        chapter_title: str | None = None,
        platform_draft_id: str | None = None,
        error_message: str | None = None,
        screenshot_path: str | None = None,
    ) -> PublishJob:
        job = PublishJob(
            chapter_id=chapter_id,
            platform=platform,
            mode=mode,
            status=status,
            chapter_version_id=chapter_version_id,
            chapter_title=chapter_title,
            platform_draft_id=platform_draft_id,
            error_message=error_message,
            screenshot_path=screenshot_path,
        )
        self.session.add(job)
        self.session.flush()
        self.session.refresh(job)
        return job

    def list_jobs(
        self,
        *,
        status: str | None = None,
        chapter_id: str | None = None,
        book_id: str | None = None,
        latest_per_chapter: bool = False,
        limit: int | None = None,
        descending: bool = False,
    ) -> list[PublishJob]:
        statement = select(PublishJob)
        if status is not None:
            statement = statement.where(PublishJob.status == status)
        if chapter_id is not None:
            statement = statement.where(PublishJob.chapter_id == chapter_id)
        if book_id is not None:
            statement = statement.join(Chapter, Chapter.id == PublishJob.chapter_id).where(
                Chapter.book_id == book_id
            )
        if latest_per_chapter:
            ranked = statement.with_only_columns(
                PublishJob.id,
                func.row_number().over(
                    partition_by=PublishJob.chapter_id,
                    order_by=self._latest_order(),
                ).label("position"),
            ).subquery()
            statement = select(PublishJob).join(ranked, ranked.c.id == PublishJob.id).where(
                ranked.c.position == 1
            )
        ordering = self._latest_order() if descending or latest_per_chapter else (PublishJob.id.asc(),)
        statement = statement.order_by(*ordering)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    @staticmethod
    def _latest_order() -> tuple:
        # Legacy NULL timestamps remain unknown. IDs only break ties deterministically.
        return (
            PublishJob.created_at.is_(None).asc(),
            PublishJob.created_at.desc(),
            PublishJob.id.desc(),
        )

    def get_job(self, job_id: str) -> PublishJob | None:
        return self.session.scalar(select(PublishJob).where(PublishJob.id == job_id))

    def get_latest_for_chapter(self, chapter_id: str) -> PublishJob | None:
        statement = (
            select(PublishJob)
            .where(PublishJob.chapter_id == chapter_id)
            .order_by(*self._latest_order())
            .limit(1)
        )
        return self.session.scalar(statement)

    def update_status(
        self,
        job: PublishJob,
        *,
        status: str,
        error_message: str | None = None,
    ) -> PublishJob:
        job.status = status
        job.error_message = error_message
        self.session.flush()
        self.session.refresh(job)
        return job


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_story_arcs(self, book_id: str) -> list[StoryArc]:
        statement = select(StoryArc).where(StoryArc.book_id == book_id).order_by(StoryArc.chapter_start.asc())
        return list(self.session.scalars(statement))

    def list_latest_character_states(
        self,
        book_id: str,
        before_chapter_number: int | None = None,
    ) -> list[dict]:
        characters = list(
            self.session.scalars(
                select(Character)
                .where(Character.book_id == book_id)
                .order_by(Character.name.asc())
            )
        )
        snapshots: list[dict] = []
        for character in characters:
            statement = select(CharacterSnapshot).where(
                CharacterSnapshot.character_id == character.id
            )
            if before_chapter_number is None:
                statement = statement.order_by(CharacterSnapshot.created_at.desc())
            else:
                statement = (
                    statement.join(Chapter, Chapter.id == CharacterSnapshot.chapter_id)
                    .where(Chapter.book_id == book_id)
                    .where(Chapter.chapter_number < before_chapter_number)
                    .order_by(
                        Chapter.chapter_number.desc(),
                        CharacterSnapshot.created_at.desc(),
                        CharacterSnapshot.id.desc(),
                    )
                )
            snapshot = self.session.scalar(statement)
            snapshots.append(
                {
                    "id": character.id,
                    "name": character.name,
                    "role": character.role,
                    "age": character.age,
                    "profile": character.profile or {},
                    "state": snapshot.state if snapshot is not None else {},
                }
            )
        return snapshots

    def list_recent_summaries(self, chapter_id: str, *, limit: int = 3) -> list[str]:
        chapter = self.session.scalar(select(Chapter).where(Chapter.id == chapter_id))
        if chapter is None:
            return []
        statement = (
            select(ChapterVersion.summary)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == chapter.book_id)
            .where(Chapter.chapter_number < chapter.chapter_number)
            .where(ChapterVersion.id == Chapter.approved_version_id)
            .where(ChapterVersion.source == "approved")
            .where(ChapterVersion.summary.is_not(None))
            .order_by(Chapter.chapter_number.desc(), ChapterVersion.created_at.desc())
            .where(ChapterVersion.summary != "")
            .limit(limit)
        )
        summaries = [summary for summary in self.session.scalars(statement) if summary]
        return summaries[:limit]


class MemoryEmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def needs_refresh(
        self,
        *,
        book_id: str,
        source_type: str,
        source_id: str,
        source_text: str,
        model: str,
    ) -> bool:
        row = self._find(
            book_id=book_id,
            source_type=source_type,
            source_id=source_id,
        )
        text_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        return row is None or row.text_hash != text_hash or row.model != model

    def upsert(
        self,
        *,
        book_id: str,
        source_type: str,
        source_id: str,
        source_text: str,
        model: str,
        vector: list[float],
        source_chapter_number: int | None = None,
    ) -> MemoryEmbedding:
        text_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        row = self._find(
            book_id=book_id,
            source_type=source_type,
            source_id=source_id,
        )
        if row is None:
            row = MemoryEmbedding(
                book_id=book_id,
                source_type=source_type,
                source_id=source_id,
                source_chapter_number=source_chapter_number,
                source_text=source_text,
                text_hash=text_hash,
                model=model,
                dimensions=len(vector),
                vector=vector,
            )
            self.session.add(row)
        elif row.text_hash != text_hash or row.model != model or row.vector != vector:
            row.source_text = source_text
            row.text_hash = text_hash
            row.model = model
            row.dimensions = len(vector)
            row.vector = vector
            row.source_chapter_number = source_chapter_number
        self.session.flush()
        self.session.refresh(row)
        return row

    def list_for_book(self, book_id: str) -> list[MemoryEmbedding]:
        statement = (
            select(MemoryEmbedding)
            .where(MemoryEmbedding.book_id == book_id)
            .order_by(MemoryEmbedding.source_type.asc(), MemoryEmbedding.source_id.asc())
        )
        return list(self.session.scalars(statement))

    def _find(
        self,
        *,
        book_id: str,
        source_type: str,
        source_id: str,
    ) -> MemoryEmbedding | None:
        statement = (
            select(MemoryEmbedding)
            .where(MemoryEmbedding.book_id == book_id)
            .where(MemoryEmbedding.source_type == source_type)
            .where(MemoryEmbedding.source_id == source_id)
        )
        return self.session.scalar(statement)


class SchedulerRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        *,
        cycle_number: int,
        processed_count: int,
        status: str,
        outcomes: list[dict],
        outcome_counts: dict[str, int] | None = None,
        reason_code: str = "unspecified",
        duration_ms: int = 0,
    ) -> SchedulerRun:
        run = SchedulerRun(
            cycle_number=cycle_number,
            processed_count=processed_count,
            status=status,
            outcomes=outcomes,
            outcome_counts=outcome_counts if outcome_counts is not None else {},
            reason_code=reason_code,
            duration_ms=duration_ms,
        )
        self.session.add(run)
        self.session.flush()
        self.session.refresh(run)
        return run

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SchedulerRun]:
        statement = select(SchedulerRun).order_by(SchedulerRun.cycle_number.desc())
        if status is not None:
            statement = statement.where(SchedulerRun.status == status)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    def get_run(self, run_id: str) -> SchedulerRun | None:
        return self.session.scalar(select(SchedulerRun).where(SchedulerRun.id == run_id))

    def get_summary(self) -> dict[str, int]:
        statement = select(
            func.count(SchedulerRun.id).label("total_runs"),
            *(
                func.count(case((SchedulerRun.status == status, 1))).label(f"{status}_runs")
                for status in ("processed", "idle", "attention_required", "failed")
            ),
            func.coalesce(func.sum(SchedulerRun.processed_count), 0).label("total_processed_items"),
        )
        return dict(self.session.execute(statement).mappings().one())

    def get_failure_clusters(self, *, limit: int = 500) -> list[dict[str, int | str]]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        recent_runs = select(SchedulerRun).order_by(
            SchedulerRun.created_at.desc(), SchedulerRun.cycle_number.desc(), SchedulerRun.id.desc()
        ).limit(limit)
        clusters: dict[str, dict[str, int | str]] = {}

        for run in self.session.scalars(recent_runs):
            for outcome in run.outcomes:
                final_status = outcome.get("final_status") or "unknown"
                item_type = outcome.get("item_type") or "item"
                error_type = outcome.get("error_type") or "none"
                if final_status not in {"publish_failed", "revision_pending"}:
                    continue

                failure_key = f"{item_type}:{final_status}:{error_type}"
                existing = clusters.get(failure_key)
                if existing is None:
                    clusters[failure_key] = {
                        "failure_key": failure_key,
                        "item_type": item_type,
                        "final_status": final_status,
                        "error_type": error_type,
                        "count": 1,
                        "latest_cycle_number": run.cycle_number,
                        "latest_at": run.created_at.isoformat(),
                        "sample_item_id": outcome.get("item_id") or "unknown",
                    }
                    continue

                existing["count"] = int(existing["count"]) + 1

        # Stable sorting preserves newest-first order for equally frequent clusters.
        return sorted(clusters.values(), key=lambda item: int(item["count"]), reverse=True)


class TrendSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_snapshot(
        self,
        *,
        capture_id: str,
        provider: str,
        source_url: str,
        status: str,
        items: list[dict],
        error_message: str | None,
    ) -> TrendSnapshot:
        row = TrendSnapshot(
            capture_id=capture_id,
            provider=provider,
            source_url=source_url,
            status=status,
            items=items,
            error_message=error_message,
        )
        self.session.add(row)
        self.session.flush()
        self.session.refresh(row)
        return row

    def get_snapshot(self, snapshot_id: str) -> TrendSnapshot | None:
        return self.session.scalar(
            select(TrendSnapshot).where(TrendSnapshot.id == snapshot_id)
        )

    def list_snapshots(
        self,
        *,
        provider: str | None = None,
        capture_id: str | None = None,
        limit: int = 50,
    ) -> list[TrendSnapshot]:
        statement = select(TrendSnapshot).order_by(
            TrendSnapshot.captured_at.desc(),
            TrendSnapshot.provider.asc(),
            TrendSnapshot.id.asc(),
        )
        if provider is not None:
            statement = statement.where(TrendSnapshot.provider == provider)
        if capture_id is not None:
            statement = statement.where(TrendSnapshot.capture_id == capture_id)
        return list(self.session.scalars(statement.limit(limit)))
