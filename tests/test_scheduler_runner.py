import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Book, Chapter, SchedulerRun
from app.orchestrator.scheduler_runner import run_scheduler_loop


def test_run_scheduler_loop_calls_scheduler_until_idle():
    calls = []

    class FakeScheduler:
        def run_pending_batch(self, *, limit: int):
            calls.append(limit)
            if len(calls) == 1:
                return [{"final_status": "persisted"}]
            return []

    result = run_scheduler_loop(
        scheduler=FakeScheduler(),
        batch_limit=2,
        poll_interval_seconds=0,
        max_cycles=2,
        sleep_fn=lambda seconds: None,
    )

    assert calls == [2, 2]
    assert result["cycles"] == 2
    assert result["processed"] == 1


def test_run_scheduler_loop_persists_cycle_outcomes():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    calls = []

    class FakeScheduler:
        def run_pending_batch(self, *, limit: int):
            calls.append(limit)
            if len(calls) == 1:
                return [{"final_status": "persisted", "item_type": "chapter", "item_id": "chapter_1"}]
            return []

    events = []
    with Session(engine) as session:
        result = run_scheduler_loop(
            scheduler=FakeScheduler(),
            batch_limit=2,
            poll_interval_seconds=0,
            max_cycles=4,
            sleep_fn=lambda seconds: None,
            session=session,
            event_callback=lambda event, payload: events.append((event, payload)),
        )

        rows = list(session.scalars(select(SchedulerRun).order_by(SchedulerRun.cycle_number.asc())))

    assert result["cycles"] == 4
    assert result["processed"] == 1
    assert len(rows) == 1
    assert rows[0].processed_count == 1
    assert rows[0].status == "processed"
    assert rows[0].outcomes == [
        {"final_status": "persisted", "item_type": "chapter", "item_id": "chapter_1"}
    ]
    assert result["status_counts"]["idle"] == 3
    assert [payload["status"] for event, payload in events if event == "worker.cycle.completed"] == [
        "processed", "idle", "idle", "idle"
    ]


def test_run_scheduler_loop_classifies_cycle_status_and_reason():
    outcomes = iter(
        [
            [{"final_status": "persisted", "item_type": "chapter", "item_id": "c1"}],
            [
                {
                    "final_status": "publish_pending",
                    "item_type": "publish_job",
                    "item_id": "p1",
                }
            ],
            [
                {
                    "final_status": "publish_failed",
                    "item_type": "publish_job",
                    "item_id": "p2",
                }
            ],
            [],
        ]
    )
    events = []

    class FakeScheduler:
        def run_pending_batch(self, *, limit: int):
            return next(outcomes)

    result = run_scheduler_loop(
        scheduler=FakeScheduler(),
        batch_limit=2,
        poll_interval_seconds=0,
        max_cycles=4,
        sleep_fn=lambda seconds: None,
        monotonic_fn=iter(
            [0.0, 0.010, 1.0, 1.020, 2.0, 2.030, 3.0, 3.040]
        ).__next__,
        event_callback=lambda event, payload: events.append((event, payload)),
    )

    assert result["status_counts"] == {
        "processed": 1,
        "attention_required": 1,
        "failed": 1,
        "idle": 1,
    }
    completed = [
        payload for event, payload in events if event == "worker.cycle.completed"
    ]
    assert [item["reason_code"] for item in completed] == [
        "chapter_persisted",
        "publish_retry_pending",
        "publish_failed",
        "no_pending_work",
    ]
    assert completed[0]["duration_ms"] == 10
    assert completed[0]["outcome_counts"] == {"persisted": 1}


