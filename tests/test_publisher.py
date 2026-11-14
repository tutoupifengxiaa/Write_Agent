from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.publisher import ensure_publishable, publish_draft
from app.services.fanqie_browser import (
    DraftUploadResult,
    FanqieBrowserClient,
    PlaywrightFanqieBrowserClient,
    create_fanqie_browser_client,
    create_fanqie_browser_client_from_settings,
    create_playwright_browser_factory,
)


class FakeBrowser:
    def __init__(self) -> None:
        self.last_title: str | None = None
        self.last_content: str | None = None

    def upload_draft(self, title: str, content: str) -> DraftUploadResult:
        self.last_title = title
        self.last_content = content
        return {
            "success": True,
            "platform_draft_id": "fanqie_123",
            "screenshot_path": "logs/ch1.png",
        }


def test_publish_draft_returns_success_payload():
    browser = FakeBrowser()

    result = publish_draft(
        browser=browser,
        chapter_title="Chapter 1 Opening",
        chapter_content="Body text",
        publish_mode="draft_only",
    )

    assert result["success"] is True
    assert result["platform_draft_id"] == "fanqie_123"
    assert browser.last_title == "Chapter 1 Opening"
    assert browser.last_content == "Body text"


def test_publish_draft_rejects_non_draft_only_mode():
    with pytest.raises(ValueError):
        publish_draft(
            browser=FakeBrowser(),
            chapter_title="Chapter 1 Opening",
            chapter_content="Body text",
            publish_mode="publish_now",
        )


def test_ensure_publishable_rejects_invalid_status():
    with pytest.raises(ValueError):
        ensure_publishable("draft_planned")


def test_create_fanqie_browser_client_defaults_to_stub_mode():
    client = create_fanqie_browser_client(browser_mode="stub")

    assert isinstance(client, FanqieBrowserClient)
    result = client.upload_draft("Chapter 1", "Body")
    assert result["success"] is True
    assert result["platform_draft_id"] == "fanqie_stub_001"


def test_create_fanqie_browser_client_rejects_unknown_mode():
    import pytest

    with pytest.raises(ValueError, match="unknown browser mode"):
        create_fanqie_browser_client(browser_mode="invalid")


def test_playwright_mode_requires_factory_dependency():
    import pytest

    client = create_fanqie_browser_client(browser_mode="playwright")

    with pytest.raises(RuntimeError, match="playwright browser factory is required"):
        client.upload_draft("Chapter 1", "Body")


def test_playwright_client_runs_minimal_upload_flow(tmp_path):
    class FakePage:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def goto(self, url: str) -> None:
            self.calls.append(("goto", url))

        def fill(self, selector: str, value: str) -> None:
            self.calls.append(("fill", selector, value))

        def click(self, selector: str) -> None:
            self.calls.append(("click", selector))

        def screenshot(self, *, path: str) -> None:
            self.calls.append(("screenshot", path))

        def is_visible(self, selector: str) -> bool:
            return False

        def close(self) -> None:
            self.calls.append(("close_page",))

    class FakeSession:
        def __init__(self) -> None:
            self.page = FakePage()
            self.closed = False

        def new_page(self) -> FakePage:
            return self.page

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    next_session = FakeSession()
    sessions = iter([session, next_session])
    client = PlaywrightFanqieBrowserClient(
        author_url="https://author.example.com",
        screenshot_dir=str(tmp_path),
        browser_factory=lambda: next(sessions),
    )

    result = client.upload_draft("Chapter 1", "Body text")
    second = client.upload_draft("Chapter 1", "Body text")

    assert result["success"] is True
    assert result["platform_draft_id"] is None
    assert Path(result["screenshot_path"]).parent == tmp_path
    assert Path(result["screenshot_path"]).suffix == ".png"
    assert result["screenshot_path"] != second["screenshot_path"]
    assert ("screenshot", result["screenshot_path"]) in session.page.calls
    assert ("screenshot", second["screenshot_path"]) in next_session.page.calls
    assert session.page.calls[-1] == ("close_page",)
    assert ("goto", "https://author.example.com") in session.page.calls
    assert ("fill", "#chapter-title", "Chapter 1") in session.page.calls
    assert ("fill", "#chapter-content", "Body text") in session.page.calls
    assert ("click", "[data-action='save-draft']") in session.page.calls
    assert session.closed is True


