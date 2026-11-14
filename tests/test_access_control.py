from hashlib import sha256
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.application import build_web_app
from app.config import Settings

TOKEN = "YOUR_TOKEN_HERE"
COOKIE = "write_agent_token"


def make_client(token):
    settings = Settings(
        _env_file=None, database_url="sqlite+pysqlite:///:memory:",
        access_token=token, openai_api_key="", embedding_api_key="",
    )
    assert isinstance(settings.access_token, SecretStr)
    assert TOKEN not in repr(settings)
    return TestClient(build_web_app(settings), base_url="http://localhost", follow_redirects=False)


@pytest.fixture
def protected_client():
    with make_client(TOKEN) as client:
        yield client


def test_local_app_without_token_keeps_pages_and_api_open():
    with make_client("") as client:
        assert client.get("/books").status_code == 200
        assert client.get("/ops", headers={"Accept": "text/html"}).status_code == 200


def test_protected_api_page_and_exempt_paths(protected_client):
    response = protected_client.get("/books")
    assert response.status_code == 401
    assert response.json() == {"detail": "未授权"}
    page = protected_client.get("/ops", headers={"Accept": "application/xhtml+xml, text/html"})
    assert page.is_redirect and page.headers["location"] == "/login"
    assert protected_client.get("/health").json() == {"status": "ok"}
    assert protected_client.get("/static/common.css").status_code == 200
    assert protected_client.get("/healthz").status_code == 401


def test_bearer_authorizes_api_and_cannot_be_replaced_by_a_query(protected_client):
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert protected_client.get("/books", headers=bearer).status_code == 200
    assert protected_client.get("/books", params={"token": TOKEN}).status_code == 401
    assert protected_client.get("/books", headers={"Authorization": TOKEN}).status_code == 401
    assert protected_client.get("/books", headers={"Cookie": f"{COOKIE}={TOKEN}"}).status_code == 401


def test_login_styles_load_without_credentials_including_after_failure(protected_client):
    from app.ui.page_shell import static_url

    url = static_url("common.css")
    assert "?v=" in url
    form = protected_client.get("/login")
    failed = protected_client.post("/login", data={"token": "incorrect"})
    assert form.status_code == 200
    assert failed.status_code == 401 and "访问令牌错误" in failed.text
    for page in (form, failed):
        assert 'name="viewport"' in page.text and "width=device-width" in page.text
        assert 'rel="stylesheet"' in page.text and f'href="{url}"' in page.text
    assert COOKIE not in protected_client.cookies
    response = protected_client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_login_form_sets_digest_cookie_then_redirects(protected_client):
    form = protected_client.get("/login")
    assert form.status_code == 200
    assert "text/html" in form.headers["content-type"] and "<form" in form.text
    login = protected_client.post("/login", data={"token": TOKEN})
    assert login.status_code in (302, 303) and login.headers["location"] == "/ops"
    cookie = SimpleCookie(login.headers["set-cookie"])[COOKIE]
    assert cookie.value == sha256(TOKEN.encode()).hexdigest()
    assert cookie["httponly"] and cookie["samesite"].lower() == "strict"
    assert cookie["path"] == "/"
    assert protected_client.get("/ops", headers={"Accept": "text/html"}).status_code == 200
    # Either credential suffices even when the other one is invalid.
    assert protected_client.get("/books", headers={"Authorization": "Bearer invalid"}).status_code == 200


def test_failed_login_and_query_only_login_do_not_authenticate(protected_client):
    failed = protected_client.post("/login", data={"token": "incorrect"})
    assert not failed.is_redirect and "访问令牌错误" in failed.text
    assert COOKIE not in protected_client.cookies
    for method in ("GET", "POST"):
        response = protected_client.request(method, "/login", params={"token": TOKEN})
        assert not response.is_redirect and COOKIE not in protected_client.cookies
    assert protected_client.get("/books").status_code == 401


def test_stream_requires_authentication_before_calling_writer(protected_client):
    calls = []

    def fake_writer(context):
        calls.append(context["chapter_title"])
        return {"title": "Test", "content": "Draft", "summary": "", "state_deltas": []}

    protected_client.app.state.creation_writer = fake_writer
    protected_client.app.state.creation_stream_writer = None
    payload = {"chapter_title": "Test", "chapter_goal": "Goal"}
    path = "/api/creation/generate-stream"
    denied = protected_client.post(path, json=payload, headers={"Accept": "text/html"})
    assert denied.status_code == 401 and denied.json() == {"detail": "未授权"}
    assert calls == []
    stream = protected_client.post(path, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert stream.status_code == 200 and "event: done" in stream.text
    assert calls == ["Test"]


def test_auth_status_reports_whether_access_control_is_on():
    with make_client("") as client:
        assert client.get("/auth/status").json() == {"enabled": False}
        assert client.post("/logout").status_code == 404
    with make_client(TOKEN) as client:
        # Reachable before login so pages can decide whether to show the logout button.
        assert client.get("/auth/status").json() == {"enabled": True}


def test_logout_clears_cookie_and_requires_login_again(protected_client):
    protected_client.post("/login", data={"token": TOKEN})
    assert protected_client.get("/books").status_code == 200

    # A GET cannot log the user out; only the POST form does.
    assert protected_client.get("/logout").status_code == 405
    assert protected_client.get("/books").status_code == 200

    response = protected_client.post("/logout")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    cleared = SimpleCookie(response.headers["set-cookie"])[COOKIE]
    assert cleared.value == "" and cleared["max-age"] == "0" and cleared["path"] == "/"
    assert COOKIE not in protected_client.cookies
    assert protected_client.get("/books").status_code == 401
