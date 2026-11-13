from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

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
from app.db.base import Base, SessionLocal, engine
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
    fastapi_app: FastAPI
    session: Any
    writer_client: OpenAIResponsesLLMClient
    reviewer_client: OpenAIResponsesLLMClient
    scheduler: Scheduler
    reviser_client: OpenAIResponsesLLMClient | None = None
    embedding_client: Any = None
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
        if first_error is not None:
            raise first_error

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
    ) -> "WriteAgentApplication":
        Base.metadata.create_all(bind=engine)
        upgrade_database(engine)
        session = session_factory()
        writer_client = OpenAIResponsesLLMClient.from_settings(settings)
        reviewer_client = OpenAIResponsesLLMClient.from_settings(settings, review=True)
        reviser_client = OpenAIResponsesLLMClient.from_settings(settings)
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
            embedding_client=embedding_client,
            embedding_model_name=embedding_model_name,
            fallback_embedding_client=fallback_embedding_client,
            embedding_fallback_mode=fallback_mode,
        )
        fastapi_app = create_fastapi_app(
            initialize_database=False,
            context_options={
                "embedding_client": embedding_client,
                "embedding_model_name": embedding_model_name,
                "fallback_embedding_client": fallback_embedding_client,
                "embedding_fallback_mode": fallback_mode,
            },
            writer=lambda context: write_chapter(writer_client, context),
            stream_writer=lambda context: stream_write_chapter(
                writer_client,
                context,
                reasoning_effort=getattr(settings, "openai_stream_reasoning_effort", "medium"),
            ),
            direction_generator=lambda payload: generate_direction_package(
                writer_client,
                payload,
            ),
            outline_generator=lambda payload: generate_direction_package(
                writer_client,
                payload,
            ),
        )
        application = cls(
            fastapi_app=fastapi_app,
            session=session,
            writer_client=writer_client,
            reviewer_client=reviewer_client,
            scheduler=scheduler,
            reviser_client=reviser_client,
            embedding_client=embedding_client,
        )
        fastapi_app.router.add_event_handler("shutdown", application.close)
        return application


def create_fastapi_app(
    *,
    initialize_database: bool = True,
    writer: Callable[[dict], dict] | None = None,
    stream_writer: Callable[[dict], Any] | None = None,
    direction_generator: Callable[[dict], dict] | None = None,
    outline_generator: Callable[[dict], dict] | None = None,
    context_options: dict | None = None,
) -> FastAPI:
    if initialize_database:
        Base.metadata.create_all(bind=engine)
    app = FastAPI()
    app.state.creation_writer = writer
    app.state.creation_stream_writer = stream_writer
    app.state.direction_generator = direction_generator
    app.state.outline_generator = outline_generator
    app.state.creation_context_options = context_options or {
        "fallback_embedding_client": KeywordEmbeddingClient(),
    }
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
