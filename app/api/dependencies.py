from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from app.db import base as db_base


def get_session(request: Request) -> Generator[Session, None, None]:
    # Apps built from settings set their own factory; bare apps fall back to the lazy default.
    factory = getattr(request.app.state, "session_factory", None) or db_base.SessionLocal
    session = factory()
    try:
        yield session
    finally:
        session.close()
