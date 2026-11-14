import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.persister import persist_approved_chapter
from app.agents.planner import plan_next_chapter
from app.agents.reviser import revise_chapter
from app.db.base import Base
from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    Character,
    CharacterSnapshot,
    ContinuityEvent,
    PublishJob,
    StoryArc,
)
from app.db.repositories import PublishJobRepository
from app.orchestrator.scheduler import Scheduler


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_book_and_chapter(session: Session) -> Chapter:
    book = Book(
        id="book_001",
        title="Test Book",
        platform="fanqie",
        genre="urban",
    )
    chapter = Chapter(
        id="ch_0001",
        book_id="book_001",
        chapter_number=1,
        title="Chapter 1",
        goal={"objective": "Opening"},
        status="approved",
    )
    session.add(book)
    session.add(chapter)
    session.commit()
    return chapter


@pytest.fixture
def fake_session():
    session = _build_session()
    _seed_book_and_chapter(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_services(fake_session):
    class FakeWriter:
        def __call__(self, context: dict) -> dict:
            return {
                "content": f"Draft for {context['chapter_goal']['objective']}",
                "summary": "Generated summary",
                "state_deltas": [{"status": "advanced"}],
            }

    class PassingReviewer:
        def __call__(self, content: str, canonical_facts: dict) -> dict:
            assert canonical_facts["chapter_goal"]["objective"] == "Opening"
            return {
                "approved": True,
                "review_score": 95,
                "summary": f"Approved: {content}",
                "state_deltas": [{"review": "passed"}],
            }

    class FakeServices:
        def __init__(self, session: Session) -> None:
            self.session = session
            self.writer = FakeWriter()
            self.reviewer = PassingReviewer()
            self.scheduler = Scheduler(
                session=session,
                writer=self.writer,
                reviewer=self.reviewer,
            )

    return FakeServices(fake_session)


def test_scheduler_run_once_delegates_to_chapter_workflow(fake_session):
    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {},
        reviewer=lambda content, context: {},
    )
    called_chapter_ids = []
    expected = {
        "final_status": "persisted",
        "item_type": "chapter",
        "item_id": "ch_0001",
    }
    scheduler.chapter_workflow.invoke = lambda chapter_id: (
        called_chapter_ids.append(chapter_id) or expected
    )

    result = scheduler.run_once("ch_0001")

    assert called_chapter_ids == ["ch_0001"]
    assert result == expected


def test_scheduler_moves_chapter_from_planned_to_persisted(fake_services):
    chapter = fake_services.session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_services.session.commit()

    result = fake_services.scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_services.session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert result["final_status"] == "persisted"
    assert refreshed is not None
    assert refreshed.status == "persisted"
    assert refreshed.current_version_id is not None


