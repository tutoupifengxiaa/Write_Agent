from __future__ import annotations

import time
from typing import Callable

from app.db.repositories import SchedulerRunRepository

CycleEventCallback = Callable[[str, dict], None]
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


def run_scheduler_loop(
    *,
    scheduler,
    batch_limit: int = 10,
    poll_interval_seconds: float = 5.0,
    max_cycles: int | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    session=None,
    event_callback: CycleEventCallback | None = None,
) -> dict[str, int | dict[str, int]]:
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be positive")
    consecutive_failures = 0
    retry_delay = poll_interval_seconds
    cycles = 0
    processed = 0
    status_counts = {
        "processed": 0,
        "attention_required": 0,
        "failed": 0,
        "idle": 0,
    }
    run_repo = SchedulerRunRepository(session) if session is not None else None

    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        cycle_started_at = monotonic_fn()
        if event_callback is not None:
            event_callback(
                "worker.cycle.started",
                {
                    "cycle_number": cycles,
                    "batch_limit": batch_limit,
                },
            )

        try:
            results = scheduler.run_pending_batch(limit=batch_limit)
            duration_ms = round((monotonic_fn() - cycle_started_at) * 1000)
            cycle_status, reason_code = _classify_cycle(results)
            outcome_counts = _count_outcomes(results)
            if run_repo is not None and cycle_status != "idle":
                run_repo.create_run(
                    cycle_number=cycles,
                    processed_count=len(results),
                    status=cycle_status,
                    outcomes=results,
                    reason_code=reason_code,
                    duration_ms=duration_ms,
                    outcome_counts=outcome_counts,
                )
                session.commit()
        except Exception as exc:
            duration_ms = round((monotonic_fn() - cycle_started_at) * 1000)
            failed_outcomes: list[dict] = []
            failed_outcome_counts: dict[str, int] = {}
            if run_repo is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    run_repo.create_run(
                        cycle_number=cycles,
                        processed_count=0,
                        status="failed",
                        outcomes=failed_outcomes,
                        reason_code="cycle_exception",
                        duration_ms=duration_ms,
                        outcome_counts=failed_outcome_counts,
                    )
                    session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            if event_callback is not None:
                try:
                    event_callback(
                        "worker.cycle.failed",
                        {
                            "cycle_number": cycles,
                            "status": "failed",
                            "reason_code": "cycle_exception",
                            "duration_ms": duration_ms,
                            "processed_count": 0,
                            "outcome_counts": failed_outcome_counts,
                            "outcomes": failed_outcomes,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
                except Exception:
                    pass
            status_counts["failed"] += 1
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                raise
            if max_cycles is None or cycles < max_cycles:
                retry_delay = min(2 * retry_delay, 300)
                sleep_fn(retry_delay)
            continue

        consecutive_failures = 0
        retry_delay = poll_interval_seconds
        processed += len(results)
        status_counts[cycle_status] += 1
        if event_callback is not None:
            event_callback(
                "worker.cycle.completed",
                {
                    "cycle_number": cycles,
                    "status": cycle_status,
                    "reason_code": reason_code,
                    "duration_ms": duration_ms,
                    "processed_count": len(results),
                    "outcome_counts": outcome_counts,
                    "outcomes": results,
                },
            )
        if max_cycles is None or cycles < max_cycles:
            sleep_fn(poll_interval_seconds)

    summary = {
        "cycles": cycles,
        "processed": processed,
        "status_counts": status_counts,
    }
    if event_callback is not None:
        event_callback("worker_loop_completed", summary)
    return summary


def _count_outcomes(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        final_status = str(item.get("final_status") or "unknown")
        counts[final_status] = counts.get(final_status, 0) + 1
    return counts


def _classify_cycle(results: list[dict]) -> tuple[str, str]:
    if not results:
        return "idle", "no_pending_work"

    final_statuses = {item.get("final_status") for item in results}
    if "generation_failed" in final_statuses:
        return "failed", "generation_failed"
    if "publish_failed" in final_statuses:
        return "failed", "publish_failed"
    if "draft_planned" in final_statuses:
        return "attention_required", "generation_retry_pending"
    if "revision_pending" in final_statuses:
        return "attention_required", "revision_pending"
    if "publish_pending" in final_statuses:
        return "attention_required", "publish_retry_pending"
    if "draft_uploaded" in final_statuses:
        return "processed", "publish_draft_uploaded"
    return "processed", "chapter_persisted"
