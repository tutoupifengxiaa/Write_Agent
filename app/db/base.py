import logging

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, settings


class Base(DeclarativeBase):
    pass


def _is_memory_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite" and (
        url.database in (None, "", ":memory:")
        or (
            str(url.query.get("uri", "")).lower() == "true"
            and url.database.startswith("file:")
            and (url.database == "file::memory:" or url.query.get("mode") == "memory")
        )
    )


def warn_if_memory_database(engine: Engine) -> None:
    if _is_memory_sqlite(engine.url):
        logging.getLogger(__name__).warning(
            "Using an in-memory SQLite database. Data is lost when this process exits "
            "and is not shared between Web and Worker processes."
        )


def _configure_file_sqlite(connection, _record) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_engine_from_settings(settings: Settings) -> Engine:
    url = make_url(settings.database_url)
    engine_kwargs = {}
    if url.get_backend_name() == "sqlite":
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        if _is_memory_sqlite(url):
            engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **engine_kwargs)
    if url.get_backend_name() == "sqlite" and not _is_memory_sqlite(url):
        event.listen(engine, "connect", _configure_file_sqlite)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


engine = create_engine_from_settings(settings)
SessionLocal = make_session_factory(engine)
