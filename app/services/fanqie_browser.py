from __future__ import annotations

from contextlib import ExitStack, suppress
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict
from uuid import uuid4


class DraftUploadResult(TypedDict):
    success: bool
    platform_draft_id: str | None
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


class BrowserContext(Protocol):
    def new_page(self) -> BrowserPage: ...
    def close(self) -> None: ...


class BrowserSession(BrowserContext, Protocol):
    def new_context(self, *, storage_state: str) -> BrowserContext: ...


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
        storage_state_path: str | None = None,
    ) -> None:
        self.author_url = author_url
        self.screenshot_dir = screenshot_dir
        self.browser_factory = browser_factory
        self.storage_state_path = storage_state_path
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}

    def upload_draft(self, title: str, content: str) -> DraftUploadResult:
        if self.browser_factory is None:
            raise RuntimeError("playwright browser factory is required")
        if not self.author_url:
            raise RuntimeError("fanqie author url is required")

        directory = Path(self.screenshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        title_hash = sha256(title.encode("utf-8")).hexdigest()[:12]
        screenshot_path = str(directory / f"fanqie_{timestamp}_{title_hash}_{uuid4().hex}.png")
        resources = ExitStack()
        session = self.browser_factory()
        resources.callback(session.close)
        try:
            page_owner: BrowserContext = session
            if self.storage_state_path and Path(self.storage_state_path).is_file():
                page_owner = session.new_context(storage_state=self.storage_state_path)
                resources.callback(page_owner.close)
            page = page_owner.new_page()
            resources.callback(page.close)
            try:
                page.goto(self.author_url)
                if page.is_visible(self.selectors["login_password_input"]):
                    raise RuntimeError("fanqie login required")
                page.fill(self.selectors["title_input"], title)
                page.fill(self.selectors["content_input"], content)
                page.click(self.selectors["save_draft_button"])
            except Exception:
                with suppress(Exception):
                    page.screenshot(path=screenshot_path)
                raise
            page.screenshot(path=screenshot_path)
        except BaseException:
            with suppress(Exception):
                resources.close()
            raise
        resources.close()

        return {
            "success": True,
            "platform_draft_id": None,
            "screenshot_path": screenshot_path,
        }


def create_fanqie_browser_client(
    *,
    browser_mode: str,
    author_url: str = "",
    screenshot_dir: str = "logs",
    browser_factory: BrowserFactory | None = None,
    storage_state_path: str | None = None,
) -> FanqieBrowserClient | PlaywrightFanqieBrowserClient:
    if browser_mode == "stub":
        return FanqieBrowserClient()
    if browser_mode == "playwright":
        return PlaywrightFanqieBrowserClient(
            author_url=author_url,
            screenshot_dir=screenshot_dir,
            browser_factory=browser_factory,
            storage_state_path=storage_state_path,
        )
    raise ValueError(f"unknown browser mode: {browser_mode}")


class _PlaywrightSession:
    def __init__(self, browser: Any, playwright: Any) -> None:
        self.browser = browser
        self.playwright = playwright

    def new_page(self) -> BrowserPage:
        return self.browser.new_page()

    def new_context(self, *, storage_state: str) -> BrowserContext:
        return self.browser.new_context(storage_state=storage_state)

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self.playwright.stop()


def create_playwright_browser_factory(
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
) -> BrowserFactory:
    def factory() -> BrowserSession:
        sync_api = import_module("playwright.sync_api")
        playwright = sync_api.sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        except BaseException:
            with suppress(Exception):
                playwright.stop()
            raise
        return _PlaywrightSession(browser, playwright)

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
        storage_state_path=getattr(settings, "fanqie_storage_state_path", None),
        browser_factory=browser_factory,
    )