@pytest.mark.parametrize("screenshot_fails", [False, True])
def test_playwright_client_rejects_when_not_logged_in(tmp_path, screenshot_fails):
    class FakePage:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def goto(self, url: str) -> None:
            self.calls.append(("goto", url))

        def fill(self, selector: str, value: str) -> None:
            self.calls.append(("fill", selector, value))

        def click(self, selector: str) -> None:
            self.calls.append(("click", selector))

        def screenshot(self, *, path: str) -> None:
            self.calls.append(("screenshot", path))
            if screenshot_fails:
                raise RuntimeError("screenshot failed")

        def close(self) -> None:
            self.calls.append(("close_page",))

        def is_visible(self, selector: str) -> bool:
            return selector == "input[type='password']"

    class FakeSession:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

        def close(self) -> None:
            pass

    session = FakeSession()
    client = PlaywrightFanqieBrowserClient(
        author_url="https://author.example.com",
        screenshot_dir=str(tmp_path),
        browser_factory=lambda: session,
    )

    with pytest.raises(RuntimeError, match="fanqie login required"):
        client.upload_draft("Chapter 1", "Body text")
    shots = [Path(call[1]) for call in session.page.calls if call[0] == "screenshot"]
    assert len(shots) == 1
    assert shots[0].parent == tmp_path
    assert session.page.calls[-1] == ("close_page",)


def test_playwright_client_uses_custom_selectors(tmp_path):
    class FakePage:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def goto(self, url: str) -> None:
            self.calls.append(("goto", url))

        def fill(self, selector: str, value: str) -> None:
            self.calls.append(("fill", selector, value))

        def click(self, selector: str) -> None:
            self.calls.append(("click", selector))

        def screenshot(self, *, path: str) -> None:
            self.calls.append(("screenshot", path))

        def close(self) -> None:
            self.calls.append(("close_page",))

        def is_visible(self, selector: str) -> bool:
            return False

    class FakeSession:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

        def close(self) -> None:
            pass

    session = FakeSession()
    client = PlaywrightFanqieBrowserClient(
        author_url="https://author.example.com",
        screenshot_dir=str(tmp_path),
        browser_factory=lambda: session,
        selectors={
            "title_input": "#custom-title",
            "content_input": "#custom-content",
            "save_draft_button": "#custom-save",
            "login_password_input": "#custom-password",
        },
    )

    client.upload_draft("Chapter 1", "Body text")

    assert ("fill", "#custom-title", "Chapter 1") in session.page.calls
    assert ("fill", "#custom-content", "Body text") in session.page.calls
    assert ("click", "#custom-save") in session.page.calls


def test_create_fanqie_browser_client_from_settings_uses_playwright_mode():
    settings = SimpleNamespace(
        fanqie_browser_mode="playwright",
        fanqie_author_url="https://author.example.com",
        fanqie_screenshot_dir="logs",
    )

    client = create_fanqie_browser_client_from_settings(
        settings,
        browser_factory=lambda: None,
    )

    assert isinstance(client, PlaywrightFanqieBrowserClient)
    assert client.author_url == "https://author.example.com"


@pytest.mark.parametrize("failure", [None, "launch", "close"])
def test_create_playwright_browser_factory_launches_chromium(monkeypatch, failure):
    launch_calls: list[tuple[bool, int]] = []
    cleanup = []

    class FakeBrowser:
        def new_page(self):
            raise AssertionError("new_page should not be called in this test")

        def close(self):
            cleanup.append("browser.close")
            if failure == "close":
                raise RuntimeError("close failed")

    class FakeChromium:
        def launch(self, *, headless: bool, slow_mo: int):
            launch_calls.append((headless, slow_mo))
            if failure == "launch":
                raise RuntimeError("launch failed")
            return FakeBrowser()

    class FakePlaywrightManager:
        chromium = FakeChromium()

        def start(self):
            return self

        def stop(self):
            cleanup.append("playwright.stop")

    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakePlaywrightManager()),
    )

    factory = create_playwright_browser_factory(headless=False, slow_mo_ms=250)
    if failure is None:
        factory().close()
    else:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            factory().close()
    assert launch_calls == [(False, 250)]
    if failure == "launch":
        assert cleanup == ["playwright.stop"]
    else:
        assert cleanup == ["browser.close", "playwright.stop"]


@pytest.mark.parametrize("state_exists", [True, False])
def test_playwright_client_loads_existing_storage_state(tmp_path, state_exists):
    from app.config import Settings

    state = tmp_path / "state.json"
    if state_exists:
        state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    settings = Settings(
        _env_file=None,
        fanqie_browser_mode="playwright",
        fanqie_author_url="https://author.example.com",
        fanqie_screenshot_dir=str(tmp_path),
        fanqie_storage_state_path=str(state),
    )
    browser = Mock()
    context = browser.new_context.return_value
    page = context.new_page.return_value if state_exists else browser.new_page.return_value
    page.is_visible.return_value = False
    client = create_fanqie_browser_client_from_settings(settings, browser_factory=lambda: browser)
    client.upload_draft("Chapter 1", "Body")

    if state_exists:
        browser.new_context.assert_called_once_with(storage_state=str(state))
        browser.new_page.assert_not_called()
        context.new_page.assert_called_once_with()
        context.close.assert_called_once_with()
    else:
        browser.new_context.assert_not_called()
        browser.new_page.assert_called_once_with()
    page.close.assert_called_once_with()
    browser.close.assert_called_once_with()
