from __future__ import annotations

from sqlalchemy import select

from app.db.models import Chapter, ChapterVersion, PublishJob
from app.orchestrator.chapter_workflow import ChapterWorkflow
from app.orchestrator.retry_policy import PLATFORM_ERROR, classify_error
from app.orchestrator.state_machine import (
    DRAFT_PLANNED,
    DRAFT_UPLOADED,
    PUBLISH_FAILED,
    PUBLISH_PENDING,
)


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
    ) -> dict:
        publish_statement = select(PublishJob).where(
            PublishJob.status == PUBLISH_PENDING
        )
        if excluded_publish_job_ids:
            publish_statement = publish_statement.where(
                PublishJob.id.not_in(excluded_publish_job_ids)
            )
        publish_job = self.session.scalar(publish_statement.order_by(PublishJob.id.asc()))
        if publish_job is not None:
            return self.run_publish_job(publish_job.id)

        chapter_statement = select(Chapter).where(Chapter.status == DRAFT_PLANNED)
        if excluded_chapter_ids:
            chapter_statement = chapter_statement.where(
                Chapter.id.not_in(excluded_chapter_ids)
            )
        chapter = self.session.scalar(
            chapter_statement.order_by(Chapter.chapter_number.asc())
        )
        if chapter is None:
            return {"final_status": "idle"}
        return self.run_once(chapter.id)

    def run_pending_batch(self, *, limit: int = 10) -> list[dict]:
        results: list[dict] = []
        excluded_publish_job_ids: set[str] = set()
        excluded_chapter_ids: set[str] = set()
        for _ in range(limit):
            result = self.run_next_pending(
                excluded_publish_job_ids=excluded_publish_job_ids,
                excluded_chapter_ids=excluded_chapter_ids,
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

        publish_job = self.session.scalar(
            select(PublishJob).where(PublishJob.id == publish_job_id)
        )
        if publish_job is None:
            raise ValueError(f"Publish job not found: {publish_job_id}")

        chapter = self.session.scalar(
            select(Chapter).where(Chapter.id == publish_job.chapter_id)
        )
        if chapter is None:
            raise ValueError(f"Chapter not found for publish job: {publish_job_id}")

        latest_version = self.session.scalar(
            select(ChapterVersion)
            .where(ChapterVersion.id == chapter.current_version_id)
            .where(ChapterVersion.chapter_id == chapter.id)
        )
        if (
            latest_version is None
            or not isinstance(latest_version.content, str)
            or not latest_version.content.strip()
        ):
            publish_job.status = PUBLISH_FAILED
            publish_job.error_message = (
                "chapter version is missing, belongs to another chapter, or has empty content"
            )
            self.session.commit()
            return {
                "final_status": PUBLISH_FAILED,
                "item_type": "publish_job",
                "item_id": publish_job_id,
                "error_type": "business_error",
            }

        try:
            result = self.publisher(chapter.title, latest_version.content)
        except Exception as exc:
            error_type = classify_error(str(exc))
            publish_job.error_message = str(exc)
            if error_type == PLATFORM_ERROR:
                publish_job.status = PUBLISH_PENDING
                self.session.commit()
                return {
                    "final_status": PUBLISH_PENDING,
                    "item_type": "publish_job",
                    "item_id": publish_job_id,
                    "error_type": error_type,
                }
            publish_job.status = PUBLISH_FAILED
            publish_job.error_message = str(exc)
            self.session.commit()
            return {
                "final_status": PUBLISH_FAILED,
                "item_type": "publish_job",
                "item_id": publish_job_id,
                "error_type": error_type,
            }

        if not result.get("success"):
            publish_job.status = PUBLISH_FAILED
            publish_job.error_message = result.get("error_message") or "publish failed"
            self.session.commit()
            return {
                "final_status": PUBLISH_FAILED,
                "item_type": "publish_job",
                "item_id": publish_job_id,
                "error_type": "business_error",
            }

        publish_job.status = DRAFT_UPLOADED
        publish_job.platform_draft_id = result.get("platform_draft_id")
        publish_job.screenshot_path = result.get("screenshot_path")
        publish_job.error_message = None
        self.session.commit()
        return {
            "final_status": DRAFT_UPLOADED,
            "item_type": "publish_job",
            "item_id": publish_job_id,
        }
