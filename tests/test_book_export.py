from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import create_fastapi_app
from app.db.base import Base
from app.db.models import Book, Chapter, ChapterVersion


@pytest.fixture
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add_all([
            Book(id="book", title="钥匙里的城市", platform="fanqie", genre="urban", logline="一把旧钥匙。"),
            Book(id="empty", title="空书", platform="fanqie", genre="urban"),
        ])
        session.flush()
        session.add_all([
            Chapter(id="c2", book_id="book", chapter_number=2, title="第一段记忆", approved_version_id="v2a",
                    current_version_id="v2draft"),
            Chapter(id="c1", book_id="book", chapter_number=1, title="仓库门锁", approved_version_id="v1"),
            Chapter(id="c3", book_id="book", chapter_number=3, title="未审核", current_version_id="v3"),
            Chapter(id="e1", book_id="empty", chapter_number=1, title="草稿", current_version_id="e1v"),
        ])
        session.flush()
        session.add_all([
            ChapterVersion(id="v1", chapter_id="c1", source="approved", content="第一章正文\n"),
            ChapterVersion(id="v2a", chapter_id="c2", source="approved", content="第二章已通过正文"),
            ChapterVersion(id="v2draft", chapter_id="c2", source="draft", content="第二章未审核的新草稿"),
            ChapterVersion(id="v3", chapter_id="c3", source="draft", content="第三章草稿"),
            ChapterVersion(id="e1v", chapter_id="e1", source="draft", content="草稿"),
        ])
        session.commit()
    app = create_fastapi_app(initialize_database=False)
    app.state.session_factory = factory
    return TestClient(app)


def test_txt_export_contains_only_approved_chapters_in_order(client):
    response = client.get("/books/book/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "钥匙里的城市\n\n\n第1章 仓库门锁\n\n第一章正文\n\n\n第2章 第一段记忆\n\n第二章已通过正文\n"
    assert "未审核" not in response.text and "草稿" not in response.text
    assert f"filename*=UTF-8''{quote('钥匙里的城市.txt')}" in response.headers["content-disposition"]


def test_markdown_export_has_headings_and_logline(client):
    response = client.get("/books/book/export", params={"format": "md"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# 钥匙里的城市\n\n\n> 一把旧钥匙。\n\n\n## 第1章 仓库门锁\n\n第一章正文")
    assert "## 第2章 第一段记忆" in response.text


@pytest.mark.parametrize(("path", "params", "status"), [
    ("/books/empty/export", {}, 404),
    ("/books/missing/export", {}, 404),
    ("/books/book/export", {"format": "pdf"}, 422),
])
def test_export_errors(client, path, params, status):
    assert client.get(path, params=params).status_code == status
