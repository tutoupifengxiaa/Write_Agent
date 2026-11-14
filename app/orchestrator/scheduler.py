from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, select, update

from app.db.models import Chapter, ChapterVersion, PublishJob
from app.orchestrator.chapter_workflow import ChapterWorkflow
from app.orchestrator.retry_policy import classify_error, max_retries
from app.orchestrator.state_machine import (
    DRAFT_PLANNED,
    DRAFT_UPLOADED,
    PUBLISH_FAILED,
    PUBLISH_PENDING,
)
from app.orchestrator.task_claims import claim_task, ready_task, utc_now
from app.services.llm_client import LLMConfigurationError


class Scheduler:
    def __init__(
        self,
        *,
        session,
        writer,
        reviewer,
        reviser=None,
        publisher=None,
        embedding_client=None,
        embedding_model_name: str = "keyword-hash",
        fallback_embedding_client=None,
        embedding_fallback_mode: str = "keyword",
    ) -> None:
        self.session = session
        self.writer = writer
        self.reviewer = reviewer
        self.reviser = reviser
        self.publisher = publisher
        self.embedding_client = embedding_client
        self.embedding_model_name = embedding_model_name
        self.fallback_embedding_client = fallback_embedding_client
        self.embedding_fallback_mode = embedding_fallback_mode
        self.chapter_workflow = ChapterWorkflow(
            session=self.session,
            writer=lambda context: self.writer(context),
            reviewer=lambda content, context: self.reviewer(content, context),
            reviser=(
                (lambda content, review: self.reviser(content, review))
                if self.reviser is not None
                else None
            ),
            embedding_client=self.embedding_client,
            embedding_model_name=self.embedding_model_name,
            fallback_embedding_client=self.fallback_embedding_client,
            embedding_fallback_mode=self.embedding_fallback_mode,
            create_publish_job=self.publisher is not None,
        )

    def run_next_pending(
        self,
        *,
        excluded_publish_job_ids: set[str] | None = None,
        excluded_chapter_ids: set[str] | None = None,
        chapters_per_book: dict[str, int] | None = None,
    ) -> dict:
        now = utc_now()
        self.session.execute(update(Chapter).where(
            Chapter.status == DRAFT_PLANNED,
            Chapter.claim_token.is_not(None), Chapter.lease_expires_at <= now,
            Chapter.attempt_count >= 3,
        ).values(status="generation_failed", claim_token=None, lease_expires_at=None,
                 error_message="generation attempts exhausted after worker interruption")
            .execution_options(synchronize_session="fetch"))
        # An expired upload may already have succeeded remotely. Require human
        # reconciliation instead of blindly replaying an external side effect.
        self.session.execute(update(PublishJob).where(
            PublishJob.status == "publishing", PublishJob.lease_expires_at <= now,
        ).values(status=PUBLISH_FAILED, claim_token=None, lease_expires_at=None,
                 error_message="publication outcome unknown; verify platform before retry")
            .execution_options(synchronize_session="fetch"))
        self.session.commit()
        publish_statement = select(PublishJob).where(ready_task(PublishJob, PUBLISH_PENDING, now))
        if excluded_publish_job_ids:
            publish_statement = publish_statement.where(
                PublishJob.id.not_in(excluded_publish_job_ids)
            )
        publish_job = self.session.scalar(publish_statement.order_by(PublishJob.created_at.asc(), PublishJob.id.asc())) if self.publisher is not None else None
        if publish_job is not None:
            return self.run_publish_job(publish_job.id)

        chapter_statement = select(Chapter).where(ready_task(Chapter, DRAFT_PLANNED, now))
        if excluded_chapter_ids:
            chapter_statement = chapter_statement.where(
                Chapter.id.not_in(excluded_chapter_ids)
            )
        if chapters_per_book:
            chapter_statement = chapter_statement.order_by(
                case(chapters_per_book, value=Chapter.book_id, else_=0)
            )
        chapter = self.session.scalar(
            chapter_statement.order_by(Chapter.chapter_number, Chapter.id).limit(1)
        )
        if chapter is None:
            self.session.rollback()
            return {"final_status": "idle"}
        chapter_id = chapter.id
        if chapters_per_book is not None:
            chapters_per_book[chapter.book_id] = chapters_per_book.get(chapter.book_id, 0) + 1
        try:
            return self.run_once(chapter_id)
        except LLMConfigurationError:
            raise
        except Exception as exc:
            self.session.rollback()
            chapter = self.session.get(Chapter, chapter_id)
            return {
                "final_status": chapter.status if chapter else "generation_failed",
                "item_type": "chapter", "item_id": chapter_id,
                "error_type": classify_error(exc),
            }

    def run_pending_batch(self, *, limit: int = 10) -> list[dict]:
        results: list[dict] = []
        excluded_publish_job_ids: set[str] = set()
        excluded_chapter_ids: set[str] = set()
        chapters_per_book: dict[str, int] = {}
        for _ in range(limit):
            result = self.run_next_pending(
                excluded_publish_job_ids=excluded_publish_job_ids,
                excluded_chapter_ids=excluded_chapter_ids,
                chapters_per_book=chapters_per_book,
            )
            if result.get("final_status") == "idle":
                break
            results.append(result)
            if result.get("item_type") == "publish_job" and result.get("item_id"):
                excluded_publish_job_ids.add(result["item_id"])
            if result.get("item_type") == "chapter" and result.get("item_id"):
                excluded_chapter_ids.add(result["item_id"])
        return results

    def run_once(self, chapter_id: str) -> dict:
        return self.chapter_workflow.invoke(chapter_id)

    def run_publish_job(self, publish_job_id: str) -> dict:
        if self.publisher is None:
            raise ValueError("publisher is not configured")

        publish_job = self.session.get(PublishJob, publish_job_id)
        if publish_job is None:
            raise ValueError(f"Publish job not found: {publish_job_id}")

        token = claim_task(self.session, PublishJob, publish_job_id, PUBLISH_PENDING, running_status="publishing")
        if token is None:
            return {"final_status": "skipped", "item_type": "publish_job", "item_id": publish_job_id}
        self.session.refresh(publish_job)
        attempts = publish_job.attempt_count
        title = publish_job.chapter_title
        version = self.session.scalar(
            select(ChapterVersion)
            .where(ChapterVersion.id == publish_job.chapter_version_id)
            .where(ChapterVersion.chapter_id == publish_job.chapter_id)
            .where(ChapterVersion.source == "approved")
        )
        content = version.content if version is not None else None
        self.session.rollback()
        if not title or not isinstance(content, str) or not content.strip():
            return self._finish_publish(publish_job_id, token, PUBLISH_FAILED,
                error_type="business_error", error_message="job has no valid bound approved version/title; review and create a new job")
        try:
            result = self.publisher(title, content)
        except Exception as exc:
            error_type = classify_error(exc)
            retry = attempts <= max_retries(error_type)
            return self._finish_publish(publish_job_id, token, PUBLISH_PENDING if retry else PUBLISH_FAILED,
                error_type=error_type, error_message=f"{type(exc).__name__}: {error_type}",
                next_attempt_at=utc_now() + timedelta(seconds=5 * 2 ** min(attempts - 1, 8)) if retry else None)
        if not isinstance(result, dict) or not result.get("success"):
            return self._finish_publish(publish_job_id, token, PUBLISH_FAILED,
                error_type="business_error", error_message="publish failed")
        return self._finish_publish(publish_job_id, token, DRAFT_UPLOADED,
            platform_draft_id=result.get("platform_draft_id"), screenshot_path=result.get("screenshot_path"))

    def _finish_publish(self, job_id, token, status, *, error_type=None, **values):
        result = self.session.execute(update(PublishJob).where(
            PublishJob.id == job_id, PublishJob.claim_token == token,
            PublishJob.status == "publishing",
        ).values(status=status, claim_token=None, lease_expires_at=None, **values))
        self.session.commit()
        outcome = {"final_status": status if result.rowcount == 1 else "superseded",
                   "item_type": "publish_job", "item_id": job_id}
        if error_type:
            outcome["error_type"] = error_type
        return outcome
