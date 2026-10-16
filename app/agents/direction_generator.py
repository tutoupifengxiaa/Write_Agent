from __future__ import annotations

import json
from typing import Any


def render_direction_generation_prompt(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "Generate a commercial web-fiction kickoff package from a selected trend direction.",
            "instructions": [
                "Return JSON only.",
                "Keep the result platform-aware and readable for an operator.",
                "Prefer strong hooks, clear audience fit, and serializable plot momentum.",
            ],
            "input": payload,
            "output_schema": {
                "direction_name": "string",
                "target_platforms": ["string"],
                "core_hook": "string",
                "audience": "string",
                "title_candidates": ["string"],
                "logline": "string",
                "selling_points": ["string"],
                "risk_flags": ["string"],
                "outline": {
                    "acts": ["string"],
                },
                "opening_scene": {
                    "location": "string",
                    "conflict": "string",
                    "hook": "string",
                },
                "chapter_plan": [
                    {"chapter": 1, "goal": "string"},
                ],
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def generate_direction_package(client, payload: dict[str, Any]) -> dict:
    prompt = render_direction_generation_prompt(payload)
    return client.generate_json(prompt)