def test_scheduler_uses_memory_backed_context_when_story_state_exists(fake_session):
    chapter = fake_session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.add(
        StoryArc(
            book_id="book_001",
            arc_type="main",
            chapter_start=1,
            chapter_end=10,
            stage_goal="solve the first case",
            outline={"beats": ["clue", "suspect"]},
        )
    )
    character = Character(
        book_id="book_001",
        name="Lin Fan",
        role="lead",
        age=18,
        profile={"trait": "calm"},
        last_updated_chapter_number=1,
    )
    fake_session.add(character)
    fake_session.flush()
    fake_session.add(
        CharacterSnapshot(
            character_id=character.id,
            chapter_id="ch_0001",
            state={"mood": "focused"},
        )
    )
    fake_session.add(Chapter(id="history", book_id="book_001", chapter_number=0, title="History", approved_version_id="history_version"))
    fake_session.add(
        ChapterVersion(
            id="history_version",
            chapter_id="history",
            source="approved",
            content="Previous chapter body",
            summary="previous summary",
            review_score=90,
        )
    )
    fake_session.commit()

    captured_context: dict = {}

    class InspectingWriter:
        def __call__(self, context: dict) -> dict:
            captured_context.update(context)
            return {
                "content": "Draft from memory context",
                "summary": "Generated summary",
                "state_deltas": [],
            }

    class PassingReviewer:
        def __call__(self, content: str, canonical_facts: dict) -> dict:
            return {
                "approved": True,
                "review_score": 95,
                "summary": "Approved",
                "state_deltas": [],
            }

    scheduler = Scheduler(
        session=fake_session,
        writer=InspectingWriter(),
        reviewer=PassingReviewer(),
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    assert result["final_status"] == "persisted"
    assert captured_context["world_state"]["active_story_arcs"][0]["stage_goal"] == "solve the first case"
    assert captured_context["character_states"][0]["name"] == "Lin Fan"
    assert "previous summary" in captured_context["recent_summaries"]


def test_scheduler_finds_and_processes_next_planned_chapter(fake_services):
    chapter = fake_services.session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_services.session.commit()

    result = fake_services.scheduler.run_next_pending()

    refreshed = fake_services.session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert result["final_status"] == "persisted"
    assert refreshed is not None
    assert refreshed.status == "persisted"


@pytest.mark.parametrize(
    ("chapter_numbers", "limit", "expected"),
    [
        ([[1, 2, 3], [90]], 2, ["b0_c1", "b1_c90"]),
        ([[1, 2, 3], [40, 41], [90, 91]], 6,
         ["b0_c1", "b1_c40", "b2_c90", "b0_c2", "b1_c41", "b2_c91"]),
        ([[1, 2, 3]], 3, ["b0_c1", "b0_c2", "b0_c3"]),
    ],
    ids=["two-books", "two-rounds", "one-book"],
)
def test_scheduler_shares_batch_across_ready_books(fake_services, chapter_numbers, limit, expected):
    session = fake_services.session
    for book_index, numbers in enumerate(chapter_numbers):
        book_id = f"b{book_index}"
        session.add(Book(id=book_id, title=book_id, platform="local", genre="fiction"))
        for number in numbers:
            session.add(Chapter(
                id=f"{book_id}_c{number}", book_id=book_id, chapter_number=number,
                title=f"Chapter {number}", goal={"objective": "Opening"}, status="draft_planned",
            ))
    session.commit()

    results = fake_services.scheduler.run_pending_batch(limit=limit)
    assert [item["item_id"] for item in results] == expected
    assert all(item["final_status"] == "persisted" for item in results)


def test_scheduler_returns_idle_when_no_planned_chapter_exists(fake_services):
    chapter = fake_services.session.scalar(
        select(Chapter).where(Chapter.id == "ch_0001")
    )
    assert chapter is not None
    chapter.status = "approved"
    fake_services.session.commit()

    result = fake_services.scheduler.run_next_pending()

    assert result == {"final_status": "idle"}


def test_scheduler_processes_pending_publish_job_before_planned_chapter(fake_session):
    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "persisted"
    fake_session.add(
        PublishJob(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
        )
    )
    planned_chapter = Chapter(
        id="ch_0002",
        book_id="book_001",
        chapter_number=2,
        title="Chapter 2",
        goal={"objective": "Continue"},
        status="draft_planned",
    )
    fake_session.add(planned_chapter)
    version = ChapterVersion(
        chapter_id=chapter.id,
        source="approved",
        content="Persisted body",
        summary="Persisted summary",
        review_score=95,
    )
    fake_session.add(version)
    fake_session.flush()
    chapter.current_version_id = version.id
    for job in fake_session.scalars(select(PublishJob).where(PublishJob.chapter_id == chapter.id)):
        job.chapter_version_id = version.id
        job.chapter_title = chapter.title
    fake_session.commit()

    class FakePublisher:
        def __call__(self, chapter_title: str, chapter_content: str) -> dict:
            assert chapter_title == "Chapter 1"
            assert chapter_content == "Persisted body"
            return {
                "success": True,
                "platform_draft_id": "draft_001",
                "screenshot_path": "logs/draft_001.png",
            }

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {"content": "unused"},
        reviewer=lambda content, facts: {"approved": True, "review_score": 90},
        publisher=FakePublisher(),
    )

    result = scheduler.run_next_pending()

    job = fake_session.scalar(select(PublishJob).where(PublishJob.chapter_id == "ch_0001"))
    refreshed_planned = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0002"))
    assert result["final_status"] == "draft_uploaded"
    assert job is not None
    assert job.status == "draft_uploaded"
    assert job.platform_draft_id == "draft_001"
    assert refreshed_planned is not None
    assert refreshed_planned.status == "draft_planned"


