from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.reviewer import review_chapter
from app.agents.reviser import revise_chapter
from app.agents.writer import stream_write_chapter, write_chapter
from app.agents.direction_generator import generate_direction_package
from app.api.routes_hub import router as hub_router
from app.api.routes_library import router as library_router
from app.api.routes_outline import router as outline_router
from app.agents.publisher import publish_draft
from app.api.routes_books import router as books_router
from app.api.routes_chapters import router as chapters_router
from app.api.routes_creation import router as creation_router
from app.api.routes_jobs import router as jobs_router
from app.api.routes_review_center import router as review_center_router
from app.api.routes_trends import router as trends_router
from app.db.base import SessionLocal, create_engine_from_settings, engine, make_session_factory, warn_if_memory_database
from app.db.schema_migrations import upgrade_database
from app.orchestrator.scheduler import Scheduler
from app.services.fanqie_browser import create_fanqie_browser_client_from_settings
from app.services.embedding import (
    KeywordEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)
from app.services.llm_client import OpenAIResponsesLLMClient


@dataclass
class WriteAgentApplication:
    fastapi_app: FastAPI | None
    session: Any
    writer_client: OpenAIResponsesLLMClient
    reviewer_client: OpenAIResponsesLLMClient
    scheduler: Scheduler
    reviser_client: OpenAIResponsesLLMClient | None = None
    embedding_client: Any = None
    session_factory: Callable[[], Any] = SessionLocal
    _owned_engine: Engine | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error = None
        for resource in (
            self.session,
            self.writer_client,
            self.reviewer_client,
            self.reviser_client,
            self.embedding_client,
        ):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        if self._owned_engine is not None:
            self._owned_engine.dispose()
        if first_error is not None:
            raise first_error

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> "WriteAgentApplication":
        worker = cls.build_worker(settings, session_factory=session_factory)
        try:
            worker.fastapi_app = build_web_app(settings, worker=worker)
        except BaseException:
            worker.close()
            raise
        return worker

    @classmethod
    def build_worker(
        cls, settings: Any, *, session_factory: Callable[[], Any] | None = None,
    ) -> "WriteAgentApplication":
        with ExitStack() as resources:
            db_engine, session_factory, owns_engine = _database_binding(settings, session_factory)
            if owns_engine:
                resources.callback(db_engine.dispose)
            warn_if_memory_database(db_engine)
            initialize_database(db_engine)
            session = session_factory()
            _close_on_exit(resources, session)
            writer_client = OpenAIResponsesLLMClient.from_settings(settings)
            _close_on_exit(resources, writer_client)
            reviewer_client = OpenAIResponsesLLMClient.from_settings(settings, review=True)
            _close_on_exit(resources, reviewer_client)
            reviser_client = OpenAIResponsesLLMClient.from_settings(settings)
            _close_on_exit(resources, reviser_client)
            publish_mode = getattr(settings, "publish_mode", "")
            publisher = None
            if publish_mode:
                browser_client = create_fanqie_browser_client_from_settings(settings)

                def publisher(chapter_title: str, chapter_content: str) -> dict:
                    return publish_draft(
                        browser=browser_client,
                        chapter_title=chapter_title,
                        chapter_content=chapter_content,
                        publish_mode=publish_mode,
                    )

            context_options = _embedding_options(settings)
            _close_on_exit(resources, context_options["embedding_client"])
            scheduler = Scheduler(
                session=session,
                writer=lambda context: write_chapter(writer_client, context),
                reviewer=lambda content, canonical_facts: review_chapter(
                    reviewer_client,
                    content,
                    canonical_facts,
                ),
                reviser=lambda original_content, revision_instructions: revise_chapter(
                    reviser_client,
                    original_content,
                    revision_instructions,
                ),
                publisher=publisher,
                **context_options,
            )
            worker = cls(
                fastapi_app=None,
                session=session,
                writer_client=writer_client,
                reviewer_client=reviewer_client,
                reviser_client=reviser_client,
                scheduler=scheduler,
                embedding_client=context_options["embedding_client"],
                session_factory=session_factory,
                _owned_engine=db_engine if owns_engine else None,
            )
            resources.pop_all()
            return worker


def _database_binding(settings, session_factory=None):
    if isinstance(session_factory, sessionmaker) and session_factory.kw.get("bind") is not None:
        return session_factory.kw["bind"], session_factory, False
    if getattr(settings, "database_url", None):
        db_engine = create_engine_from_settings(settings)
        return db_engine, session_factory or make_session_factory(db_engine), True
    return engine, session_factory or SessionLocal, False


