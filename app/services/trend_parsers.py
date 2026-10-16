from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit


PROVIDER_RULES = {
    "fanqie": {
        "container_class": "muye-rank-book-list",
        "item_class": "rank-book-item",
        "rank_class": "book-item-index",
        "title_parent_class": "title",
        "author_class": "author",
    },
    "qimao": {
        "container_class": "rank-list",
        "item_class": "rank-list-item",
        "rank_class": "rank-number",
        "title_class": "s-book-title",
        "info_class": "s-book-info",
    },
    "qidian": {
        "container_class": "rank-list",
        "rank_attr": "data-rid",
        "title_classes": ("book-name", "name"),
        "author_class": "writer",
        "category_class": "type",
    },
}


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

_SAFE_METADATA_ATTRIBUTES = {"class", "data-bid", "data-rid"}
_SENSITIVE_ATTRIBUTE_PARTS = {
    "token",
    "session",
    "auth",
    "cookie",
    "signature",
    "key",
}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    parent: _Node | None = None
    content: list[str | _Node] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def descendants(self) -> Iterator[_Node]:
        for part in self.content:
            if isinstance(part, _Node):
                yield part
                yield from part.descendants()

    def text_content(self) -> str:
        return " ".join(self._raw_text_content().split())

    def _raw_text_content(self) -> str:
        return "".join(
            part if isinstance(part, str) else part._raw_text_content()
            for part in self.content
        )