def test_scheduler_marks_publish_job_failed_when_publisher_raises(fake_session):
    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "persisted"
    fake_session.add(
        PublishJob(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
        )
    )
    version = ChapterVersion(
        chapter_id=chapter.id,
        source="approved",
        content="Persisted body",
        summary="Persisted summary",
        review_score=95,
    )
    fake_session.add(version)
    fake_session.flush()
    chapter.current_version_id = version.id
    for job in fake_session.scalars(select(PublishJob).where(PublishJob.chapter_id == chapter.id)):
        job.chapter_version_id = version.id
        job.chapter_title = chapter.title
    fake_session.commit()

    class FailingPublisher:
        def __call__(self, chapter_title: str, chapter_content: str) -> dict:
            raise RuntimeError("fatal publish crash")

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {"content": "unused"},
        reviewer=lambda content, facts: {"approved": True, "review_score": 90},
        publisher=FailingPublisher(),
    )

    result = scheduler.run_next_pending()

    job = fake_session.scalar(select(PublishJob).where(PublishJob.chapter_id == "ch_0001"))
    assert result["final_status"] == "publish_failed"
    assert job is not None
    assert job.status == "publish_failed"
    assert job.error_message == "RuntimeError: business_error"


def test_scheduler_rejects_cross_chapter_publish_version_without_calling_publisher(
    fake_session,
):
    target = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert target is not None
    target.status = "persisted"
    foreign = Chapter(
        id="ch_0002",
        book_id="book_001",
        chapter_number=2,
        title="Foreign Chapter",
        status="persisted",
    )
    foreign_version = ChapterVersion(
        chapter_id=foreign.id,
        source="approved",
        content="Foreign body must never be published for the target",
        summary="Foreign summary",
        review_score=95,
    )
    job = PublishJob(
        chapter_id=target.id,
        platform="fanqie",
        mode="draft_only",
        status="publish_pending",
    )
    fake_session.add_all([foreign, foreign_version, job])
    fake_session.flush()
    target.current_version_id = foreign_version.id
    job.chapter_version_id = foreign_version.id
    job.chapter_title = target.title
    fake_session.commit()
    publish_calls = []

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {"content": "unused"},
        reviewer=lambda content, facts: {"approved": True, "review_score": 90},
        publisher=lambda title, content: publish_calls.append((title, content)),
    )

    result = scheduler.run_publish_job(job.id)

    fake_session.refresh(job)
    assert result == {
        "final_status": "publish_failed",
        "item_type": "publish_job",
        "item_id": job.id,
        "error_type": "business_error",
    }
    assert publish_calls == []
    assert job.status == "publish_failed"
    assert "approved version" in job.error_message.lower()
    assert "Foreign body" not in job.error_message


def test_scheduler_keeps_publish_job_pending_for_retryable_platform_error(fake_session):
    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "persisted"
    fake_session.add(
        PublishJob(
            chapter_id=chapter.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
        )
    )
    version = ChapterVersion(
        chapter_id=chapter.id,
        source="approved",
        content="Persisted body",
        summary="Persisted summary",
        review_score=95,
    )
    fake_session.add(version)
    fake_session.flush()
    chapter.current_version_id = version.id
    for job in fake_session.scalars(select(PublishJob).where(PublishJob.chapter_id == chapter.id)):
        job.chapter_version_id = version.id
        job.chapter_title = chapter.title
    fake_session.commit()

    class RetryablePublisher:
        def __call__(self, chapter_title: str, chapter_content: str) -> dict:
            raise RuntimeError("platform timeout")

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {"content": "unused"},
        reviewer=lambda content, facts: {"approved": True, "review_score": 90},
        publisher=RetryablePublisher(),
    )

    result = scheduler.run_next_pending()

    job = fake_session.scalar(select(PublishJob).where(PublishJob.chapter_id == "ch_0001"))
    assert result["final_status"] == "publish_pending"
    assert result["error_type"] == "platform_error"
    assert job is not None
    assert job.status == "publish_pending"
    assert job.error_message == "RuntimeError: platform_error"


