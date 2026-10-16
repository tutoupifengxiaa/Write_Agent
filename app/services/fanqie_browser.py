from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict


class DraftUploadResult(TypedDict):
    success: bool
    platform_draft_id: str
    screenshot_path: str


class FanqieBrowserClient:
    def upload_draft(self, title: str, content: str) -> DraftUploadResult:
        return {
            "success": True,
            "platform_draft_id": "fanqie_stub_001",
            "screenshot_path": "logs/fanqie_stub.png",
        }


class BrowserPage(Protocol):
    def goto(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def screenshot(self, *, path: str) -> None: ...
    def is_visible(self, selector: str) -> bool: ...
    def close(self) -> None: ...


class BrowserSession(Protocol):
    def new_page(self) -> BrowserPage: ...
    def close(self) -> None: ...


BrowserFactory = Callable[[], BrowserSession]

DEFAULT_SELECTORS = {
    "title_input": "#chapter-title",
    "content_input": "#chapter-content",
    "save_draft_button": "[data-action='save-draft']",
    "login_password_input": "input[type='password']",
}


class PlaywrightFanqieBrowserClient:
    def __init__(
        self,
        *,
        author_url: str,
        screenshot_dir: str,
        browser_factory: BrowserFactory | None,
        selectors: dict[str, str] | None = None,
    ) -> None:
        self.author_url = author_url
        self.screenshot_dir = screenshot_dir
        self.browser_factory = browser_factory
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}

    def upload_draft(self, title: str, content: str) -> DraftUploadResult:
        if self.browser_factory is None:
            raise RuntimeError("playwright browser factory is required")
        if not self.author_url:
            raise RuntimeError("fanqie author url is required")

        session = self.browser_factory()
        page = session.new_page()
        screenshot_path = str(Path(self.screenshot_dir) / "fanqie_playwright_stub.png")
        try:
            page.goto(self.author_url)
            if page.is_visible(self.selectors["login_password_input"]):
                raise RuntimeError("fanqie login required")
            page.fill(self.selectors["title_input"], title)
            page.fill(self.selectors["content_input"], content)
            page.click(self.selectors["save_draft_button"])
            page.screenshot(path=screenshot_path)
        finally:
            page.close()
            session.close()

        return {
            "success": True,
            "platform_draft_id": f"playwright::{title[:12]}",
            "screenshot_path": screenshot_path,
        }


def create_fanqie_browser_client(
    *,
    browser_mode: str,
    author_url: str = "",
    screenshot_dir: str = "logs",
    browser_factory: BrowserFactory | None = None,
) -> FanqieBrowserClient | PlaywrightFanqieBrowserClient:
    if browser_mode == "stub":
        return FanqieBrowserClient()
    if browser_mode == "playwright":
        return PlaywrightFanqieBrowserClient(
            author_url=author_url,
            screenshot_dir=screenshot_dir,
            browser_factory=browser_factory,
        )
    raise ValueError(f"unknown browser mode: {browser_mode}")


def create_playwright_browser_factory(
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
) -> BrowserFactory:
    def factory() -> Any:
        sync_api = import_module("playwright.sync_api")
        playwright = sync_api.sync_playwright().start()
        return playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)

    return factory


def create_fanqie_browser_client_from_settings(
    settings: Any,
    *,
    browser_factory: BrowserFactory | None = None,
) -> FanqieBrowserClient | PlaywrightFanqieBrowserClient:
    if (
        getattr(settings, "fanqie_browser_mode", "stub") == "playwright"
        and browser_factory is None
    ):
        browser_factory = create_playwright_browser_factory(
            headless=getattr(settings, "fanqie_headless", True),
            slow_mo_ms=getattr(settings, "fanqie_slow_mo_ms", 0),
        )

    return create_fanqie_browser_client(
        browser_mode=getattr(settings, "fanqie_browser_mode", "stub"),
        author_url=getattr(settings, "fanqie_author_url", ""),
        screenshot_dir=getattr(settings, "fanqie_screenshot_dir", "logs"),
        browser_factory=browser_factory,
    )