class RankingParser(HTMLParser):
    def __init__(
        self,
        *,
        provider: str,
        source_url: str,
        rule: dict[str, Any],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.provider = provider
        self.source_url = source_url
        self.rule = rule
        self.root = _Node(tag="document", attrs={})
        self._stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _Node(tag=tag, attrs=dict(attrs), parent=self._stack[-1])
        self._stack[-1].content.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _Node(tag=tag, attrs=dict(attrs), parent=self._stack[-1])
        self._stack[-1].content.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._stack[-1].content.append(data)

    @property
    def items(self) -> list[dict[str, Any]]:
        if self.provider == "fanqie":
            return self._parse_fanqie()
        if self.provider == "qimao":
            return self._parse_qimao()
        return self._parse_qidian()

    def _parse_fanqie(self) -> list[dict[str, Any]]:
        containers = _nodes_with_class(self.root, self.rule["container_class"])
        if not containers:
            return []
        result = []
        for item in _nodes_with_class(containers[0], self.rule["item_class"]):
            rank_node = _first_with_class(item, self.rule["rank_class"])
            rank_value_node = _first_tag(rank_node, "h1") or rank_node
            title_parent = _first_with_class(item, self.rule["title_parent_class"])
            title_node = _first_tag(title_parent, "a")
            author_parent = _first_with_class(item, self.rule["author_class"])
            result_item = self._normalized_item(
                item=item,
                rank=_first_integer(
                    rank_value_node.text_content() if rank_value_node else ""
                ),
                title_node=title_node,
                author=author_parent.text_content() if author_parent else None,
                category=None,
            )
            if result_item is not None:
                result.append(result_item)
        return result

    def _parse_qimao(self) -> list[dict[str, Any]]:
        containers = _nodes_with_class(self.root, self.rule["container_class"])
        if not containers:
            return []
        result = []
        for item in _nodes_with_class(containers[0], self.rule["item_class"]):
            rank_node = _first_with_class(item, self.rule["rank_class"])
            title_node = _first_with_class(item, self.rule["title_class"], tag="a")
            info_node = _first_with_class(item, self.rule["info_class"])
            info_links = (
                [node for node in info_node.descendants() if node.tag == "a"]
                if info_node
                else []
            )
            result_item = self._normalized_item(
                item=item,
                rank=_first_integer(rank_node.text_content() if rank_node else ""),
                title_node=title_node,
                author=info_links[0].text_content() if info_links else None,
                category=info_links[1].text_content() if len(info_links) > 1 else None,
            )
            if result_item is not None:
                result.append(result_item)
        return result

    def _parse_qidian(self) -> list[dict[str, Any]]:
        detailed_containers = _nodes_with_class(self.root, "rank-view-list")
        containers = detailed_containers or _nodes_with_class(self.root, self.rule["container_class"])
        if not containers:
            return []
        result = []
        for item in containers[0].descendants():
            if item.tag != "li" or self.rule["rank_attr"] not in item.attrs:
                continue
            title_node = next(
                (
                    node
                    for node in item.descendants()
                    if node.tag == "a"
                    and node.classes.intersection(self.rule["title_classes"])
                ),
                None,
            )
            author_node = _first_with_class(item, self.rule["author_class"], tag="a")
            category_node = _first_with_class(
                item,
                self.rule["category_class"],
                tag="a",
            )
            if detailed_containers:
                info = _first_with_class(item, "book-mid-info")
                if info is None:
                    continue
                title_node = _first_tag(_first_tag(info, "h2"), "a")
                author_info = _first_with_class(info, "author")
                author_node = _first_with_class(author_info, "name", tag="a") if author_info else None
                category_node = next(
                    (node for node in author_info.descendants()
                     if node.tag == "a" and node is not author_node
                     and "go-sub-type" not in node.classes),
                    None,
                ) if author_info else None
            result_item = self._normalized_item(
                item=item,
                rank=_first_integer(item.attrs.get(self.rule["rank_attr"]) or ""),
                title_node=title_node,
                author=author_node.text_content() if author_node else None,
                category=category_node.text_content() if category_node else None,
            )
            if result_item is not None:
                result.append(result_item)
        # The overview previously exposed the first ten monthly-ranking entries.
        return result[:10] if detailed_containers else result

    def _normalized_item(
        self,
        *,
        item: _Node,
        rank: int | None,
        title_node: _Node | None,
        author: str | None,
        category: str | None,
    ) -> dict[str, Any] | None:
        title = title_node.text_content() if title_node else ""
        if rank is None or not title:
            return None
        return {
            "id": f"{self.provider}:{rank}",
            "provider": self.provider,
            "rank": rank,
            "title": title,
            "author": author or None,
            "category": category or None,
            "url": _safe_url(
                self.source_url,
                title_node.attrs.get("href") or "",
            ),
            "raw_metadata": {
                "item_attributes": _safe_metadata_attributes(item.attrs),
                "title_attributes": _safe_metadata_attributes(title_node.attrs),
            },
        }


def parse_fanqie_book_details(*, html: str, book_id: str) -> dict[str, str]:
    # Detail-page state contains ordinary Unicode; ranking text uses a custom font.
    # Decode JSON only: never execute scripts supplied by the remote page.
    marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if marker is None:
        raise ValueError("番茄作品详情缺少书籍数据")
    state, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    page = state.get("page", {}) if isinstance(state, dict) else {}
    if not isinstance(page, dict) or str(page.get("bookId")) != book_id:
        raise ValueError("番茄作品详情与榜单书籍不匹配")
    result = {"title": page.get("bookName"), "author": page.get("author")}
    if any(
        not isinstance(value, str) or not value.strip() or has_font_encoded_text(value)
        for value in result.values()
    ):
        raise ValueError("番茄作品详情仍含异常文字")
    result = {key: value.strip() for key, value in result.items()}
    categories = page.get("categoryV2") or []
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except ValueError:
            categories = []
    category = next(
        (entry.get("Name") for entry in categories
         if isinstance(entry, dict) and entry.get("MainCategory") is True),
        None,
    ) if isinstance(categories, list) else None
    category = category or page.get("category")
    if isinstance(category, str) and category.strip() and not has_font_encoded_text(category):
        result["category"] = category.strip()
    return result


def has_font_encoded_text(value: str | None) -> bool:
    return bool(re.search(
        r"[\ue000-\uf8ff\ufffd\U000f0000-\U000ffffd\U00100000-\U0010fffd]",
        value or "",
    ))


def parse_provider_ranking(
    *,
    provider: str,
    html: str,
    source_url: str,
) -> list[dict[str, Any]]:
    if provider not in PROVIDER_RULES:
        raise ValueError(f"unsupported trend provider: {provider}")
    parser = RankingParser(
        provider=provider,
        source_url=source_url,
        rule=PROVIDER_RULES[provider],
    )
    parser.feed(html)
    parser.close()
    return parser.items


def _nodes_with_class(root: _Node, class_name: str) -> list[_Node]:
    return [node for node in root.descendants() if class_name in node.classes]


def _first_with_class(
    root: _Node,
    class_name: str,
    *,
    tag: str | None = None,
) -> _Node | None:
    return next(
        (
            node
            for node in root.descendants()
            if class_name in node.classes and (tag is None or node.tag == tag)
        ),
        None,
    )


def _first_tag(root: _Node | None, tag: str) -> _Node | None:
    if root is None:
        return None
    return next((node for node in root.descendants() if node.tag == tag), None)


def _first_integer(value: str) -> int | None:
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _safe_metadata_attributes(
    attrs: dict[str, str | None],
) -> dict[str, str | None]:
    return {
        name: value
        for name, value in attrs.items()
        if name in _SAFE_METADATA_ATTRIBUTES
        and not any(part in name.lower() for part in _SENSITIVE_ATTRIBUTE_PARTS)
    }


def _safe_url(source_url: str, href: str) -> str:
    parsed = urlsplit(urljoin(source_url, href))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
