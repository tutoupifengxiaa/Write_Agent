from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.db.models import Chapter, PublishJob
from app.db.repositories import PublishJobRepository, SchedulerRunRepository

router = APIRouter()


@router.get("/jobs/health")
def jobs_health(session: Session = Depends(get_session)) -> dict[str, int | str]:
    pending_chapters = session.scalar(
        select(func.count()).select_from(Chapter).where(Chapter.status == "draft_planned")
    )
    pending_publish_jobs = session.scalar(
        select(func.count()).select_from(PublishJob).where(PublishJob.status == "publish_pending")
    )
    scheduler_state = "busy" if pending_chapters or pending_publish_jobs else "idle"
    return {
        "scheduler": scheduler_state,
        "pending_chapters": pending_chapters,
        "pending_publish_jobs": pending_publish_jobs,
    }


@router.get("/jobs")
def list_jobs(
    session: Session = Depends(get_session),
    status: str | None = None,
    chapter_id: str | None = None,
    book_id: str | None = None,
    latest_per_chapter: bool = False,
    limit: int | None = Query(default=None, ge=1),
) -> list[dict]:
    repo = PublishJobRepository(session)
    jobs = repo.list_jobs(
        status=status, chapter_id=chapter_id, book_id=book_id,
        latest_per_chapter=latest_per_chapter, limit=limit, descending=True,
    )
    return [
        {
            "id": job.id,
            "chapter_id": job.chapter_id,
            "platform": job.platform,
            "mode": job.mode,
            "status": job.status,
            "platform_draft_id": job.platform_draft_id,
            "error_message": job.error_message,
            "screenshot_path": job.screenshot_path,
            "created_at": _job_created_at(job),
        }
        for job in jobs
    ]


@router.get("/jobs/runs/summary")
def get_scheduler_run_summary(session: Session = Depends(get_session)) -> dict[str, int]:
    repo = SchedulerRunRepository(session)
    return repo.get_summary()


@router.get("/jobs/runs/failure-clusters")
def list_scheduler_run_failure_clusters(
    session: Session = Depends(get_session),
) -> list[dict[str, int | str]]:
    repo = SchedulerRunRepository(session)
    return repo.get_failure_clusters()


@router.get("/jobs/runs")
def list_scheduler_runs(
    session: Session = Depends(get_session),
    status: str | None = None,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    repo = SchedulerRunRepository(session)
    runs = repo.list_runs(status=status, limit=limit, offset=offset)
    return [
        {
            "id": run.id,
            "cycle_number": run.cycle_number,
            "processed_count": run.processed_count,
            "status": run.status,
            "outcomes": run.outcomes,
            "reason_code": getattr(run, "reason_code", None) or "unspecified",
            "duration_ms": getattr(run, "duration_ms", None) or 0,
            "outcome_counts": getattr(run, "outcome_counts", None) or {},
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }
        for run in runs
    ]


@router.get("/jobs/runs/{run_id}")
def get_scheduler_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    repo = SchedulerRunRepository(session)
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scheduler run not found")
    return {
        "id": run.id,
        "cycle_number": run.cycle_number,
        "processed_count": run.processed_count,
        "status": run.status,
        "outcomes": run.outcomes,
        "reason_code": getattr(run, "reason_code", None) or "unspecified",
        "duration_ms": getattr(run, "duration_ms", None) or 0,
        "outcome_counts": getattr(run, "outcome_counts", None) or {},
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_session)) -> dict:
    repo = PublishJobRepository(session)
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": job.id,
        "chapter_id": job.chapter_id,
        "platform": job.platform,
        "mode": job.mode,
        "status": job.status,
        "platform_draft_id": job.platform_draft_id,
        "error_message": job.error_message,
        "screenshot_path": job.screenshot_path,
        "created_at": _job_created_at(job),
    }


def _job_created_at(job: PublishJob) -> str | None:
    if job.created_at is None:
        return None
    timestamp = job.created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
