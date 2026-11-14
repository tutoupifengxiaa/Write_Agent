from typing import get_args, get_type_hints

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    ContinuityEvent,
    PublishJob,
    SchedulerRun,
)
from app.orchestrator.chapter_workflow import ChapterWorkflow, ChapterWorkflowState
from app.orchestrator.retry_policy import classify_error
from app.orchestrator.scheduler import Scheduler
from app.orchestrator.scheduler_runner import run_scheduler_loop
from app.orchestrator.task_claims import claim_task, utc_now
from app.services.llm_client import LLMConfigurationError


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        database_session.add(
            Book(
                id="book_001",
                title="Test Book",
                platform="fanqie",
                genre="urban",
            )
        )
        database_session.add(
            Chapter(
                id="ch_0001",
                book_id="book_001",
                chapter_number=1,
                title="Chapter 1",
                goal={"objective": "Opening"},
                status="draft_planned",
            )
        )
        database_session.commit()
        yield database_session


def test_workflow_compiles_and_persists_direct_approval(session):
    revision_type = get_type_hints(ChapterWorkflowState)["revision"]
    assert type(None) in get_args(revision_type)

    workflow = ChapterWorkflow(
        session=session,
        writer=lambda context: {
            "content": "Original draft",
            "summary": "Draft summary",
            "state_deltas": [{"source": "draft"}],
        },
        reviewer=lambda content, context: {
            "approved": True,
            "review_score": 95,
            "summary": "Approved directly",
            "issues": [],
            "state_deltas": [{"source": "review"}],
        },
        create_publish_job=True,
    )

    result = workflow.invoke("ch_0001")

    assert result == {
        "final_status": "persisted",
        "item_type": "chapter",
        "item_id": "ch_0001",
    }
    chapter = session.get(Chapter, "ch_0001")
    assert chapter is not None
    assert chapter.status == "persisted"
    version = session.get(ChapterVersion, chapter.current_version_id)
    assert version is not None
    assert version.source == "approved"
    assert version.content == "Original draft"
    assert version.generation_metadata["review_trace"] == {
        "initial_review": {
            "approved": True,
            "review_score": 95,
            "summary": "Approved directly",
            "issues": [],
            "state_deltas": [{"source": "review"}],
        },
        "final_review": {
            "approved": True,
            "review_score": 95,
            "summary": "Approved directly",
            "issues": [],
            "state_deltas": [{"source": "review"}],
        },
    }
    events = session.scalars(
        select(ContinuityEvent).where(ContinuityEvent.chapter_id == "ch_0001")
    ).all()
    assert len(events) == 1
    assert events[0].delta == {"source": "review"}
    jobs = session.scalars(
        select(PublishJob).where(PublishJob.chapter_id == "ch_0001")
    ).all()
    assert len(jobs) == 1
    assert jobs[0].status == "publish_pending"


def test_workflow_persists_revision_after_complete_issue_mapping(session):
    class ReviewThenApprove:
        def __init__(self):
            self.calls = 0

        def __call__(self, content, context):
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 60,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_high",
                            "severity": "high",
                            "message": "Missing causal setup",
                        }
                    ],
                    "state_deltas": [{"review": "failed"}],
                }
            return {
                "approved": True,
                "review_score": 92,
                "summary": "Approved after revision",
                "issues": [],
                "state_deltas": [{"review": "passed"}],
            }

    reviewer = ReviewThenApprove()
    workflow = ChapterWorkflow(
        session=session,
        writer=lambda context: {
            "content": "Original draft",
            "summary": "Draft summary",
            "state_deltas": [{"source": "draft"}],
        },
        reviewer=reviewer,
        reviser=lambda content, instructions: {
            "revised_content": "  Revised draft  ",
            "summary": "Revision summary",
            "resolved_issue_ids": [" issue_high ", "issue_high", ""],
            "unresolved_issue_ids": [],
        },
    )

    result = workflow.invoke("ch_0001")

    assert result == {
        "final_status": "persisted",
        "item_type": "chapter",
        "item_id": "ch_0001",
    }
    chapter = session.get(Chapter, "ch_0001")
    assert chapter is not None
    version = session.get(ChapterVersion, chapter.current_version_id)
    assert version is not None
    assert version.source == "approved"
    assert version.content == "Revised draft"
    assert version.generation_metadata["review_trace"] == {
        "initial_review": {
            "approved": False,
            "review_score": 60,
            "summary": "Needs revision",
            "issues": [
                {
                    "id": "issue_high",
                    "severity": "high",
                    "message": "Missing causal setup",
                }
            ],
            "state_deltas": [{"review": "failed"}],
        },
        "revision": {
            "revised_content": "Revised draft",
            "summary": "Revision summary",
            "resolved_issue_ids": ["issue_high"],
            "unresolved_issue_ids": [],
        },
        "final_review": {
            "approved": True,
            "review_score": 92,
            "summary": "Approved after revision",
            "issues": [],
            "state_deltas": [{"review": "passed"}],
        },
    }
    events = session.scalars(select(ContinuityEvent)).all()
    assert [event.delta for event in events] == [{"review": "passed"}]