def test_scheduler_publish_retry_does_not_block_chapter_in_same_batch(fake_session):
    first = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert first is not None
    first.status = "persisted"
    fake_session.add(
        PublishJob(
            chapter_id=first.id,
            platform="fanqie",
            mode="draft_only",
            status="publish_pending",
        )
    )
    version = ChapterVersion(
        chapter_id=first.id,
        source="approved",
        content="Persisted body",
        summary="Persisted summary",
        review_score=95,
    )
    fake_session.add(version)
    fake_session.flush()
    first.current_version_id = version.id
    for job in fake_session.scalars(select(PublishJob).where(PublishJob.chapter_id == first.id)):
        job.chapter_version_id = version.id
        job.chapter_title = first.title

    second = Chapter(
        id="ch_0002",
        book_id="book_001",
        chapter_number=2,
        title="Chapter 2",
        goal={"objective": "Continue"},
        status="draft_planned",
    )
    fake_session.add(second)
    fake_session.commit()

    publish_calls = []

    class RetryablePublisher:
        def __call__(self, chapter_title: str, chapter_content: str) -> dict:
            publish_calls.append((chapter_title, chapter_content))
            raise RuntimeError("platform timeout")

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {
            "content": f"Draft for {context['chapter_goal']['objective']}",
            "summary": "Generated summary",
            "state_deltas": [],
        },
        reviewer=lambda content, facts: {
            "approved": True,
            "review_score": 95,
            "summary": "Approved",
            "state_deltas": [],
        },
        publisher=RetryablePublisher(),
    )

    results = scheduler.run_pending_batch(limit=2)

    refreshed_second = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0002"))
    assert len(publish_calls) == 1
    assert len(results) == 2
    assert results[0]["final_status"] == "publish_pending"
    assert results[0]["item_id"] != results[1]["item_id"]
    assert results[1]["item_type"] == "chapter"
    assert results[1]["final_status"] == "persisted"
    assert refreshed_second is not None
    assert refreshed_second.status == "persisted"


def test_scheduler_returns_revision_pending_when_review_fails(fake_session):
    class FakeWriter:
        def __call__(self, context: dict) -> dict:
            return {
                "content": f"Draft for {context['chapter_goal']['objective']}",
                "summary": "Generated summary",
                "state_deltas": [{"status": "advanced"}],
            }

    class ReviewThenApprove:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            assert canonical_facts["chapter_goal"]["objective"] == "Opening"
            self.calls += 1
            if self.calls > 1:
                return {
                    "approved": True,
                    "review_score": 90,
                    "summary": "Model approved the revision",
                    "issues": [],
                    "state_deltas": [],
                }
            return {
                "approved": False,
                "review_score": 61,
                "summary": "Needs revision",
                "issues": [
                    {
                        "id": "issue_001",
                        "type": "continuity",
                        "severity": "high",
                        "message": "character reaction is too abrupt",
                        "evidence": "The reaction contradicts the prior state.",
                        "source": "model",
                    },
                    {
                        "type": "logic",
                        "severity": "medium",
                        "message": "blocking issue has no identifier",
                        "evidence": "The causal transition is unsupported.",
                        "source": "model",
                    },
                ],
                "state_deltas": [{"review": "failed"}],
            }

    class UnresolvedReviser:
        def __call__(self, original_content: str, revision_instructions: dict) -> dict:
            return {
                "revised_content": "Revised but still inconsistent",
                "summary": "Attempted revision",
                "resolved_issue_ids": [],
                "unresolved_issue_ids": ["issue_001"],
                "change_summary": [],
            }

    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=FakeWriter(),
        reviewer=ReviewThenApprove(),
        reviser=UnresolvedReviser(),
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    versions = fake_session.scalars(
        select(ChapterVersion).where(ChapterVersion.chapter_id == "ch_0001")
    ).all()
    assert result["final_status"] == "revision_pending"
    assert result["item_type"] == "chapter"
    assert result["item_id"] == "ch_0001"
    assert refreshed is not None
    assert refreshed.status == "revision_pending"
    assert refreshed.current_version_id is not None
    assert len(versions) == 1
    assert versions[0].id == refreshed.current_version_id
    assert versions[0].source == "review_candidate"
    assert versions[0].content == "Revised but still inconsistent"
    review_trace = versions[0].generation_metadata["review_trace"]
    assert review_trace["mapping_complete"] is False
    assert review_trace["unresolved_issue_ids"] == ["issue_001"]
    assert (refreshed.goal or {})["review_trace"] == review_trace
    jobs = fake_session.scalars(
        select(PublishJob).where(PublishJob.chapter_id == "ch_0001")
    ).all()
    assert jobs == []