def initialize_database(db_engine: Engine | None = None) -> None:
    upgrade_database(engine if db_engine is None else db_engine)


build_worker = WriteAgentApplication.build_worker


def _close_on_exit(resources: ExitStack, resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        resources.callback(close)


def _embedding_options(settings: Any) -> dict:
    fallback_mode = getattr(settings, "embedding_fallback_mode", "keyword")
    fallback_embedding_client = (
        KeywordEmbeddingClient() if fallback_mode == "keyword" else None
    )
    embedding_api_key = _secret_value(
        getattr(settings, "embedding_api_key", "")
    ).strip()
    if not embedding_api_key:
        embedding_api_key = _secret_value(
            getattr(settings, "openai_api_key", "")
        ).strip()
    embedding_base_url = str(
        getattr(settings, "embedding_base_url", "") or ""
    ).strip()
    if not embedding_base_url:
        embedding_base_url = str(
            getattr(settings, "openai_base_url", "") or ""
        ).strip()
    if embedding_api_key and embedding_base_url:
        embedding_client = OpenAICompatibleEmbeddingClient(
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            model=getattr(settings, "embedding_model", "text-embedding-3-small"),
            timeout_seconds=getattr(settings, "embedding_timeout_seconds", 30.0),
        )
        embedding_model_name = getattr(
            settings,
            "embedding_model",
            "text-embedding-3-small",
        )
    else:
        embedding_client = None
        embedding_model_name = "keyword-hash"
    return {
        "embedding_client": embedding_client,
        "embedding_model_name": embedding_model_name,
        "fallback_embedding_client": fallback_embedding_client,
        "embedding_fallback_mode": fallback_mode,
    }


def _configure_web(app: FastAPI, settings: Any, writer_client: Any, context_options: dict) -> None:
    app.state.creation_writer = lambda context: write_chapter(writer_client, context)
    app.state.creation_stream_writer = lambda context: stream_write_chapter(
        writer_client, context,
        reasoning_effort=getattr(settings, "openai_stream_reasoning_effort", "medium"),
    )
    app.state.direction_generator = lambda payload: generate_direction_package(writer_client, payload)
    app.state.outline_generator = app.state.direction_generator
    app.state.creation_context_options = context_options


def build_web_app(settings: Any, *, worker: WriteAgentApplication | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with ExitStack() as resources:
            if worker is None:
                db_engine, factory, owns_engine = _database_binding(settings)
                if owns_engine:
                    resources.callback(db_engine.dispose)
                warn_if_memory_database(db_engine)
                initialize_database(db_engine)
                app.state.session_factory = factory
                writer_client = OpenAIResponsesLLMClient.from_settings(settings)
                _close_on_exit(resources, writer_client)
                options = _embedding_options(settings)
                _close_on_exit(resources, options["embedding_client"])
                _configure_web(app, settings, writer_client, options)
            else:
                resources.callback(worker.close)
            yield

    app = create_fastapi_app(initialize_database=False, lifespan=lifespan, context_options={})
    if worker is not None:
        app.state.session_factory = worker.session_factory
        scheduler = worker.scheduler
        _configure_web(app, settings, worker.writer_client, {
            "embedding_client": worker.embedding_client,
            "embedding_model_name": scheduler.embedding_model_name,
            "fallback_embedding_client": scheduler.fallback_embedding_client,
            "embedding_fallback_mode": scheduler.embedding_fallback_mode,
        })
    return app


def create_fastapi_app(
    *,
    initialize_database: bool = True,
    lifespan=None,
    writer: Callable[[dict], dict] | None = None,
    stream_writer: Callable[[dict], Any] | None = None,
    direction_generator: Callable[[dict], dict] | None = None,
    outline_generator: Callable[[dict], dict] | None = None,
    context_options: dict | None = None,
) -> FastAPI:
    if initialize_database:
        upgrade_database(engine)
    app = FastAPI(lifespan=lifespan)
    app.state.session_factory = SessionLocal
    app.state.creation_writer = writer
    app.state.creation_stream_writer = stream_writer
    app.state.direction_generator = direction_generator
    app.state.outline_generator = outline_generator
    app.state.creation_context_options = context_options or (
        {} if lifespan is not None else {"fallback_embedding_client": KeywordEmbeddingClient()}
    )
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(books_router)
    app.include_router(chapters_router)
    app.include_router(creation_router)
    app.include_router(hub_router)
    app.include_router(jobs_router)
    app.include_router(library_router)
    app.include_router(outline_router)
    app.include_router(review_center_router)
    app.include_router(trends_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/ops")

    return app


def _secret_value(value: Any) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value or "")
