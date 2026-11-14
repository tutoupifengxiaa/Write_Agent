from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import application as application_module
from app import launch
from app.application import WriteAgentApplication
from app.db.base import Base
from app.db.models import Book
from app.db.repositories import BookRepository, ChapterRepository, PublishJobRepository
from app.services.llm_client import OpenAIResponsesLLMClient


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def create_book(session):
    return BookRepository(session).create_book(
        id="transaction-book", title="Book", platform="fanqie", genre="urban"
    )


def test_repository_create_book_can_be_rolled_back(session):
    create_book(session)
    session.rollback()
    assert session.get(Book, "transaction-book") is None


def test_repository_update_outline_can_be_rolled_back(session):
    create_book(session)
    session.commit()
    BookRepository(session).update_outline(
        "transaction-book", book_outline={"new": True}, logline="new", style_rules={}
    )
    session.rollback()
    assert session.get(Book, "transaction-book").logline is None


def test_repository_update_job_can_be_rolled_back(session):
    create_book(session)
    chapter = ChapterRepository(session).create_planned_chapter(
        book_id="transaction-book", chapter_number=1, title="Chapter", goal={}
    )
    repo = PublishJobRepository(session)
    job = repo.create_job(
        chapter_id=chapter.id, platform="fanqie", mode="draft_only", status="publish_failed"
    )
    session.commit()
    repo.update_status(job, status="publish_pending")
    session.rollback()
    assert job.status == "publish_failed"


def test_llm_client_closes_http_transport():
    transport = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    client = OpenAIResponsesLLMClient(api_key="", model="test", http_client=transport)
    try:
        client.close()
        assert transport.is_closed
    finally:
        transport.close()


@pytest.mark.parametrize("shutdown", [False, True])
def test_application_closes_all_resources_once(monkeypatch, shutdown):
    clients = [SimpleNamespace(close=Mock()) for _ in range(3)]
    client_iter = iter(clients)
    session = SimpleNamespace(close=Mock())
    embedding = SimpleNamespace(close=Mock())
    upgrade = Mock()
    monkeypatch.setattr(application_module, "upgrade_database", upgrade)
    monkeypatch.setattr(OpenAIResponsesLLMClient, "from_settings", lambda *args, **kwargs: next(client_iter))
    monkeypatch.setattr(application_module, "OpenAICompatibleEmbeddingClient", lambda **kwargs: embedding)
    settings = SimpleNamespace(embedding_api_key="placeholder", embedding_base_url="https://example.invalid")
    application = WriteAgentApplication.from_settings(settings, session_factory=lambda: session)
    if shutdown:
        with TestClient(application.fastapi_app) as client:
            assert client.get("/health").status_code == 200
    else:
        application.close()
    application.close()
    for resource in [session, *clients, embedding]:
        resource.close.assert_called_once()
    upgrade.assert_called_once()


def test_application_closes_remaining_resources_when_one_close_fails():
    session = SimpleNamespace(close=Mock(side_effect=RuntimeError("close failed")))
    writer = SimpleNamespace(close=Mock())
    reviewer = SimpleNamespace(close=Mock())
    application = WriteAgentApplication(None, session, writer, reviewer, None)
    with pytest.raises(RuntimeError, match="close failed"):
        application.close()
    writer.close.assert_called_once()
    reviewer.close.assert_called_once()


@pytest.mark.parametrize("loop_error, close_error, expected", [(None, None, 0), (RuntimeError("loop failed"), RuntimeError("close failed"), 1), (KeyboardInterrupt(), None, 0)])
def test_worker_closes_application_on_every_exit(monkeypatch, loop_error, close_error, expected):
    monkeypatch.setattr(launch, "validate_worker_settings", lambda settings: None)
    application = SimpleNamespace(scheduler=object(), session=object(), close=Mock(side_effect=close_error))
    monkeypatch.setattr(launch, "build_worker", lambda settings: application)
    def run_loop(**kwargs):
        if loop_error is not None:
            raise loop_error
        return {"cycles": 1}
    monkeypatch.setattr(launch, "run_scheduler_loop", run_loop)
    assert launch.run_worker(max_cycles=1) == expected
    application.close.assert_called_once()