def test_scheduler_auto_revises_once_before_revision_pending(fake_session):
    class FakeWriter:
        def __call__(self, context: dict) -> dict:
            return {
                "content": "Draft with continuity issue",
                "summary": "Generated summary",
                "state_deltas": [],
            }

    class ReviewThenApprove:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 61,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_001",
                            "type": "continuity",
                            "severity": "high",
                            "message": "character reaction is too abrupt",
                            "evidence": "The clue is missing from the reaction.",
                            "source": "model",
                        }
                    ],
                    "state_deltas": [],
                }
            return {
                "approved": True,
                "review_score": 90,
                "summary": "Approved after revision",
                "state_deltas": [],
            }

    class FakeReviser:
        def __call__(self, original_content: str, revision_instructions: dict) -> dict:
            assert "issues" in revision_instructions
            assert "revision_priorities" in revision_instructions
            return {
                "revised_content": "Revised draft",
                "summary": "Revision complete",
                "resolved_issue_ids": ["issue_001"],
                "unresolved_issue_ids": [],
                "change_summary": [
                    {"issue_id": "issue_001", "change": "restored the clue"}
                ],
            }

    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    reviewer = ReviewThenApprove()
    scheduler = Scheduler(
        session=fake_session,
        writer=FakeWriter(),
        reviewer=reviewer,
        reviser=FakeReviser(),
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert result["final_status"] == "persisted"
    assert refreshed is not None
    assert refreshed.status == "persisted"
    version = fake_session.get(ChapterVersion, refreshed.current_version_id)
    assert version is not None
    assert version.generation_metadata["planning_trace"] is None
    assert version.generation_metadata["retrieval_evidence"] == []
    assert version.generation_metadata["retrieval_mode"] == "none"
    review_trace = version.generation_metadata["review_trace"]
    assert review_trace["initial_review"]["issues"][0]["id"] == "issue_001"
    assert review_trace["revision"]["resolved_issue_ids"] == ["issue_001"]
    assert review_trace["final_review"]["approved"] is True
    jobs = fake_session.scalars(
        select(PublishJob).where(PublishJob.chapter_id == "ch_0001")
    ).all()
    assert len(jobs) == 1
    assert jobs[0].status == "publish_pending"


def test_scheduler_persists_when_only_low_severity_issue_remains_unresolved(
    fake_session,
):
    class ReviewThenApprove:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 60,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_high",
                            "type": "logic",
                            "severity": "high",
                            "message": "The clue has no causal link.",
                            "evidence": "The clue appears without setup.",
                            "source": "model",
                        },
                        {
                            "id": "issue_low",
                            "type": "style",
                            "severity": "low",
                            "message": "One sentence is repetitive.",
                            "evidence": "The same phrase is used twice.",
                            "source": "model",
                        },
                    ],
                    "state_deltas": [],
                }
            return {
                "approved": True,
                "review_score": 92,
                "summary": "Model approved the revision",
                "issues": [],
                "state_deltas": [],
            }

    chapter = fake_session.get(Chapter, "ch_0001")
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {
            "content": "Draft",
            "summary": "Draft summary",
            "state_deltas": [],
        },
        reviewer=ReviewThenApprove(),
        reviser=lambda content, instructions: {
            "revised_content": "Revised draft",
            "summary": "Revision summary",
            "resolved_issue_ids": ["issue_high"],
            "unresolved_issue_ids": ["issue_low"],
            "change_summary": [
                {"issue_id": "issue_high", "change": "Added the causal setup."}
            ],
        },
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.get(Chapter, "ch_0001")
    assert result["final_status"] == "persisted"
    assert refreshed is not None
    assert refreshed.status == "persisted"
    version = fake_session.get(ChapterVersion, refreshed.current_version_id)
    assert version is not None
    trace = version.generation_metadata["review_trace"]
    assert trace["revision"]["unresolved_issue_ids"] == ["issue_low"]
    assert trace["final_review"]["approved"] is True


