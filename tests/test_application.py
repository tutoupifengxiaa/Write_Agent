import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.persister import persist_review_candidate
from app.application import WriteAgentApplication
from app.db.base import Base
from app.db.models import Book, Chapter, ChapterVersion, TrendSnapshot
from app.services.embedding import KeywordEmbeddingClient
from app.services.llm_client import OpenAIResponsesLLMClient


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key="test-key",
        openai_model="writer-model",
        openai_review_model="review-model",
        openai_base_url="https://wuzuapi.com",
        openai_reasoning_effort="xhigh",
        openai_fallback_model="fallback-model",
        openai_fallback_reasoning_effort="medium",
        openai_store_response=False,
        openai_timeout_seconds=60.0,
        embedding_model="embedding-model",
        embedding_timeout_seconds=15.0,
        embedding_fallback_mode="keyword",
        embedding_base_url="",
        embedding_api_key=SecretStr(""),
        fanqie_browser_mode="stub",
        fanqie_author_url="",
        fanqie_screenshot_dir="logs",
        fanqie_headless=True,
        fanqie_slow_mo_ms=0,
        publish_mode="",
    )


def test_application_create_fastapi_app_includes_existing_routes() -> None:
    application = WriteAgentApplication.from_settings(_settings())

    assert isinstance(application.fastapi_app, FastAPI)

    client = TestClient(application.fastapi_app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/jobs/health").json()["scheduler"] == "idle"


def test_application_wires_scheduler_with_writer_and_reviewer_clients() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    application = WriteAgentApplication.from_settings(
        _settings(),
        session_factory=session_factory,
    )

    assert application.scheduler.session is application.session
    assert isinstance(application.writer_client, OpenAIResponsesLLMClient)
    assert isinstance(application.reviewer_client, OpenAIResponsesLLMClient)
    assert application.writer_client.model == "writer-model"
    assert application.reviewer_client.model == "review-model"
    assert application.writer_client.fallback_model == "fallback-model"


def test_application_wires_reviser_and_optional_publisher() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    settings = _settings()
    settings.publish_mode = "draft_only"

    application = WriteAgentApplication.from_settings(
        settings,
        session_factory=session_factory,
    )

    assert application.scheduler.reviser is not None
    assert application.scheduler.publisher is not None


def test_application_exposes_session_for_worker_run_persistence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    application = WriteAgentApplication.from_settings(
        _settings(),
        session_factory=session_factory,
    )

    assert application.session is application.scheduler.session


def test_application_wires_embedding_fallback_settings_and_closes_client(
    monkeypatch,
) -> None:
    created = []

    class FakeEmbeddingClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "app.application.OpenAICompatibleEmbeddingClient",
        FakeEmbeddingClient,
    )

    application = WriteAgentApplication.from_settings(_settings())

    assert created[0].kwargs == {
        "api_key": "test-key",
        "base_url": "https://wuzuapi.com",
        "model": "embedding-model",
        "timeout_seconds": 15.0,
    }
    assert application.scheduler.embedding_client is created[0]
    assert isinstance(
        application.scheduler.fallback_embedding_client,
        KeywordEmbeddingClient,
    )
    assert application.scheduler.embedding_model_name == "embedding-model"
    assert application.scheduler.embedding_fallback_mode == "keyword"

    with TestClient(application.fastapi_app) as client:
        assert client.get("/health").status_code == 200

    assert created[0].closed is True


def test_application_without_embedding_key_wires_explicit_keyword_degradation() -> None:
    settings = _settings()
    settings.openai_api_key = ""
    settings.embedding_api_key = SecretStr("")

    application = WriteAgentApplication.from_settings(settings)

    assert application.scheduler.embedding_client is None
    assert application.scheduler.embedding_model_name == "keyword-hash"
    assert isinstance(
        application.scheduler.fallback_embedding_client,
        KeywordEmbeddingClient,
    )