def test_workflow_persists_candidate_when_blocking_issue_remains(session):
    class ReviewThenApprove:
        def __init__(self):
            self.calls = 0

        def __call__(self, content, context):
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 60,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_medium",
                            "severity": "medium",
                            "message": "Continuity gap",
                        }
                    ],
                    "state_deltas": [],
                }
            return {
                "approved": True,
                "review_score": 94,
                "summary": "Model approved",
                "issues": [],
                "state_deltas": [],
            }

    class CountingReviser:
        def __init__(self):
            self.calls = 0

        def __call__(self, content, instructions):
            self.calls += 1
            return {
                "revised_content": "Revised but still blocked",
                "summary": "Attempted revision",
                "resolved_issue_ids": [],
                "unresolved_issue_ids": ["issue_medium"],
            }

    reviewer = ReviewThenApprove()
    reviser = CountingReviser()
    workflow = ChapterWorkflow(
        session=session,
        writer=lambda context: {
            "content": "Original draft",
            "summary": "Draft summary",
            "state_deltas": [],
        },
        reviewer=reviewer,
        reviser=reviser,
        create_publish_job=True,
    )

    result = workflow.invoke("ch_0001")

    assert result == {
        "final_status": "revision_pending",
        "item_type": "chapter",
        "item_id": "ch_0001",
    }
    assert reviser.calls == 1
    chapter = session.get(Chapter, "ch_0001")
    assert chapter is not None
    assert chapter.status == "revision_pending"
    version = session.get(ChapterVersion, chapter.current_version_id)
    assert version is not None
    assert version.source == "review_candidate"
    assert version.content == "Revised but still blocked"
    trace = version.generation_metadata["review_trace"]
    assert trace["initial_review"]["issues"][0]["id"] == "issue_medium"
    assert trace["revision"]["unresolved_issue_ids"] == ["issue_medium"]
    assert trace["final_review"]["approved"] is True
    assert trace["mapping_complete"] is True
    assert trace["unresolved_issue_ids"] == ["issue_medium"]
    assert (chapter.goal or {})["review_trace"] == trace
    assert session.scalars(select(ContinuityEvent)).all() == []
    assert session.scalars(select(PublishJob)).all() == []

    second_chapter = Chapter(
        id="ch_0002",
        book_id="book_001",
        chapter_number=2,
        title="Chapter 2",
        goal={"objective": "Continue"},
        status="draft_planned",
    )
    session.add(second_chapter)
    session.commit()
    workflow_without_reviser = ChapterWorkflow(
        session=session,
        writer=lambda context: {
            "content": "Second original draft",
            "summary": "Second draft summary",
            "state_deltas": [],
        },
        reviewer=lambda content, context: {
            "approved": False,
            "review_score": 58,
            "summary": "Second chapter needs revision",
            "issues": [
                {
                    "id": "issue_second",
                    "severity": "high",
                    "message": "Second chapter continuity gap",
                }
            ],
            "state_deltas": [],
        },
        create_publish_job=True,
    )

    second_result = workflow_without_reviser.invoke("ch_0002")

    assert second_result == {
        "final_status": "revision_pending",
        "item_type": "chapter",
        "item_id": "ch_0002",
    }
    refreshed_second = session.get(Chapter, "ch_0002")
    assert refreshed_second is not None
    second_version = session.get(
        ChapterVersion, refreshed_second.current_version_id
    )
    assert second_version is not None
    second_trace = second_version.generation_metadata["review_trace"]
    assert second_trace["revision"] is None
    assert (refreshed_second.goal or {})["review_trace"]["revision"] is None
    assert session.scalars(
        select(ContinuityEvent).where(ContinuityEvent.chapter_id == "ch_0002")
    ).all() == []
    assert session.scalars(
        select(PublishJob).where(PublishJob.chapter_id == "ch_0002")
    ).all() == []