def test_scheduler_keeps_revision_pending_when_blocking_issue_remains_unresolved(
    fake_session,
):
    class ReviewThenApprove:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 60,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_001",
                            "type": "logic",
                            "severity": "high",
                            "message": "The clue has no causal link.",
                            "evidence": "The clue appears without setup.",
                            "source": "model",
                        }
                    ],
                    "state_deltas": [],
                }
            return {
                "approved": True,
                "review_score": 92,
                "summary": "Model approved the revision",
                "issues": [],
                "state_deltas": [],
            }

    chapter = fake_session.get(Chapter, "ch_0001")
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {
            "content": "Draft",
            "summary": "Draft summary",
            "state_deltas": [],
        },
        reviewer=ReviewThenApprove(),
        reviser=lambda content, instructions: {
            "revised_content": "Revised draft",
            "summary": "Revision summary",
            "resolved_issue_ids": [],
            "unresolved_issue_ids": ["issue_001"],
            "change_summary": [],
        },
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.get(Chapter, "ch_0001")
    assert result["final_status"] == "revision_pending"
    assert refreshed is not None
    candidate = fake_session.get(ChapterVersion, refreshed.current_version_id)
    assert candidate is not None
    assert candidate.source == "review_candidate"
    assert candidate.generation_metadata["review_trace"]["unresolved_issue_ids"] == [
        "issue_001"
    ]
    assert fake_session.scalars(select(PublishJob)).all() == []


@pytest.mark.parametrize(
    "reviser_result",
    [
        None,
        {
            "revised_content": "Malformed mapping revision",
            "summary": "Malformed mapping",
            "resolved_issue_ids": {"issue_001": True},
            "unresolved_issue_ids": "issue_extra",
            "change_summary": [],
        },
    ],
)
def test_scheduler_safely_rejects_malformed_reviser_mapping(
    fake_session,
    reviser_result,
):
    class ReviewThenApprove:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "approved": False,
                    "review_score": 60,
                    "summary": "Needs revision",
                    "issues": [
                        {
                            "id": "issue_001",
                            "type": "logic",
                            "severity": "high",
                            "message": "The setup is incomplete.",
                            "evidence": "The transition has no cause.",
                            "source": "model",
                        }
                    ],
                    "state_deltas": [],
                }
            return {
                "approved": True,
                "review_score": 90,
                "summary": "Model approved malformed mapping",
                "issues": [],
                "state_deltas": [],
            }

    chapter = fake_session.get(Chapter, "ch_0001")
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {
            "content": "Original draft",
            "summary": "Original summary",
            "state_deltas": [],
        },
        reviewer=ReviewThenApprove(),
        reviser=lambda content, instructions: reviser_result,
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.get(Chapter, "ch_0001")
    assert result["final_status"] == "revision_pending"
    assert refreshed is not None
    candidate = fake_session.get(ChapterVersion, refreshed.current_version_id)
    assert candidate is not None
    assert candidate.source == "review_candidate"
    trace = candidate.generation_metadata["review_trace"]
    assert trace["mapping_complete"] is False
    if isinstance(reviser_result, dict):
        assert trace["revision"]["resolved_issue_ids"] == []
        assert trace["revision"]["unresolved_issue_ids"] == []
    else:
        assert trace["revision"] == {}
    assert fake_session.scalars(select(PublishJob)).all() == []


