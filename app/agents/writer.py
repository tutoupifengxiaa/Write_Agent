from __future__ import annotations

import json
from typing import Iterable

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
            "state_deltas": [
                {
                    "type": "state change type",
                    "target": "affected entity",
                    "change": "structured update description",
                }
            ],
        },
        "requirements": {
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


def stream_write_chapter(
    client: LLMClient,
    context: dict,
    *,
    reasoning_effort: str | None = None,
) -> Iterable[dict]:
    prompt = render_writer_prompt(context)
    for delta in client.stream_text(prompt, reasoning_effort=reasoning_effort):
        yield {"event": "delta", "data": {"value": delta}}
