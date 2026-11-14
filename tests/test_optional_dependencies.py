import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_core_install_can_publish_to_stub_and_use_sqlite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # The copy also isolates app.config's project-relative .env lookup.
    for directory in ("app", "alembic"):
        shutil.copytree(root / directory, tmp_path / directory, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(root / "alembic.ini", tmp_path / "alembic.ini")
    script = """
import sys
from pathlib import Path

sys.modules["playwright"] = None
sys.modules["psycopg"] = None

from fastapi.testclient import TestClient
from app.agents.publisher import publish_draft
from app.config import settings
from app.main import app
from app.services.fanqie_browser import create_fanqie_browser_client_from_settings

assert settings.model_config["env_file"] == Path.cwd() / ".env"
browser = create_fanqie_browser_client_from_settings(settings)
draft = publish_draft(browser=browser, chapter_title="Chapter", chapter_content="Draft", publish_mode="draft_only")
assert draft["success"] and draft["platform_draft_id"] == "fanqie_stub_001"

with TestClient(app) as client:
    book = {"id": "test-book", "title": "Test book", "platform": "local", "genre": "fiction"}
    assert client.post("/books", json=book).status_code == 201
    assert client.get("/books/test-book").json()["title"] == book["title"]
"""
    env = dict(
        os.environ,
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        FANQIE_BROWSER_MODE="stub",
        ACCESS_TOKEN="",
        OPENAI_API_KEY="",
        EMBEDDING_API_KEY="",
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, "Stub publishing or SQLite app startup and book access failed without optional packages"