@pytest.mark.parametrize(
    "malformed_content",
    [
        ["not", "chapter", "text"],
        {"content": "not chapter text"},
        42,
        "   ",
    ],
)
def test_scheduler_uses_original_candidate_for_invalid_revised_content(
    fake_session,
    malformed_content,
):
    class RejectingReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, content: str, canonical_facts: dict) -> dict:
            self.calls += 1
            return {
                "approved": False,
                "review_score": 60,
                "summary": "Needs revision",
                "issues": [
                    {
                        "id": "issue_001",
                        "type": "logic",
                        "severity": "high",
                        "message": "The setup is incomplete.",
                        "evidence": "The transition has no cause.",
                        "source": "model",
                    }
                ],
                "state_deltas": [],
            }

    chapter = fake_session.get(Chapter, "ch_0001")
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    reviewer = RejectingReviewer()
    scheduler = Scheduler(
        session=fake_session,
        writer=lambda context: {
            "content": "Original safe draft",
            "summary": "Original summary",
            "state_deltas": [],
        },
        reviewer=reviewer,
        reviser=lambda content, instructions: {
            "revised_content": malformed_content,
            "summary": "Invalid revision",
            "resolved_issue_ids": ["issue_001"],
            "unresolved_issue_ids": [],
            "change_summary": [],
        },
        publisher=lambda title, content: {"success": True},
    )

    result = scheduler.run_once(chapter_id="ch_0001")

    refreshed = fake_session.get(Chapter, "ch_0001")
    assert result["final_status"] == "revision_pending"
    assert reviewer.calls == 1
    assert refreshed is not None
    candidate = fake_session.get(ChapterVersion, refreshed.current_version_id)
    assert candidate is not None
    assert candidate.source == "review_candidate"
    assert candidate.content == "Original safe draft"
    assert fake_session.scalars(select(PublishJob)).all() == []


def test_scheduler_raises_clear_error_when_writer_content_missing(fake_session):
    class IncompleteWriter:
        def __call__(self, context: dict) -> dict:
            return {
                "summary": "Generated summary",
                "state_deltas": [{"status": "advanced"}],
            }

    class PassingReviewer:
        def __call__(self, content: str, canonical_facts: dict) -> dict:
            return {
                "approved": True,
                "review_score": 95,
                "summary": f"Approved: {content}",
                "state_deltas": [{"review": "passed"}],
            }

    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "draft_planned"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=IncompleteWriter(),
        reviewer=PassingReviewer(),
    )

    with pytest.raises(ValueError, match="writer output missing required field: content"):
        scheduler.run_once(chapter_id="ch_0001")


def test_scheduler_raises_for_invalid_initial_state(fake_session):
    class FakeWriter:
        def __call__(self, context: dict) -> dict:
            return {
                "content": f"Draft for {context['chapter_goal']['objective']}",
                "summary": "Generated summary",
                "state_deltas": [{"status": "advanced"}],
            }

    class PassingReviewer:
        def __call__(self, content: str, canonical_facts: dict) -> dict:
            return {
                "approved": True,
                "review_score": 95,
                "summary": f"Approved: {content}",
                "state_deltas": [{"review": "passed"}],
            }

    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "unknown_state"
    fake_session.commit()

    scheduler = Scheduler(
        session=fake_session,
        writer=FakeWriter(),
        reviewer=PassingReviewer(),
    )

    with pytest.raises(ValueError, match="unknown state: unknown_state"):
        scheduler.run_once(chapter_id="ch_0001")


def test_persist_approved_chapter_creates_version_and_summary(fake_session):
    result = persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="正文",
        summary="摘要",
        review_score=90,
        state_deltas=[{"wealth": 1000}],
    )

    assert result["db_saved"] is True
    assert result["chapter_version_id"]

    version = fake_session.scalar(
        select(ChapterVersion).where(
            ChapterVersion.id == result["chapter_version_id"]
        )
    )
    assert version is not None
    assert version.summary == "摘要"
    assert version.review_score == 90

    events = fake_session.scalars(
        select(ContinuityEvent).where(ContinuityEvent.chapter_id == "ch_0001")
    ).all()
    assert len(events) == 1
    assert events[0].delta == {"wealth": 1000}


