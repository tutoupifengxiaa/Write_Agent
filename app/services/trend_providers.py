from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
import re

import httpx

from app.services.trend_parsers import (
    has_font_encoded_text,
    parse_fanqie_book_details,
    parse_provider_ranking,
)


HotlistItem = dict[str, Any]


def _reference_samples(provider: str, provider_label: str) -> list[HotlistItem]:
    if provider == "fanqie":
        return [
            {
                "provider": provider,
                "provider_label": provider_label,
                "rank": 1,
                "title": "都市重生：我在风口翻盘",
                "author": "样例作者",
                "category": "都市",
                "url": "https://fanqienovel.com",
                "summary": "快节奏都市逆袭与资源争夺。",
            },
            {
                "provider": provider,
                "provider_label": provider_label,
                "rank": 2,
                "title": "高武觉醒：从底层到巅峰",
                "author": "样例作者",
                "category": "高武",
                "url": "https://fanqienovel.com",
                "summary": "成长升级与强冲突推进。",
            },
        ]
    if provider == "qimao":
        return [
            {
                "provider": provider,
                "provider_label": provider_label,
                "rank": 1,
                "title": "寒门逆袭：小人物上桌",
                "author": "样例作者",
                "category": "历史",
                "url": "https://www.qimao.com/paihang",
                "summary": "草根跃迁与权力博弈。",
            },
            {
                "provider": provider,
                "provider_label": provider_label,
                "rank": 2,
                "title": "离婚后她爆红全网",
                "author": "样例作者",
                "category": "现言",
                "url": "https://www.qimao.com/paihang",
                "summary": "情绪强钩子与女性成长。",
            },
        ]
    return [
        {
            "provider": provider,
            "provider_label": provider_label,
            "rank": 1,
            "title": "赛道新贵：从失意到翻身",
            "author": "样例作者",
            "category": "都市",
            "url": "https://www.qidian.com/rank/",
            "summary": "以行业热点切入的升级故事。",
        }
    ]


def _resolve_fanqie_metadata(
    items: list[HotlistItem],
    http_get: Callable[[str], httpx.Response],
) -> None:
    for item in items:
        needs_text = any(has_font_encoded_text(item.get(field)) for field in ("title", "author"))
        if not needs_text and item.get("category"):
            continue
        parsed = urlsplit(item["url"])
        match = re.fullmatch(r"/page/(\d+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "fanqienovel.com" or not match:
            raise ValueError("番茄榜单包含无效的作品详情地址")
        try:
            response = http_get(item["url"])
            response.raise_for_status()
            metadata = parse_fanqie_book_details(html=response.text, book_id=match[1])
        except Exception:
            if not needs_text:
                # Missing optional metadata must not discard a readable ranking.
                continue
            raise ValueError("番茄作品文字校正失败，请稍后刷新采集") from None
        item.update(metadata)


def _provider_payload(
    *,
    provider: str,
    provider_label: str,
    url: str,
    http_get: Callable[[str], httpx.Response],
    browser_get: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    reference_samples = _reference_samples(provider, provider_label)
    try:
        response = http_get(url)
        response.raise_for_status()
    except Exception as exc:
        return {
            "provider": provider,
            "provider_label": provider_label,
            "source_url": url,
            "status": "failed",
            "error": str(exc),
            "items": [],
            "reference_samples": reference_samples,
        }

    try:
        items = parse_provider_ranking(
            provider=provider,
            html=response.text,
            source_url=url,
        )
        if not items and provider == "qidian" and browser_get is not None:
            try:
                items = parse_provider_ranking(
                    provider=provider,
                    html=browser_get(url),
                    source_url=url,
                )
            except Exception:
                raise ValueError("起点浏览器采集失败，请确认浏览器可用或稍后重试") from None
        if not items:
            if provider == "qidian":
                raise ValueError("起点未返回有效榜单，可能仍在进行访问验证，请稍后重试")
            raise ValueError("no ranking items parsed")
        if provider == "fanqie":
            _resolve_fanqie_metadata(items, http_get)
    except Exception as exc:
        return {
            "provider": provider,
            "provider_label": provider_label,
            "source_url": url,
            "status": "degraded",
            "error": str(exc),
            "items": [],
            "reference_samples": reference_samples,
        }

    return {
        "provider": provider,
        "provider_label": provider_label,
        "source_url": url,
        "status": "real",
        "items": items,
        "reference_samples": reference_samples,
    }


def _fetch_with_getter(
    http_get: Callable[[str], httpx.Response],
    browser_get: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    providers = [
        _provider_payload(
            provider="fanqie",
            provider_label="番茄小说",
            url="https://fanqienovel.com/rank",
            http_get=http_get,
        ),
        _provider_payload(
            provider="qimao",
            provider_label="七猫小说",
            url="https://www.qimao.com/paihang",
            http_get=http_get,
        ),
        _provider_payload(
            provider="qidian",
            provider_label="起点中文网",
            url="https://www.qidian.com/rank/yuepiao/",
            http_get=http_get,
            browser_get=browser_get,
        ),
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }


def _fetch_qidian_browser_html(url: str) -> str:
    from time import monotonic

    from playwright.sync_api import Error, sync_playwright

    # Each collection uses a temporary browser session, without saved login data.
    with sync_playwright() as playwright:
        browser = None
        for channel in (None, "chrome", "msedge"):
            options = {"channel": channel} if channel else {}
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                    timeout=10000,
                    **options,
                )
                break
            except Error:
                continue
        if browser is None:
            raise RuntimeError("起点采集需要 Chromium、Chrome 或 Edge 浏览器")
        try:
            page = browser.new_page()
            deadline = monotonic() + 30
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.locator(
                ".rank-view-list .book-mid-info h2 a, .rank-list a.book-name, .rank-list a.name"
            ).first.wait_for(
                state="attached",
                timeout=max(1, int((deadline - monotonic()) * 1000)),
            )
            return page.content()
        finally:
            browser.close()


def fetch_hotlists(
    *,
    http_get: Callable[[str], httpx.Response] | None = None,
    browser_get: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    if http_get is not None:
        return _fetch_with_getter(http_get, browser_get=browser_get)

    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        return _fetch_with_getter(
            client.get,
            browser_get=browser_get or _fetch_qidian_browser_html,
        )
