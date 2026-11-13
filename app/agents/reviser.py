from __future__ import annotations

import json

from app.prompts.loader import load_prompt_template


def render_reviser_prompt(original_content: str, revision_instructions: dict) -> str:
    payload = {
        "task": load_prompt_template("reviser"),
        "instructions": [
            "Return a JSON object only.",
            "Keep the chapter's core plot direction unless revision_instructions explicitly say otherwise.",
            "Resolve listed issues as directly as possible.",
        ],
        "output_schema": {
            "revised_content": "full revised chapter text",
            "summary": "short plot summary of the revised chapter, not revision commentary",
            "resolved_issue_ids": ["issue identifier repaired by this revision"],
            "unresolved_issue_ids": ["issue identifier still unresolved"],
            "change_summary": [
                {
                    "issue_id": "issue identifier",
                    "change": "concrete repair made",
                }
            ],
        },
        "original_content": original_content,
        "revision_instructions": revision_instructions,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def revise_chapter(client, original_content: str, revision_instructions: dict) -> dict:
    prompt = render_reviser_prompt(original_content, revision_instructions)
    return client.generate_json(prompt)