def test_run_scheduler_loop_emits_cycle_failure_before_reraising():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    events = []

    class BrokenScheduler:
        def run_pending_batch(self, *, limit: int):
            raise RuntimeError("database unavailable")

    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="database unavailable"):
            run_scheduler_loop(
                scheduler=BrokenScheduler(),
                max_cycles=1,
                max_consecutive_failures=1,
                monotonic_fn=iter([0.0, 0.025]).__next__,
                session=session,
                event_callback=lambda event, payload: events.append((event, payload)),
            )
        rows = list(session.scalars(select(SchedulerRun)))

    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].reason_code == "cycle_exception"
    assert rows[0].duration_ms == 25
    assert rows[0].processed_count == 0
    assert rows[0].outcomes == []
    assert rows[0].outcome_counts == {}

    assert events[-1] == (
        "worker.cycle.failed",
        {
            "cycle_number": 1,
            "status": "failed",
            "reason_code": "cycle_exception",
            "duration_ms": 25,
            "processed_count": 0,
            "outcome_counts": {},
            "outcomes": [],
            "error": "database unavailable",
            "error_type": "RuntimeError",
        },
    )


def test_run_scheduler_loop_rolls_back_business_changes_before_recording_failure():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    events = []

    with Session(engine) as session:
        session.add(
            Book(
                id="book_failure_rollback",
                title="Failure Rollback",
                platform="fanqie",
                genre="urban",
            )
        )
        session.add(
            Chapter(
                id="chapter_failure_rollback",
                book_id="book_failure_rollback",
                chapter_number=1,
                title="Rollback Chapter",
                status="draft_planned",
            )
        )
        session.commit()

        class MutatingBrokenScheduler:
            def run_pending_batch(self, *, limit: int):
                chapter = session.get(Chapter, "chapter_failure_rollback")
                chapter.status = "writing"
                session.flush()
                raise RuntimeError("writer crashed after status transition")

        with pytest.raises(RuntimeError, match="writer crashed after status transition"):
            run_scheduler_loop(
                scheduler=MutatingBrokenScheduler(),
                max_cycles=1,
                max_consecutive_failures=1,
                monotonic_fn=iter([0.0, 0.012]).__next__,
                session=session,
                event_callback=lambda event, payload: events.append((event, payload)),
            )

        chapter = session.get(Chapter, "chapter_failure_rollback")
        rows = list(session.scalars(select(SchedulerRun)))

    assert chapter.status == "draft_planned"
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].reason_code == "cycle_exception"
    assert events[-1][0] == "worker.cycle.failed"
    assert events[-1][1]["error"] == "writer crashed after status transition"


def test_run_scheduler_loop_recovers_and_resets_failure_backoff():
    errors = iter([True, False, True, True])
    sleeps = []

    class FlakyScheduler:
        def run_pending_batch(self, *, limit: int):
            if next(errors):
                raise RuntimeError("temporary failure")
            return [{"final_status": "persisted"}]

    result = run_scheduler_loop(
        scheduler=FlakyScheduler(),
        poll_interval_seconds=5,
        max_consecutive_failures=3,
        max_cycles=4,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: 0.0,
    )

    assert result["cycles"] == 4
    assert result["processed"] == 1
    assert result["status_counts"]["failed"] == 3
    assert result["status_counts"]["processed"] == 1
    assert sleeps == [10, 5, 10]


def test_run_scheduler_loop_caps_backoff_and_reraises_fifth_consecutive_error():
    errors = [RuntimeError(f"failure {cycle}") for cycle in range(1, 6)]
    failures = iter(errors)
    sleeps = []

    class BrokenScheduler:
        def run_pending_batch(self, *, limit: int):
            raise next(failures)

    with pytest.raises(RuntimeError) as caught:
        run_scheduler_loop(
            scheduler=BrokenScheduler(),
            poll_interval_seconds=60,
            max_cycles=6,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0.0,
        )

    assert caught.value is errors[-1]
    assert sleeps == [120, 240, 300, 300]


def test_run_scheduler_loop_propagates_keyboard_interrupt_without_backoff():
    sleeps = []

    class InterruptedScheduler:
        def run_pending_batch(self, *, limit: int):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_scheduler_loop(
            scheduler=InterruptedScheduler(),
            max_cycles=1,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0.0,
        )

    assert sleeps == []