def test_plan_next_chapter_returns_expected_shape():
    plan = plan_next_chapter(
        chapter_number=2,
        chapter_goal="????",
        must_include=["????"],
        must_avoid=["????"],
        target_words=2500,
    )

    assert plan == {
        "objective": "????",
        "must_include": ["????"],
        "must_avoid": ["????"],
        "target_words": 2500,
        "chapter_number": 2,
        "context_signals": {
            "story_arc_goals": [],
            "character_state_hints": [],
        },
        "execution_beats": ["????"],
        "continuity_focus": ["????"],
    }


def test_persist_approved_chapter_updates_status_to_persisted(fake_session):
    persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="正文",
        summary="摘要",
        review_score=90,
        state_deltas=[{"wealth": 1000}],
    )

    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    assert chapter.status == "persisted"
    assert chapter.current_version_id is not None


def test_revise_chapter_calls_generate_json_with_prompt_string():
    class FakeClient:
        def __init__(self) -> None:
            self.prompt = None

        def generate_json(self, prompt: str) -> dict:
            self.prompt = prompt
            return {"revised_content": "已修订"}

    client = FakeClient()

    result = revise_chapter(
        client,
        original_content="主角在雨夜追凶",
        revision_instructions={
            "focus": "强化悬念",
            "issues": [{"id": "issue_001", "message": "线索没有闭环"}],
        },
    )

    assert result == {"revised_content": "已修订"}
    assert isinstance(client.prompt, str)
    assert "主角在雨夜追凶" in client.prompt
    assert "强化悬念" in client.prompt
    assert "issue_001" in client.prompt
    assert "resolved_issue_ids" in client.prompt
    assert "unresolved_issue_ids" in client.prompt


def test_persist_approved_chapter_defers_commit_to_caller(fake_session):
    result = persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="正文",
        summary="摘要",
        review_score=90,
        state_deltas=[{"wealth": 1000}],
    )

    version_id = result["chapter_version_id"]
    fake_session.rollback()

    version = fake_session.scalar(
        select(ChapterVersion).where(ChapterVersion.id == version_id)
    )
    events = fake_session.scalars(
        select(ContinuityEvent).where(ContinuityEvent.chapter_id == "ch_0001")
    ).all()
    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))

    assert version is None
    assert events == []
    assert chapter is not None
    assert chapter.status == "approved"
    assert chapter.current_version_id is None


def test_persist_approved_chapter_creates_unique_versions_on_repeat(fake_session):
    first = persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="正文一",
        summary="摘要一",
        review_score=90,
        state_deltas=[{"wealth": 1000}],
    )
    fake_session.flush()

    second = persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="正文二",
        summary="摘要二",
        review_score=91,
        state_deltas=[{"wealth": 1200}],
    )

    assert first["chapter_version_id"] != second["chapter_version_id"]


def test_persist_approved_chapter_creates_publish_job(fake_session):
    chapter = fake_session.scalar(select(Chapter).where(Chapter.id == "ch_0001"))
    assert chapter is not None
    chapter.status = "approved"
    fake_session.commit()

    result = persist_approved_chapter(
        session=fake_session,
        chapter_id="ch_0001",
        content="content",
        summary="summary",
        review_score=91,
        state_deltas=[],
        create_publish_job=True,
    )

    repo = PublishJobRepository(fake_session)
    jobs = repo.list_jobs()

    assert result["db_saved"] is True
    assert result["publish_job_id"] is not None
    assert len(jobs) == 1
    assert jobs[0].status == "publish_pending"


def test_plan_next_chapter_includes_story_arc_and_character_signals():
    plan = plan_next_chapter(
        chapter_number=3,
        chapter_goal="push the case forward",
        must_include=["find a clue"],
        must_avoid=["repeat exposition"],
        target_words=2600,
        story_arcs=[
            {
                "arc_type": "main",
                "stage_goal": "solve the first case",
            }
        ],
        character_states=[
            {
                "name": "Lin Fan",
                "role": "lead",
                "state": {"mood": "focused"},
            }
        ],
    )

    assert plan["chapter_number"] == 3
    assert plan["objective"] == "push the case forward"
    assert "solve the first case" in plan["context_signals"]["story_arc_goals"]
    assert "Lin Fan:focused" in plan["context_signals"]["character_state_hints"]
    assert "find a clue" in plan["execution_beats"]
    assert "repeat exposition" in plan["continuity_focus"]