def test_worker_rolls_back_graph_node_failure_before_recording_cycle_failure(session):
    class FlushThenFailWriter:
        def __init__(self):
            self.calls = 0

        def __call__(self, context):
            self.calls += 1
            chapter = session.get(Chapter, "ch_0001")
            assert chapter is not None
            chapter.goal = {"intermediate": True}
            session.flush()
            raise RuntimeError("writer exploded")

    writer = FlushThenFailWriter()
    workflow = ChapterWorkflow(
        session=session,
        writer=writer,
        reviewer=lambda content, context: {
            "approved": True,
            "review_score": 95,
        },
    )

    with pytest.raises(RuntimeError, match="writer exploded"):
        workflow.invoke("ch_0001")

    chapter = session.get(Chapter, "ch_0001")
    assert chapter is not None
    assert chapter.status == "generation_failed"
    assert chapter.goal == {"objective": "Opening"}
    assert session.scalars(select(ChapterVersion)).all() == []

    chapter.status = "draft_planned"
    session.commit()

    class WorkflowScheduler:
        def run_pending_batch(self, *, limit):
            return [workflow.invoke("ch_0001")]

    with pytest.raises(RuntimeError, match="writer exploded"):
        run_scheduler_loop(
            scheduler=WorkflowScheduler(),
            batch_limit=1,
            max_cycles=1,
            max_consecutive_failures=1,
            session=session,
        )

    assert writer.calls == 2
    chapter = session.get(Chapter, "ch_0001")
    assert chapter is not None
    assert chapter.status == "generation_failed"
    assert chapter.goal == {"objective": "Opening"}
    assert session.scalars(select(ChapterVersion)).all() == []
    runs = session.scalars(select(SchedulerRun)).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].reason_code == "cycle_exception"


def test_configuration_error_releases_claim_and_stops_batch(session):
    first = session.get(Chapter, "ch_0001")
    first.attempt_count = 1
    second = Chapter(id="ch_0002", book_id=first.book_id, chapter_number=2, title="Next")
    session.add(second)
    session.commit()
    calls = []

    def unconfigured_writer(context):
        calls.append(context)
        first.title = "Uncommitted edit"
        session.flush()
        raise LLMConfigurationError("Model configuration incomplete")

    scheduler = Scheduler(session=session, writer=unconfigured_writer, reviewer=None)
    with pytest.raises(LLMConfigurationError):
        scheduler.run_pending_batch(limit=2)

    assert len(calls) == 1
    assert first.title == "Chapter 1"
    assert first.status == "draft_planned"
    assert first.attempt_count == 1
    assert first.claim_token is first.lease_expires_at is None
    assert second.status == "draft_planned"
    assert second.attempt_count == 0
    assert second.claim_token is None
    assert claim_task(session, Chapter, first.id, "draft_planned") is not None
    assert first.attempt_count == 2


def test_configuration_error_does_not_release_another_workers_claim(session):
    replacement = []

    def reclaimed_while_writing(context):
        with Session(session.get_bind()) as other:
            chapter = other.get(Chapter, "ch_0001")
            chapter.lease_expires_at = utc_now()
            other.commit()
            replacement.append(claim_task(other, Chapter, chapter.id, "draft_planned"))
        raise LLMConfigurationError("Model configuration incomplete")

    scheduler = Scheduler(session=session, writer=reclaimed_while_writing, reviewer=None)
    with pytest.raises(LLMConfigurationError):
        scheduler.run_pending_batch(limit=1)
    chapter = session.get(Chapter, "ch_0001")
    assert replacement[0] is not None
    assert chapter.claim_token == replacement[0]
    assert chapter.lease_expires_at is not None
    assert chapter.attempt_count == 2
    assert chapter.status == "draft_planned"


def test_retry_policy_distinguishes_configuration_and_transport_errors():
    assert classify_error(LLMConfigurationError("Model configuration incomplete")) == "configuration_error"
    assert classify_error(httpx.ConnectError("offline")) == "transient_error"
    assert classify_error(httpx.RemoteProtocolError("unexpected end")) == "transient_error"
