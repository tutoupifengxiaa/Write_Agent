from __future__ import annotations

import json
import re
from typing import Iterable

from app.agents.state_deltas import STATE_DELTA_RULE, STATE_DELTA_SCHEMA
from app.prompts.loader import load_prompt_template
from app.services.llm_client import LLMClient


def render_writer_prompt(context: dict) -> str:
    chapter_goal = context.get("chapter_goal", {})
    payload = {
        "task": load_prompt_template("writer"),
        "output_schema": {
            "title": "chapter title string",
            "content": "chapter body string",
            "summary": "short chapter summary",
            "state_deltas": [STATE_DELTA_SCHEMA],
        },
        "requirements": {
            "state_deltas": STATE_DELTA_RULE,
            "language": "Simplified Chinese",
            "return_json_only": True,
            "no_markdown": True,
            "no_code_fence": True,
            "keep_paragraphs_readable": True,
        },
        "context": {
            "chapter_title": context.get("chapter_title", ""),
            "objective": chapter_goal.get("objective") or chapter_goal.get("chapter_goal", ""),
            "must_include": chapter_goal.get("must_include", []),
            "must_avoid": chapter_goal.get("must_avoid", []),
            "style_rules": chapter_goal.get("style_rules", ""),
            "target_words": chapter_goal.get("target_words", 2500),
            "world_state": context.get("world_state", {}),
            "character_states": context.get("character_states", []),
            "recent_summaries": context.get("recent_summaries", []),
            "retrieval_evidence": context.get("retrieval_evidence", []),
        },
        "quality_bar": [
            "plot continuity",
            "character consistency",
            "avoid repeated exposition",
            "roughly match target length",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_chapter(client: LLMClient, context: dict) -> dict:
    prompt = render_writer_prompt(context)
    return client.generate_json(prompt)


# Inside a JSON string, plain text can be emitted as one span per provider chunk.
_STRING_TEXT = re.compile(r'[^"\\\x00-\x1f]+')


class _ChapterFormatError(ValueError):
    pass


def _string_escape(text: str, start: int) -> tuple[str, int] | None:
    """Decode one escape, keeping a split escape or surrogate pair for the next chunk."""
    end = start + (6 if text[start + 1:start + 2] == "u" else 2)
    if len(text) < end:
        return None
    value = json.loads('"' + text[start:end] + '"')
    if 0xD800 <= ord(value) <= 0xDBFF:
        if len(text) < end + 6:
            return None
        value = json.loads('"' + text[start:end + 6] + '"')
        end += 6
    if len(value) != 1 or 0xD800 <= ord(value) <= 0xDFFF:
        raise _ChapterFormatError("invalid Unicode escape")
    return value, end


def _chapter_tokens(chunks: Iterable[str]):
    quoted = False
    pending = ""
    for chunk in chunks:
        text = pending + chunk
        pending = ""
        index = 0
        while index < len(text):
            char = text[index]
            if char == '"':
                quoted = not quoted
                yield "open_string" if quoted else "close_string", ""
            elif not quoted:
                if char in "{}[],:":
                    yield char, ""
            elif char == "\\":
                escape = _string_escape(text, index)
                if escape is None:
                    pending = text[index:]
                    break
                value, index = escape
                yield "text", value
                continue
            else:
                span = _STRING_TEXT.match(text, index)
                if span is None:
                    raise _ChapterFormatError("invalid JSON string")
                index = span.end()
                yield "text", span.group()
                continue
            index += 1


def _chapter_text_events(chunks: Iterable[str]):
    depth = 0
    field = ""
    reading_key = False
    want_key = False
    parts = []
    seen = set()
    for token, value in _chapter_tokens(chunks):
        if token in ("{", "["):
            depth += 1
            if depth == 1:
                if token != "{":
                    raise _ChapterFormatError("expected a chapter object")
                want_key = True
        elif token in ("}", "]"):
            depth -= 1
        elif depth == 1:
            if token == ",":
                want_key = True
                field = ""
            elif token == "open_string":
                reading_key = want_key
                parts.clear()
            elif token == "text":
                if reading_key or field == "title":
                    parts.append(value)
                elif field == "content":
                    yield {"event": "delta", "data": {"value": value}}
            elif token == "close_string":
                if reading_key:
                    field = "".join(parts)
                    if field in ("title", "content") and field in seen:
                        raise _ChapterFormatError("duplicate chapter field")
                    seen.add(field)
                    want_key = reading_key = False
                elif field == "title":
                    yield {"event": "title", "data": {"value": "".join(parts)}}


def stream_write_chapter(
    client: LLMClient,
    context: dict,
    *,
    reasoning_effort: str | None = None,
) -> Iterable[dict]:
    prompt = render_writer_prompt(context)
    raw = []

    def chunks():
        for chunk in client.stream_text(prompt, reasoning_effort=reasoning_effort):
            raw.append(chunk)
            yield chunk

    try:
        yield from _chapter_text_events(chunks())
        chapter = json.loads("".join(raw))
        if not isinstance(chapter, dict) or not isinstance(chapter.get("content"), str):
            raise _ChapterFormatError("invalid chapter content")
        summary = chapter.get("summary", "")
        changes = chapter.get("state_deltas", [])
        if not isinstance(chapter.get("title", ""), str) or not isinstance(summary, str) or not isinstance(changes, list):
            raise _ChapterFormatError("invalid chapter fields")
    except (json.JSONDecodeError, _ChapterFormatError):
        yield {"event": "error", "data": {"detail": "模型返回的章节数据格式无效", "status_code": 502}}
        return
    yield {"event": "summary", "data": {"value": summary}}
    yield {"event": "done", "data": {"ok": True, "state_deltas": changes}}