def test_scheduler_persists_retrieval_evidence_from_application_assembly(
    monkeypatch,
) -> None:
    class FakeEmbeddingClient:
        def __init__(self, **kwargs) -> None:
            pass

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [
                [1.0, 0.0] if "warehouse" in text else [0.0, 1.0]
                for text in texts
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "app.application.OpenAICompatibleEmbeddingClient",
        FakeEmbeddingClient,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    settings = _settings()
    settings.embedding_api_key = SecretStr("embedding-key")
    settings.embedding_base_url = "https://embeddings.example/v1"
    application = WriteAgentApplication.from_settings(
        settings,
        session_factory=session_factory,
    )
    application.session.add(
        Book(id="book_memory", title="Memory", platform="fanqie", genre="urban")
    )
    application.session.add_all(
        [
            Chapter(
                id="chapter_history",
                approved_version_id="history_summary",
                book_id="book_memory",
                chapter_number=1,
                title="History",
                status="persisted",
            ),
            Chapter(
                id="chapter_target",
                book_id="book_memory",
                chapter_number=2,
                title="Target",
                goal={"objective": "warehouse clue"},
                status="draft_planned",
            ),
        ]
    )
    application.session.flush()
    application.session.add(
        ChapterVersion(
            id="history_summary",
            chapter_id="chapter_history",
            source="approved",
            summary="warehouse clue",
        )
    )
    application.session.commit()
    captured_context = {}
    application.scheduler.writer = lambda context: (
        captured_context.update(context) or {"content": "new chapter"}
    )
    application.scheduler.reviewer = lambda content, context: {
        "approved": True,
        "review_score": 95,
        "summary": "approved summary",
    }

    result = application.scheduler.run_once("chapter_target")

    target = application.session.get(Chapter, "chapter_target")
    version = application.session.get(ChapterVersion, target.current_version_id)
    assert result["final_status"] == "persisted"
    assert captured_context["retrieval_evidence"][0]["source_id"] == "history_summary"
    assert version.generation_metadata["retrieval_mode"] == "semantic"
    assert version.generation_metadata["retrieval_evidence"][0]["source_id"] == "history_summary"
    assert version.generation_metadata["retrieval_degradation_reason"] is None


def test_main_exposes_application_fastapi_app() -> None:
    from app.main import app, application

    assert app is application.fastapi_app


def test_import_main_does_not_initialize_database_or_clients() -> None:
    # Use a fresh interpreter so importing the module tests its first import.
    script = """
from contextlib import ExitStack
from unittest.mock import patch
from fastapi import FastAPI

with ExitStack() as stack:
    for target in (
        "sqlalchemy.schema.MetaData.create_all",
        "alembic.command.upgrade",
        "sqlalchemy.orm.sessionmaker.__call__",
        "app.services.llm_client.OpenAIResponsesLLMClient.from_settings",
        "app.services.embedding.OpenAICompatibleEmbeddingClient.__init__",
        "app.orchestrator.scheduler.Scheduler.__init__",
    ):
        stack.enter_context(patch(target, side_effect=AssertionError(target)))
    from app.main import app
    assert isinstance(app, FastAPI)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=30
    )
    assert result.returncode == 0, "Importing app.main attempted startup work or failed"


def test_web_lifespan_initializes_once_each_start_and_closes_only_web_resources(monkeypatch):
    from app import application

    initialize = Mock()
    writers = [Mock(), Mock()]
    embeddings = [Mock(), Mock()]
    make_writer = Mock(side_effect=writers)
    make_embedding = Mock(side_effect=embeddings)
    monkeypatch.setattr(application, "initialize_database", initialize)
    monkeypatch.setattr(OpenAIResponsesLLMClient, "from_settings", make_writer)
    monkeypatch.setattr(application, "OpenAICompatibleEmbeddingClient", make_embedding)
    monkeypatch.setattr(application, "Scheduler", Mock(side_effect=AssertionError("unexpected scheduler")))
    monkeypatch.setattr(sessionmaker, "__call__", Mock(side_effect=AssertionError("unexpected session")))
    settings = _settings()
    settings.embedding_api_key = SecretStr("YOUR_API_KEY_HERE")
    web = application.build_web_app(settings)
    initialize.assert_not_called()
    make_writer.assert_not_called()
    make_embedding.assert_not_called()

    for index in range(2):
        with TestClient(web) as client:
            assert client.get("/health").status_code == 200
            assert initialize.call_count == index + 1
            assert make_writer.call_count == index + 1
            assert not make_writer.call_args.kwargs.get("review", False)
            assert make_embedding.call_count == index + 1
            assert web.state.creation_context_options["embedding_client"] is embeddings[index]
            writers[index].close.assert_not_called()
        writers[index].close.assert_called_once()
        embeddings[index].close.assert_called_once()


def test_worker_builds_scheduler_without_fastapi(monkeypatch):
    from app import application

    initialize = Mock()
    session = Mock()
    clients = [Mock(), Mock(), Mock()]
    embedding = Mock()
    monkeypatch.setattr(application, "initialize_database", initialize)
    monkeypatch.setattr(OpenAIResponsesLLMClient, "from_settings", Mock(side_effect=clients))
    monkeypatch.setattr(application, "OpenAICompatibleEmbeddingClient", Mock(return_value=embedding))
    monkeypatch.setattr(application, "FastAPI", Mock(side_effect=AssertionError("unexpected web app")))
    settings = _settings()
    settings.embedding_api_key = SecretStr("YOUR_API_KEY_HERE")
    worker = application.build_worker(settings, session_factory=lambda: session)
    try:
        initialize.assert_called_once()
        assert worker.scheduler.session is worker.session is session
    finally:
        worker.close()
    for resource in [session, *clients, embedding]:
        resource.close.assert_called_once()


def test_create_and_get_book() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    create_response = client.post(
        "/books",
        json={
            "id": "book_alpha",
            "title": "Alpha",
            "platform": "fanqie",
            "genre": "urban",
            "logline": "A test book",
            "book_outline": {"acts": ["setup"]},
            "style_rules": {"tone": "fast"},
        },
    )

    assert create_response.status_code == 201
    assert create_response.json()["id"] == "book_alpha"

    list_response = client.get("/books")

    assert list_response.status_code == 200
    assert list_response.json()[0]["title"] == "Alpha"


def test_web_worker_and_compatibility_app_use_their_configured_database(tmp_path, monkeypatch):
    from app.application import build_web_app, build_worker

    monkeypatch.setattr(OpenAIResponsesLLMClient, "from_settings", lambda *args, **kwargs: Mock())
    left, right = _settings(), _settings()
    for settings, name in ((left, "left"), (right, "right")):
        settings.database_url = f"sqlite+pysqlite:///{tmp_path / (name + '.db')}"
        settings.embedding_api_key = SecretStr("")
        settings.openai_api_key = ""

    book = {"id": "same_id", "platform": "local", "genre": "fiction"}
    combined = WriteAgentApplication.from_settings(right)
    with TestClient(build_web_app(left)) as web, TestClient(combined.fastapi_app) as compat:
        assert web.post("/books", json={**book, "title": "Left"}).status_code == 201
        assert compat.get("/books/same_id").status_code == 404
        assert compat.post("/books", json={**book, "title": "Right"}).status_code == 201
        assert combined.session.get(Book, "same_id").title == "Right"
        assert web.get("/books/same_id").json()["title"] == "Left"

    worker = build_worker(left)
    try:
        assert worker.session.get(Book, "same_id").title == "Left"
    finally:
        worker.close()


def test_direct_fastapi_factory_provides_a_session_for_routes():
    from app.application import create_fastapi_app

    with TestClient(create_fastapi_app()) as client:
        assert client.get("/books").status_code == 200


def test_web_and_worker_warn_on_start_only_for_memory_sqlite(tmp_path, caplog, monkeypatch):
    import logging

    from app.application import build_web_app, build_worker

    monkeypatch.setattr(OpenAIResponsesLLMClient, "from_settings", lambda *args, **kwargs: Mock())
    settings = _settings()
    settings.openai_api_key = ""
    settings.embedding_api_key = SecretStr("")
    caplog.set_level(logging.WARNING)

    def memory_warnings():
        return [
            record for record in caplog.records
            if record.levelno == logging.WARNING and "sqlite" in record.getMessage().lower()
            and "memory" in record.getMessage().lower()
        ]

    for in_memory in (True, False):
        settings.database_url = (
            "sqlite+pysqlite:///:memory:" if in_memory else f"sqlite+pysqlite:///{tmp_path / 'worker.db'}"
        )
        caplog.clear()
        app = build_web_app(settings)
        assert not memory_warnings()
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert bool(memory_warnings()) is in_memory

        caplog.clear()
        worker = build_worker(settings)
        try:
            assert bool(memory_warnings()) is in_memory
        finally:
            worker.close()


def test_get_book_detail_returns_outline_fields() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_outline",
            "title": "Outline Book",
            "platform": "fanqie",
            "genre": "urban",
            "logline": "A city rise story",
            "book_outline": {"acts": ["setup"]},
            "style_rules": {"tone": "fast"},
        },
    )

    response = client.get("/books/book_outline")

    assert response.status_code == 200
    assert response.json()["id"] == "book_outline"
    assert response.json()["book_outline"] == {"acts": ["setup"]}
    assert response.json()["style_rules"] == {"tone": "fast"}


def test_create_and_fetch_chapter() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_beta",
            "title": "Beta",
            "platform": "fanqie",
            "genre": "urban",
        },
    )

    create_response = client.post(
        "/chapters",
        json={
            "book_id": "book_beta",
            "chapter_number": 1,
            "title": "Chapter One",
            "goal": {"objective": "Open strong"},
        },
    )

    assert create_response.status_code == 201
    chapter_id = create_response.json()["id"]

    get_response = client.get(f"/chapters/{chapter_id}")

    assert get_response.status_code == 200
    assert get_response.json()["status"] == "draft_planned"
    assert get_response.json()["goal"] == {"objective": "Open strong"}


def test_list_chapter_versions_returns_latest_first_for_creation_workspace() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_versions",
            "title": "Versions",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_versions",
            "chapter_number": 1,
            "title": "Versions Chapter",
            "goal": {"objective": "Track saved drafts"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "First draft content",
            "summary": "First draft summary",
            "review_score": 7.5,
            "state_deltas": [{"type": "beat", "change": "first"}],
            "create_publish_job": False,
        },
    )
    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Second draft content",
            "summary": "Second draft summary",
            "review_score": 8.4,
            "state_deltas": [{"type": "beat", "change": "second"}],
            "create_publish_job": False,
        },
    )

    response = client.get(f"/chapters/{chapter_id}/versions")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["content"] == "Second draft content"
    assert payload[0]["summary"] == "Second draft summary"
    assert payload[0]["review_score"] == 0.0
    assert payload[0]["generation_metadata"] == {
        "pending_state_deltas": [{"type": "beat", "change": "second"}]
    }
    assert payload[1]["content"] == "First draft content"
    assert payload[1]["summary"] == "First draft summary"
    assert payload[1]["generation_metadata"] == {
        "pending_state_deltas": [{"type": "beat", "change": "first"}]
    }


def test_list_book_chapters_returns_chapters_for_one_book() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_library",
            "title": "Library Book",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_library",
            "chapter_number": 1,
            "title": "Library Chapter",
            "goal": {"objective": "Open strong"},
        },
    )

    response = client.get("/books/book_library/chapters")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == chapter_response.json()["id"]
    assert response.json()[0]["title"] == "Library Chapter"


def test_update_book_outline_persists_generated_outline() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_update_outline",
            "title": "Update Outline",
            "platform": "fanqie",
            "genre": "urban",
        },
    )

    response = client.put(
        "/books/book_update_outline/outline",
        json={
            "book_outline": {"acts": ["setup", "conflict", "payoff"]},
            "logline": "An underdog breaks into the city hierarchy.",
            "style_rules": {"tone": "fast", "pov": "third"},
        },
    )

    assert response.status_code == 200
    assert response.json()["book_outline"]["acts"][1] == "conflict"
    assert response.json()["logline"] == "An underdog breaks into the city hierarchy."


def test_update_book_outline_canonicalizes_direction_source_from_saved_capture() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)
    client.post(
        "/books",
        json={
            "id": "book_trace_canonical",
            "title": "Canonical Trace",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    snapshots = [
        TrendSnapshot(
            capture_id="capture_canonical",
            provider="fanqie",
            source_url="https://example.test/fanqie",
            status="real",
            items=[{"id": "fanqie_item_1", "title": "真实条目一"}],
        ),
        TrendSnapshot(
            capture_id="capture_canonical",
            provider="qidian",
            source_url="https://example.test/qidian",
            status="degraded",
            items=[{"id": "qidian_item_1", "title": "真实条目二"}],
        ),
    ]
    application.session.add_all(snapshots)
    application.session.commit()

    response = client.put(
        "/books/book_trace_canonical/outline",
        json={
            "book_outline": {
                "acts": ["setup"],
                "direction_source": {
                    "capture_id": "capture_canonical",
                    "selected_item_ids": ["qidian_item_1"],
                    "snapshot_ids": ["forged_snapshot"],
                    "providers": ["forged_provider"],
                    "captured_at": "2099-01-01T00:00:00Z",
                },
            }
        },
    )

    assert response.status_code == 200
    trace = response.json()["book_outline"]["direction_source"]
    assert trace["capture_id"] == "capture_canonical"
    assert trace["snapshot_ids"] == [snapshot.id for snapshot in snapshots]
    assert trace["providers"] == ["fanqie", "qidian"]
    assert trace["selected_item_ids"] == ["qidian_item_1"]
    assert trace["captured_at"] == max(
        snapshot.captured_at for snapshot in snapshots
    ).isoformat()


def test_update_book_outline_rejects_unknown_capture_and_selected_item() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)
    client.post(
        "/books",
        json={
            "id": "book_trace_invalid",
            "title": "Invalid Trace",
            "platform": "fanqie",
            "genre": "urban",
        },
    )

    unknown_capture = client.put(
        "/books/book_trace_invalid/outline",
        json={
            "book_outline": {
                "direction_source": {
                    "capture_id": "capture_missing",
                    "selected_item_ids": [],
                }
            }
        },
    )
    assert unknown_capture.status_code == 400

    application.session.add(
        TrendSnapshot(
            capture_id="capture_known",
            provider="fanqie",
            source_url="https://example.test/fanqie",
            status="real",
            items=[{"id": "known_item", "title": "Known"}],
        )
    )
    application.session.commit()
    unknown_item = client.put(
        "/books/book_trace_invalid/outline",
        json={
            "book_outline": {
                "direction_source": {
                    "capture_id": "capture_known",
                    "selected_item_ids": ["unknown_item"],
                }
            }
        },
    )
    assert unknown_item.status_code == 400


def test_create_chapters_from_outline_builds_planned_chapters_from_seeds() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_seed_plan",
            "title": "Seed Plan Book",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    application.session.add(
        TrendSnapshot(
            id="snapshot_seed_plan",
            capture_id="capture_seed_plan",
            provider="fanqie",
            source_url="https://example.test/fanqie",
            status="real",
            items=[],
        )
    )
    application.session.commit()
    outline_response = client.put(
        "/books/book_seed_plan/outline",
        json={
            "book_outline": {
                "acts": ["setup", "pressure", "breakthrough"],
                "direction_source": {
                    "capture_id": "capture_seed_plan",
                    "selected_item_ids": [],
                },
                "chapter_seed_plan": [
                    {"chapter": 1, "title": "Cold Start", "goal": "Open with a sharp failure"},
                    {
                        "chapter": 2,
                        "goal": "Find the first leverage point",
                        "must_include": ["mentor clue", "public pressure"],
                        "must_avoid": ["instant victory"],
                        "target_words": 3200,
                    },
                ],
            },
            "logline": "A scrappy lead tries to break into the city hierarchy.",
            "style_rules": {"tone": "fast"},
        },
    )
    assert outline_response.status_code == 200
    saved_direction_source = outline_response.json()["book_outline"]["direction_source"]

    response = client.post("/books/book_seed_plan/chapters/from-outline")

    assert response.status_code == 201
    payload = response.json()
    assert payload["book_id"] == "book_seed_plan"
    assert payload["created_count"] == 2
    assert len(payload["items"]) == 2
    assert payload["items"][0]["chapter_number"] == 1
    assert payload["items"][0]["title"] == "Cold Start"
    assert payload["items"][0]["status"] == "draft_planned"
    assert payload["items"][0]["goal"]["objective"] == "Open with a sharp failure"
    assert payload["items"][0]["goal"]["planning_trace"] == {
        "direction_source": saved_direction_source,
        "outline_seed": {
            "chapter": 1,
            "title": "Cold Start",
            "goal": "Open with a sharp failure",
        },
    }
    assert payload["items"][1]["chapter_number"] == 2
    assert payload["items"][1]["title"] == "第2章"
    assert payload["items"][1]["goal"]["must_include"] == ["mentor clue", "public pressure"]
    assert payload["items"][1]["goal"]["must_avoid"] == ["instant victory"]
    assert payload["items"][1]["goal"]["target_words"] == 3200

    chapters_response = client.get("/books/book_seed_plan/chapters")
    assert chapters_response.status_code == 200
    assert [item["chapter_number"] for item in chapters_response.json()] == [1, 2]


def test_create_chapters_from_outline_returns_error_when_outline_missing() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_no_outline",
            "title": "No Outline",
            "platform": "fanqie",
            "genre": "urban",
        },
    )

    response = client.post("/books/book_no_outline/chapters/from-outline")

    assert response.status_code == 400
    assert response.json()["detail"] == "book outline not found"


def test_create_chapters_from_outline_returns_error_when_seed_plan_missing() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_no_seeds",
            "title": "No Seeds",
            "platform": "fanqie",
            "genre": "urban",
            "book_outline": {"acts": ["setup", "pressure"]},
        },
    )

    response = client.post("/books/book_no_seeds/chapters/from-outline")

    assert response.status_code == 400
    assert response.json()["detail"] == "chapter seed plan not found"


def test_generate_outline_uses_configured_outline_generator() -> None:
    captured_payload = None

    def fake_outline_generator(payload: dict) -> dict:
        nonlocal captured_payload
        captured_payload = payload
        return {
            "title": "霓虹翻盘",
            "logline": "小人物借行业风口翻身。",
            "book_outline": {
                "acts": ["入局", "受压", "破局"],
                "character_arcs": ["主角成长", "对手反扑"],
                "chapter_seed_plan": [
                    {"chapter": 1, "goal": "主角先败"},
                    {"chapter": 2, "goal": "找到缺口"},
                ],
            },
            "style_rules": {"tone": "fast", "hook": "strong"},
        }

    app = WriteAgentApplication.from_settings(_settings()).fastapi_app
    app.state.outline_generator = fake_outline_generator
    client = TestClient(app)

    response = client.post(
        "/outline/generate",
        json={
            "title": "霓虹翻盘",
            "platform": "fanqie",
            "genre": "urban",
            "direction_name": "都市逆袭成长",
            "core_hook": "普通人逆势翻盘",
            "audience": "男频快节奏读者",
            "notes": "强钩子，低门槛",
        },
    )

    assert response.status_code == 200
    assert response.json()["book_outline"]["acts"][0] == "入局"
    assert response.json()["style_rules"]["hook"] == "strong"
    assert captured_payload["direction_name"] == "都市逆袭成长"


def test_list_jobs_returns_persisted_publish_jobs() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_jobs",
            "title": "Jobs",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_jobs",
            "chapter_number": 1,
            "title": "Chapter Jobs",
            "goal": {"objective": "Queue publish"},
        },
    )
    chapter_id = chapter_response.json()["id"]

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(session)
    repo.create_job(
        chapter_id=chapter_id,
        platform="fanqie",
        mode="draft_only",
        status="publish_pending",
    )
    session.commit()

    response = client.get("/jobs")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["chapter_id"] == chapter_id


def test_retry_publish_updates_failed_job() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_retry",
            "title": "Retry",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_retry",
            "chapter_number": 1,
            "title": "Retry Chapter",
            "goal": {"objective": "Retry publish"},
        },
    )
    chapter_id = chapter_response.json()["id"]

    from app.agents.persister import persist_approved_chapter
    from app.db.models import PublishJob

    result = persist_approved_chapter(
        session=session, chapter_id=chapter_id, content="Approved draft",
        summary="Approved summary", review_score=90, state_deltas=[],
        create_publish_job=True,
    )
    job = session.get(PublishJob, result["publish_job_id"])
    job.status = "publish_failed"
    job.error_message = "platform timeout"
    session.commit()

    response = client.post(f"/chapters/{chapter_id}/retry-publish")

    assert response.status_code == 202
    assert response.json()["job_id"] == job.id
    assert response.json()["status"] == "publish_pending"


def test_list_jobs_supports_status_filter_for_failed_publish_queue() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_failed_jobs",
            "title": "Failed Jobs",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_failed_jobs",
            "chapter_number": 1,
            "title": "Failed Job Chapter",
            "goal": {"objective": "Observe failures"},
        },
    )
    chapter_id = chapter_response.json()["id"]

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(session)
    repo.create_job(
        chapter_id=chapter_id,
        platform="fanqie",
        mode="draft_only",
        status="publish_failed",
        error_message="fatal publish crash",
    )
    repo.create_job(
        chapter_id=chapter_id,
        platform="fanqie",
        mode="draft_only",
        status="draft_uploaded",
    )
    session.commit()

    response = client.get("/jobs", params={"status": "publish_failed"})

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["status"] == "publish_failed"
    assert response.json()[0]["error_message"] == "fatal publish crash"


def test_list_jobs_supports_chapter_filter_for_library_actions() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_library_filter",
            "title": "Library Filter",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_one = client.post(
        "/chapters",
        json={
            "book_id": "book_library_filter",
            "chapter_number": 1,
            "title": "Chapter One",
            "goal": {"objective": "Observe first job"},
        },
    ).json()["id"]
    chapter_two = client.post(
        "/chapters",
        json={
            "book_id": "book_library_filter",
            "chapter_number": 2,
            "title": "Chapter Two",
            "goal": {"objective": "Observe second job"},
        },
    ).json()["id"]

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(session)
    repo.create_job(
        chapter_id=chapter_one,
        platform="fanqie",
        mode="draft_only",
        status="publish_failed",
        error_message="first failure",
    )
    repo.create_job(
        chapter_id=chapter_two,
        platform="fanqie",
        mode="draft_only",
        status="publish_pending",
    )
    session.commit()

    response = client.get("/jobs", params={"chapter_id": chapter_one})

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["chapter_id"] == chapter_one
    assert response.json()[0]["error_message"] == "first failure"


def test_archive_publish_failure_moves_job_to_archived() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_archive_job",
            "title": "Archive Job",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_archive_job",
            "chapter_number": 1,
            "title": "Archive Chapter",
            "goal": {"objective": "Archive failure"},
        },
    )
    chapter_id = chapter_response.json()["id"]

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(session)
    job = repo.create_job(
        chapter_id=chapter_id,
        platform="fanqie",
        mode="draft_only",
        status="publish_failed",
        error_message="manual intervention required",
    )
    session.commit()

    response = client.post(f"/chapters/{chapter_id}/archive-publish-failure")

    assert response.status_code == 202
    assert response.json()["job_id"] == job.id
    assert response.json()["status"] == "archived"


def test_jobs_health_reports_pending_counts() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_health",
            "title": "Health",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_response = client.post(
        "/chapters",
        json={
            "book_id": "book_health",
            "chapter_number": 1,
            "title": "Health Chapter",
            "goal": {"objective": "Observe queue"},
        },
    )
    chapter_id = chapter_response.json()["id"]

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(session)
    repo.create_job(
        chapter_id=chapter_id,
        platform="fanqie",
        mode="draft_only",
        status="publish_pending",
    )
    session.commit()

    response = client.get("/jobs/health")

    assert response.status_code == 200
    assert response.json()["scheduler"] == "busy"
    assert response.json()["pending_chapters"] >= 1
    assert response.json()["pending_publish_jobs"] >= 1


def test_list_scheduler_runs_returns_structured_cycle_records() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    repo.create_run(
        cycle_number=1,
        processed_count=1,
        status="processed",
        outcomes=[
            {
                "final_status": "persisted",
                "item_type": "chapter",
                "item_id": "chapter_1",
            }
        ],
    )
    session.commit()

    response = client.get("/jobs/runs")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["cycle_number"] == 1
    assert response.json()[0]["status"] == "processed"


def test_get_scheduler_run_returns_single_cycle_record() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    run = repo.create_run(
        cycle_number=2,
        processed_count=0,
        status="idle",
        outcomes=[],
        reason_code="no_pending_work",
        duration_ms=23,
        outcome_counts={"idle": 1},
    )
    session.commit()

    response = client.get(f"/jobs/runs/{run.id}")

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert response.json()["cycle_number"] == 2
    assert response.json()["status"] == "idle"
    assert {
        key: response.json()[key]
        for key in ("reason_code", "duration_ms", "outcome_counts")
    } == {
        "reason_code": "no_pending_work",
        "duration_ms": 23,
        "outcome_counts": {"idle": 1},
    }


def test_list_scheduler_runs_supports_status_filter_and_paging() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    repo.create_run(cycle_number=1, processed_count=1, status="processed", outcomes=[])
    repo.create_run(cycle_number=2, processed_count=0, status="idle", outcomes=[])
    repo.create_run(cycle_number=3, processed_count=2, status="processed", outcomes=[])
    session.commit()

    filtered_response = client.get("/jobs/runs", params={"status": "processed"})
    paged_response = client.get("/jobs/runs", params={"limit": 1, "offset": 1})

    assert filtered_response.status_code == 200
    filtered_cycles = [row["cycle_number"] for row in filtered_response.json()]
    assert 3 in filtered_cycles
    assert 1 in filtered_cycles
    assert all(row["status"] == "processed" for row in filtered_response.json())
    assert paged_response.status_code == 200
    assert len(paged_response.json()) == 1
    assert "cycle_number" in paged_response.json()[0]


def test_scheduler_run_summary_returns_operator_facing_counts() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    repo.create_run(cycle_number=1, processed_count=1, status="processed", outcomes=[])
    repo.create_run(cycle_number=2, processed_count=0, status="idle", outcomes=[])
    repo.create_run(cycle_number=3, processed_count=2, status="processed", outcomes=[])
    session.commit()

    response = client.get("/jobs/runs/summary")

    assert response.status_code == 200
    assert response.json()["total_runs"] >= 3
    assert response.json()["processed_runs"] >= 2
    assert response.json()["idle_runs"] >= 1
    assert response.json()["total_processed_items"] >= 3


def test_scheduler_run_failure_clusters_group_repeated_failure_signals() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    repo.create_run(
        cycle_number=1,
        processed_count=1,
        status="processed",
        outcomes=[
            {
                "item_type": "publish_job",
                "item_id": "publish_job_1",
                "final_status": "publish_failed",
                "error_type": "platform_error",
            }
        ],
    )
    repo.create_run(
        cycle_number=2,
        processed_count=1,
        status="processed",
        outcomes=[
            {
                "item_type": "publish_job",
                "item_id": "publish_job_2",
                "final_status": "publish_failed",
                "error_type": "platform_error",
            }
        ],
    )
    repo.create_run(
        cycle_number=3,
        processed_count=1,
        status="processed",
        outcomes=[
            {
                "item_type": "chapter",
                "item_id": "chapter_3",
                "final_status": "revision_pending",
            }
        ],
    )
    session.commit()

    response = client.get("/jobs/runs/failure-clusters")

    assert response.status_code == 200
    clusters = response.json()
    assert clusters[0]["failure_key"] == "publish_job:publish_failed:platform_error"
    assert clusters[0]["count"] == 2
    assert clusters[0]["latest_cycle_number"] == 2
    assert clusters[0]["sample_item_id"] == "publish_job_2"


def test_get_scheduler_run_returns_detailed_outcomes_for_ops_drill_down() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    from app.db.repositories import SchedulerRunRepository

    repo = SchedulerRunRepository(session)
    run = repo.create_run(
        cycle_number=4,
        processed_count=2,
        status="processed",
        outcomes=[
            {
                "item_type": "chapter",
                "item_id": "chapter_4",
                "final_status": "persisted",
            },
            {
                "item_type": "publish_job",
                "item_id": "publish_job_4",
                "final_status": "publish_failed",
                "error_type": "business_error",
            },
        ],
    )
    session.commit()

    response = client.get(f"/jobs/runs/{run.id}")

    assert response.status_code == 200
    assert response.json()["processed_count"] == 2
    assert response.json()["outcomes"][1]["error_type"] == "business_error"


def test_plan_chapter_endpoint_creates_planned_chapter_from_memory_signals() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    session = application.session
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_plan",
            "title": "Plan",
            "platform": "fanqie",
            "genre": "urban",
        },
    )

    from app.db.models import Character, StoryArc

    session.add(
        StoryArc(
            book_id="book_plan",
            arc_type="main",
            chapter_start=1,
            chapter_end=10,
            stage_goal="solve the first case",
            outline={"beats": ["clue", "suspect"]},
        )
    )
    session.add(
        Character(
            book_id="book_plan",
            name="Lin Fan",
            role="lead",
            age=18,
            profile={"trait": "calm"},
            last_updated_chapter_number=1,
        )
    )
    session.commit()

    response = client.post(
        "/chapters/plan",
        json={
            "book_id": "book_plan",
            "chapter_number": 2,
            "title": "Chapter Two",
            "chapter_goal": "push the case forward",
            "must_include": ["find a clue"],
            "must_avoid": ["repeat exposition"],
            "target_words": 2600,
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "draft_planned"
    assert response.json()["goal"]["context_signals"]["story_arc_goals"] == ["solve the first case"]


def test_creation_page_accepts_book_and_chapter_query_params() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    response = client.get("/creation", params={"book_id": "book_demo", "chapter_id": "chapter_demo"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<div id="creation-root"></div>' in response.text


def test_creation_save_persists_manual_chapter_version() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_creation_save",
            "title": "Creation Save",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_creation_save",
            "chapter_number": 1,
            "title": "Creation Chapter",
            "goal": {"objective": "Finish the confrontation"},
        },
    ).json()["id"]

    response = client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Chapter content",
            "summary": "Chapter summary",
            "review_score": 8.8,
            "state_deltas": [{"type": "wealth", "change": "+1000"}],
            "create_publish_job": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "draft_saved"
    assert response.json()["publish_job_id"] is None

    chapter_response = client.get(f"/chapters/{chapter_id}")
    assert chapter_response.status_code == 200
    assert chapter_response.json()["status"] == "draft_saved"
    assert chapter_response.json()["current_version_id"] is not None


def test_creation_save_rejects_publish_without_review() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_creation_publish",
            "title": "Creation Publish",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_creation_publish",
            "chapter_number": 1,
            "title": "Publish Chapter",
            "goal": {"objective": "Queue manual draft for publish"},
        },
    ).json()["id"]

    response = client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Publishable chapter content",
            "summary": "Ready to publish",
            "review_score": 9.2,
            "state_deltas": [],
            "create_publish_job": True,
        },
    )

    assert response.status_code == 409

    jobs_response = client.get("/jobs", params={"chapter_id": chapter_id})
    assert jobs_response.status_code == 200
    assert jobs_response.json() == []


def test_creation_review_marks_chapter_review_pending_with_manual_note() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_creation_review",
            "title": "Creation Review",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_creation_review",
            "chapter_number": 1,
            "title": "Review Chapter",
            "goal": {"objective": "Check continuity"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Saved chapter content",
            "summary": "Saved summary",
            "review_score": 9.0,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )

    response = client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "review_pending",
            "note": "请先人工核对连续性和角色状态。",
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "review_pending"
    assert response.json()["note"] == "请先人工核对连续性和角色状态。"

    chapter_response = client.get(f"/chapters/{chapter_id}")
    assert chapter_response.status_code == 200
    assert chapter_response.json()["status"] == "review_pending"
    assert chapter_response.json()["goal"]["manual_review_note"] == "请先人工核对连续性和角色状态。"
    assert chapter_response.json()["goal"]["manual_review_action"] == "review_pending"


def test_creation_review_marks_chapter_revision_pending_with_manual_note() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_creation_revision",
            "title": "Creation Revision",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_creation_revision",
            "chapter_number": 1,
            "title": "Revision Chapter",
            "goal": {"objective": "Revise by note"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Saved chapter content",
            "summary": "Saved summary",
            "review_score": 8.5,
            "state_deltas": [{"type": "mood", "change": "tense"}],
            "create_publish_job": False,
        },
    )

    response = client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "revision_pending",
            "note": "结尾钩子偏弱，补强冲突升级。",
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "revision_pending"
    assert response.json()["note"] == "结尾钩子偏弱，补强冲突升级。"

    chapter_response = client.get(f"/chapters/{chapter_id}")
    assert chapter_response.status_code == 200
    assert chapter_response.json()["status"] == "revision_pending"
    assert chapter_response.json()["goal"]["manual_review_note"] == "结尾钩子偏弱，补强冲突升级。"
    assert chapter_response.json()["goal"]["manual_review_action"] == "revision_pending"


def test_review_center_lists_manual_review_queue() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_center",
            "title": "Review Center",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    review_pending_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_center",
            "chapter_number": 1,
            "title": "Review Pending Chapter",
            "goal": {"objective": "First manual check"},
        },
    ).json()["id"]
    revision_pending_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_center",
            "chapter_number": 2,
            "title": "Revision Pending Chapter",
            "goal": {"objective": "Second manual check"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/review",
        json={
            "chapter_id": review_pending_id,
            "review_action": "review_pending",
            "note": "先核对连续性。",
        },
    )
    client.post(
        "/api/creation/review",
        json={
            "chapter_id": revision_pending_id,
            "review_action": "revision_pending",
            "note": "补强结尾钩子。",
        },
    )

    response = client.get("/chapters/manual-review-queue")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) >= 2
    matching_rows = [item for item in payload if item["book_id"] == "book_review_center"]
    assert len(matching_rows) == 2
    assert matching_rows[0]["status"] in {"review_pending", "revision_pending"}
    assert matching_rows[0]["book_title"] == "Review Center"
    assert "manual_review_note" in matching_rows[0]
    returned_ids = {item["id"] for item in payload}
    assert review_pending_id in returned_ids
    assert revision_pending_id in returned_ids


def test_review_center_queue_does_not_return_cross_chapter_current_version() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_cross_pointer",
            "title": "Cross Pointer Review",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    target_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_cross_pointer",
            "chapter_number": 1,
            "title": "Target Chapter",
            "goal": {"objective": "Review the target only"},
        },
    ).json()["id"]
    foreign_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_cross_pointer",
            "chapter_number": 2,
            "title": "Foreign Chapter",
            "goal": {"objective": "Keep this content isolated"},
        },
    ).json()["id"]
    foreign_candidate = persist_review_candidate(
        session=application.session,
        chapter_id=foreign_id,
        content="Foreign candidate content must stay hidden",
        summary="Foreign summary",
        review_score=70,
    )
    target = application.session.get(Chapter, target_id)
    assert target is not None
    target.status = "revision_pending"
    target.current_version_id = foreign_candidate["chapter_version_id"]
    application.session.commit()

    response = client.get("/chapters/manual-review-queue")

    assert response.status_code == 200
    target_item = next(item for item in response.json() if item["id"] == target_id)
    assert target_item["candidate_version"] is None


def test_review_center_approve_persists_manual_reviewed_chapter() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_approve",
            "title": "Review Approve",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_approve",
            "chapter_number": 1,
            "title": "Approved Chapter",
            "goal": {"objective": "Approve from review center"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Manual draft content",
            "summary": "Manual draft summary",
            "review_score": 8.6,
            "state_deltas": [{"type": "wealth", "change": "+300"}],
            "create_publish_job": False,
        },
    )
    client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "review_pending",
            "note": "核对通过后即可入库。",
        },
    )

    response = client.post(
        "/api/review-center/approve",
        json={
            "chapter_id": chapter_id,
            "version_id": client.get(f"/chapters/{chapter_id}").json().get("current_version_id"),
            "note": "人工审核通过，允许入库。",
            "create_publish_job": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "persisted"
    assert response.json()["publish_job_id"] is None

    chapter_response = client.get(f"/chapters/{chapter_id}")
    assert chapter_response.status_code == 200
    assert chapter_response.json()["status"] == "persisted"
    assert chapter_response.json()["current_version_id"] is not None
    assert chapter_response.json()["goal"]["manual_review_note"] == "人工审核通过，允许入库。"
    assert chapter_response.json()["goal"]["manual_review_action"] == "approved"


def test_review_center_revise_updates_note_and_keeps_revision_pending() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_revise",
            "title": "Review Revise",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_revise",
            "chapter_number": 1,
            "title": "Revise Chapter",
            "goal": {"objective": "Revise from review center"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Manual draft content",
            "summary": "Manual draft summary",
            "review_score": 7.8,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )
    client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "review_pending",
            "note": "送入人工审核。",
        },
    )

    response = client.post(
        "/api/review-center/revise",
        json={
            "chapter_id": chapter_id,
            "note": "结尾钩子仍然不够强，补一次反转。",
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "revision_pending"
    assert response.json()["note"] == "结尾钩子仍然不够强，补一次反转。"

    chapter_response = client.get(f"/chapters/{chapter_id}")
    assert chapter_response.status_code == 200
    assert chapter_response.json()["status"] == "revision_pending"
    assert chapter_response.json()["goal"]["manual_review_note"] == "结尾钩子仍然不够强，补一次反转。"
    assert chapter_response.json()["goal"]["manual_review_action"] == "revision_pending"


def test_review_center_cannot_return_persisted_chapter_to_revision_pending() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)
    client.post(
        "/books",
        json={
            "id": "book_revise_once",
            "title": "Revise Once",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_revise_once",
            "chapter_number": 1,
            "title": "Persisted Chapter",
            "goal": {"objective": "Do not reopen after persistence"},
        },
    ).json()["id"]
    persist_review_candidate(
        session=application.session,
        chapter_id=chapter_id,
        content="Candidate content",
        summary="Candidate summary",
        review_score=88,
    )
    application.session.commit()
    approve_response = client.post(
        "/api/review-center/approve",
        json={
            "chapter_id": chapter_id,
            "version_id": client.get(f"/chapters/{chapter_id}").json().get("current_version_id"),
            "note": "批准并排队。",
            "create_publish_job": True,
        },
    )
    assert approve_response.status_code == 200
    version_count = len(client.get(f"/chapters/{chapter_id}/versions").json())
    job_count = len(client.get("/jobs", params={"chapter_id": chapter_id}).json())

    response = client.post(
        "/api/review-center/revise",
        json={"chapter_id": chapter_id, "note": "不应重复退回。"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "chapter is not pending manual review"
    assert len(client.get(f"/chapters/{chapter_id}/versions").json()) == version_count
    assert len(client.get("/jobs", params={"chapter_id": chapter_id}).json()) == job_count


def test_review_center_approve_can_create_publish_job() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_publish",
            "title": "Review Publish",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_publish",
            "chapter_number": 1,
            "title": "Publish From Review",
            "goal": {"objective": "Approve and queue publish"},
        },
    ).json()["id"]

    candidate = persist_review_candidate(
        session=application.session,
        chapter_id=chapter_id,
        content="Queued candidate content",
        summary="Queued candidate summary",
        review_score=8.9,
        generation_metadata={
            "planning_trace": {"direction_source": "manual"},
            "retrieval_evidence": [{"source_id": "summary_001"}],
            "retrieval_mode": "semantic",
            "review_trace": {
                "initial_review": {"approved": False},
                "revision": {"resolved_issue_ids": ["issue_001"]},
                "final_review": {"approved": True},
            },
        },
    )
    application.session.commit()

    queue_response = client.get("/chapters/manual-review-queue")
    assert queue_response.status_code == 200
    queue_item = next(item for item in queue_response.json() if item["id"] == chapter_id)
    assert queue_item["candidate_version"] == {
        "id": candidate["chapter_version_id"],
        "source": "review_candidate",
        "content": "Queued candidate content",
        "generation_metadata": {
            "planning_trace": {"direction_source": "manual"},
            "retrieval_evidence": [{"source_id": "summary_001"}],
            "retrieval_mode": "semantic",
            "review_trace": {
                "initial_review": {"approved": False},
                "revision": {"resolved_issue_ids": ["issue_001"]},
                "final_review": {"approved": True},
            },
        },
    }

    foreign_chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_publish",
            "chapter_number": 2,
            "title": "Foreign Candidate",
            "goal": {"objective": "Must not be approved as another chapter"},
        },
    ).json()["id"]
    foreign_candidate = persist_review_candidate(
        session=application.session,
        chapter_id=foreign_chapter_id,
        content="Foreign candidate content",
        summary="Foreign summary",
        review_score=7.0,
    )
    chapter = application.session.get(Chapter, chapter_id)
    assert chapter is not None
    chapter.current_version_id = foreign_candidate["chapter_version_id"]
    application.session.commit()

    mismatched_response = client.post(
        "/api/review-center/approve",
        json={
            "chapter_id": chapter_id,
            "version_id": client.get(f"/chapters/{chapter_id}").json().get("current_version_id"),
            "note": "不应批准错章正文。",
            "create_publish_job": True,
        },
    )
    assert mismatched_response.status_code == 400

    chapter.current_version_id = candidate["chapter_version_id"]
    application.session.commit()

    response = client.post(
        "/api/review-center/approve",
        json={
            "chapter_id": chapter_id,
            "version_id": client.get(f"/chapters/{chapter_id}").json().get("current_version_id"),
            "note": "人工审核通过并加入发布队列。",
            "create_publish_job": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["chapter_id"] == chapter_id
    assert response.json()["status"] == "persisted"
    assert response.json()["publish_job_id"] is not None

    versions_response = client.get(f"/chapters/{chapter_id}/versions")
    assert versions_response.status_code == 200
    approved_version = versions_response.json()[0]
    assert approved_version["content"] == "Queued candidate content"
    assert approved_version["generation_metadata"]["review_trace"]["revision"] == {
        "resolved_issue_ids": ["issue_001"]
    }
    assert approved_version["generation_metadata"]["manual_review"] == {
        "action": "approved",
        "note": "人工审核通过并加入发布队列。",
    }

    jobs_response = client.get("/jobs", params={"chapter_id": chapter_id})
    assert jobs_response.status_code == 200
    assert len(jobs_response.json()) == 1
    assert jobs_response.json()[0]["status"] == "publish_pending"
    assert jobs_response.json()[0]["id"] == response.json()["publish_job_id"]


def test_review_center_cannot_approve_the_same_candidate_twice() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_approve_once",
            "title": "Approve Once",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_approve_once",
            "chapter_number": 1,
            "title": "Single Approval Chapter",
            "goal": {"objective": "Approve this candidate once"},
        },
    ).json()["id"]
    persist_review_candidate(
        session=application.session,
        chapter_id=chapter_id,
        content="Candidate content",
        summary="Candidate summary",
        review_score=88,
        generation_metadata={"review_trace": {"final_review": {"approved": False}}},
    )
    application.session.commit()

    payload = {
        "chapter_id": chapter_id,
        "version_id": client.get(f"/chapters/{chapter_id}").json()["current_version_id"],
        "note": "人工批准一次。",
        "create_publish_job": True,
    }
    first_response = client.post("/api/review-center/approve", json=payload)
    assert first_response.status_code == 200
    version_count = len(client.get(f"/chapters/{chapter_id}/versions").json())
    job_count = len(client.get("/jobs", params={"chapter_id": chapter_id}).json())

    second_response = client.post("/api/review-center/approve", json=payload)

    assert second_response.status_code == 409
    assert len(client.get(f"/chapters/{chapter_id}/versions").json()) == version_count
    assert len(client.get("/jobs", params={"chapter_id": chapter_id}).json()) == job_count


def test_review_center_handoff_job_detail_can_be_queried() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_handoff_detail",
            "title": "Review Handoff Detail",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_handoff_detail",
            "chapter_number": 1,
            "title": "Handoff Detail Chapter",
            "goal": {"objective": "Observe queued publish detail"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Queued draft content",
            "summary": "Queued draft summary",
            "review_score": 8.2,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )
    client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "review_pending",
            "note": "送入人工审核后查看发布交接。",
        },
    )

    approve_response = client.post(
        "/api/review-center/approve",
        json={
            "chapter_id": chapter_id,
            "version_id": client.get(f"/chapters/{chapter_id}").json().get("current_version_id"),
            "note": "人工审核通过并排队。",
            "create_publish_job": True,
        },
    )

    assert approve_response.status_code == 200
    job_id = approve_response.json()["publish_job_id"]
    job_response = client.get(f"/jobs/{job_id}")

    assert job_response.status_code == 200
    assert job_response.json()["id"] == job_id
    assert job_response.json()["status"] == "publish_pending"
    assert job_response.json()["chapter_id"] == chapter_id


def test_review_center_can_retry_and_archive_failed_publish_job() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_review_failed_publish",
            "title": "Review Failed Publish",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_review_failed_publish",
            "chapter_number": 1,
            "title": "Failed Publish Chapter",
            "goal": {"objective": "Recover failed publish"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Queued draft content",
            "summary": "Queued draft summary",
            "review_score": 9.0,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )
    client.post("/api/creation/review", json={"chapter_id": chapter_id, "review_action": "review_pending"})
    approved = client.post("/api/review-center/approve", json={"chapter_id": chapter_id, "version_id": client.get(f"/chapters/{chapter_id}").json()["current_version_id"], "create_publish_job": True})
    assert approved.status_code == 200

    from app.db.repositories import PublishJobRepository

    repo = PublishJobRepository(application.session)
    job = repo.get_latest_for_chapter(chapter_id)
    repo.update_status(job, status="publish_failed", error_message="platform timeout")
    application.session.commit()

    retry_response = client.post(f"/chapters/{chapter_id}/retry-publish")
    assert retry_response.status_code == 202
    assert retry_response.json()["status"] == "publish_pending"

    repo.update_status(job, status="publish_failed", error_message="platform timeout")
    application.session.commit()
    archive_response = client.post(f"/chapters/{chapter_id}/archive-publish-failure")
    assert archive_response.status_code == 202
    assert archive_response.json()["status"] == "archived"


def test_chapter_detail_endpoint_returns_chapter_and_publish_summary() -> None:
    application = WriteAgentApplication.from_settings(_settings())
    client = TestClient(application.fastapi_app)

    client.post(
        "/books",
        json={
            "id": "book_chapter_detail",
            "title": "Chapter Detail",
            "platform": "fanqie",
            "genre": "urban",
        },
    )
    chapter_id = client.post(
        "/chapters",
        json={
            "book_id": "book_chapter_detail",
            "chapter_number": 1,
            "title": "Chapter Detail Page",
            "goal": {"objective": "Inspect chapter detail"},
        },
    ).json()["id"]

    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Detail page content",
            "summary": "Detail page summary",
            "review_score": 8.0,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )
    client.post("/api/creation/review", json={"chapter_id": chapter_id, "review_action": "review_pending"})
    approved = client.post("/api/review-center/approve", json={"chapter_id": chapter_id, "version_id": client.get(f"/chapters/{chapter_id}").json()["current_version_id"], "create_publish_job": True})
    assert approved.status_code == 200
    client.post(
        "/api/creation/save",
        json={
            "chapter_id": chapter_id,
            "content": "Detail page content v2",
            "summary": "Detail page summary v2",
            "review_score": 9.1,
            "state_deltas": [],
            "create_publish_job": False,
        },
    )
    client.post(
        "/api/creation/review",
        json={
            "chapter_id": chapter_id,
            "review_action": "review_pending",
            "note": "Detail page should show this manual note.",
        },
    )

    response = client.get(f"/chapters/{chapter_id}/detail")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Chapter Detail Page" in response.text
    assert "publish_pending" in response.text
    assert "版本历史" in response.text
    assert "发布历史" in response.text
    assert "Detail page summary" in response.text
    assert "Detail page summary v2" in response.text
    assert "Detail page should show this manual note." in response.text
    assert "/creation?book_id=book_chapter_detail" in response.text
    assert "/review-center" in response.text


def test_settings_built_web_and_worker_never_create_default_engine(tmp_path) -> None:
    # Fresh interpreter: the lazy module-level engine must stay unused when apps get their engine from settings.
    script = """
import sys
from fastapi.testclient import TestClient
from app.application import build_web_app, build_worker
from app.config import Settings
from app.db import base

settings = Settings(_env_file=None, database_url="sqlite+pysqlite:///" + sys.argv[1],
                    openai_api_key="", embedding_api_key="")
build_worker(settings).close()
with TestClient(build_web_app(settings)) as client:
    assert client.get("/books").status_code == 200
assert base._default_engine is None, "default engine was created"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "app.db")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-2000:]
