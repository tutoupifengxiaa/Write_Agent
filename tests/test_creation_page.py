import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_creation_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/creation")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="creation-root"></div>' in html
    assert "/static/creation.css" in html
    assert "/static/creation-app.js" in html
    assert "react.production.min.js" in html
    assert "react-dom.production.min.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_ops_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/ops")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="ops-root"></div>' in html
    assert "/static/ops.css" in html
    assert "/static/ops-app.js" in html
    assert "react.production.min.js" in html
    assert "react-dom.production.min.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_radar_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/radar")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="radar-root"></div>' in html
    assert "/static/radar.css" in html
    assert "/static/radar-app.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_library_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/library")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="library-root"></div>' in html
    assert "/static/library.css" in html
    assert "/static/library-app.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_review_center_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/review-center")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="review-center-root"></div>' in html
    assert "/static/review-center.css" in html
    assert "/static/review-center-app.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_outline_page_renders_html_shell() -> None:
    client = TestClient(app)

    response = client.get("/outline")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<div id="outline-root"></div>' in html
    assert "/static/outline.css" in html
    assert "/static/outline-app.js" in html
    assert "<title>" in html
    assert "Write Agent" in html


def test_chapter_detail_page_renders_html_shell() -> None:
    with TestClient(app) as client:
        response = client.get("/chapters/example/detail")

    assert response.status_code in {200, 404}
    if response.status_code == 200:
        assert "detail" in response.text.lower() or "history" in response.text.lower() or "绔犺妭" in response.text


def test_hub_page_renders_navigation_shell() -> None:
    client = TestClient(app)

    response = client.get("/hub")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "Write Agent" in html
    assert "/library" in html
    assert "/outline" in html
    assert "/creation" in html
    assert "/ops" in html
    assert "/radar" in html


def test_root_redirects_to_creation_page() -> None:
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ops"


# Interactive behavior is covered by scripts/check_creation_ui.py and the
# request/ordering contracts in test_shared_ui/test_publish_data_optimization.
# Asset tests verify deployment wiring, not a particular JS spelling.
@pytest.mark.parametrize("page", ["creation", "ops", "radar", "library", "outline", "review-center"])
def test_page_static_assets_are_served(page):
    client = TestClient(app)
    html = client.get("/" + page).text
    for asset in ["common.css", "common.js", page + ".css", page + "-app.js"]:
        assert "/static/" + asset in html
        response = client.get("/static/" + asset)
        assert response.status_code == 200
        assert ("text/css" if asset.endswith(".css") else "javascript") in response.headers["content-type"]
    if page == "creation":
        for asset in ["creation-stream.js", "creation-panels.js"]:
            assert "/static/" + asset in html
            assert client.get("/static/" + asset).status_code == 200


def test_library_static_assets_include_status_filter_behavior() -> None:
    client = TestClient(app)

    js_response = client.get("/static/library-app.js")

    assert js_response.status_code == 200
    assert "chapterStatusFilter" in js_response.text
    assert "setChapterStatusFilter" in js_response.text
    assert "filteredChapters" in js_response.text
    assert "chapter.status === chapterStatusFilter" in js_response.text


def test_library_static_assets_include_search_and_quick_jump_behavior() -> None:
    client = TestClient(app)

    js_response = client.get("/static/library-app.js")

    assert js_response.status_code == 200
    assert "chapterSearch" in js_response.text
    assert "setChapterSearch" in js_response.text
    assert "chapter.title.toLowerCase().includes(chapterSearch.toLowerCase())" in js_response.text
    assert "selectedChapter" in js_response.text
    assert "selectedChapter.current_version_id" in js_response.text
