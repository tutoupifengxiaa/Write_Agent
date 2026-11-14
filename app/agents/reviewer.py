from __future__ import annotations

import json
import math

from app.agents.review_issues import has_blocking_issues, normalize_review_issues
from app.agents.state_deltas import STATE_DELTA_RULE, STATE_DELTA_SCHEMA
from app.prompts.loader import load_prompt_template
from app.services.llm_client import LLMClient


def render_reviewer_prompt(content: str, canonical_facts: dict) -> str:
    payload = {
        "task": load_prompt_template("reviewer"),
        "instructions": [
            "Return a JSON object only.",
            "Check whether the chapter aligns with chapter_goal, story arcs, character state, and recent summaries.",
            "Flag concrete issues instead of giving vague advice.",
            STATE_DELTA_RULE,
        ],
        "output_schema": {
            "approved": "boolean",
            "review_score": "number from 0 to 100",
            "summary": "short review summary",
            "issues": [
                {
                    "type": "continuity|pacing|safety|logic",
                    "message": "specific issue description",
                    "severity": "low|medium|high",
                    "evidence": "specific excerpt or canonical fact supporting the issue",
                }
            ],
            "revision_priorities": ["ordered revision focus"],
            "state_deltas": [STATE_DELTA_SCHEMA],
        },
        "chapter_content": content,
        "canonical_facts": canonical_facts,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def review_chapter(client: LLMClient, content: str, canonical_facts: dict) -> dict:
    prompt = render_reviewer_prompt(content, canonical_facts)
    review = _sanitize_model_review(client.generate_json(prompt))
    return _apply_rule_based_quality_checks(review, content, canonical_facts)


def _sanitize_model_review(review: object) -> dict:
    if not isinstance(review, dict):
        raise ValueError("reviewer output must be an object")

    sanitized = dict(review)
    raw_issues = review.get("issues")
    sanitized["issues"] = (
        [issue for issue in raw_issues if isinstance(issue, dict)]
        if isinstance(raw_issues, list)
        else []
    )

    approved = review.get("approved")
    sanitized["approved"] = approved if isinstance(approved, bool) else False

    raw_revision_priorities = review.get("revision_priorities")
    sanitized["revision_priorities"] = (
        [
            priority.strip()
            for priority in raw_revision_priorities
            if isinstance(priority, str) and priority.strip()
        ]
        if isinstance(raw_revision_priorities, list)
        else []
    )

    try:
        review_score = float(review.get("review_score", 0))
    except (TypeError, ValueError):
        review_score = 0.0
    if not math.isfinite(review_score):
        review_score = 0.0
    sanitized["review_score"] = min(100.0, max(0.0, review_score))
    return sanitized


def _apply_rule_based_quality_checks(review: dict, content: str, canonical_facts: dict) -> dict:
    chapter_goal = canonical_facts.get("chapter_goal") or {}
    issues = list(review.get("issues") or [])
    revision_priorities = list(review.get("revision_priorities") or [])
    review_score = float(review.get("review_score", 0))

    for phrase in dict.fromkeys(chapter_goal.get("must_include") or []):
        if phrase and phrase not in content:
            issues.append(
                {
                    "type": "coverage",
                    "message": f"缺少必须包含要素：{phrase}",
                    "severity": "high",
                    "evidence": f"chapter_goal.must_include 要求包含：{phrase}",
                    "source": "rule",
                }
            )
            if "补齐必需要素" not in revision_priorities:
                revision_priorities.append("补齐必需要素")
            review_score -= 12

    for phrase in dict.fromkeys(chapter_goal.get("must_avoid") or []):
        if phrase and phrase in content:
            issues.append(
                {
                    "type": "constraint",
                    "message": f"命中必须避免内容：{phrase}",
                    "severity": "high",
                    "evidence": f"正文出现禁用短语：{phrase}",
                    "source": "rule",
                }
            )
            if "移除违规或重复表达" not in revision_priorities:
                revision_priorities.append("移除违规或重复表达")
            review_score -= 12

    target_words = int(chapter_goal.get("target_words") or 0)
    if target_words > 0 and len(content) < int(target_words * 0.5):
        issues.append(
            {
                "type": "pacing",
                "message": "正文篇幅明显低于目标字数，情节推进可能过快。",
                "severity": "medium",
                "evidence": f"正文长度为 {len(content)}，目标字数为 {target_words}",
                "source": "rule",
            }
        )
        if "补足正文篇幅" not in revision_priorities:
            revision_priorities.append("补足正文篇幅")
        review_score -= 8

    issues = normalize_review_issues(issues)
    if any(
        issue["severity"] in {"medium", "high"} and not issue["evidence"]
        for issue in issues
    ):
        raise ValueError("blocking review issue missing evidence")
    review["issues"] = issues
    review["revision_priorities"] = list(dict.fromkeys(revision_priorities))
    review["review_score"] = min(100.0, max(0.0, review_score))
    if review["approved"] is False and not issues:
        raise ValueError("reviewer rejected chapter without structured issues")
    if has_blocking_issues(issues):
        review["approved"] = False
    if review["approved"] is False and not any(
        issue["evidence"] and issue["source"] in {"rule", "model"}
        for issue in issues
    ):
        raise ValueError("reviewer rejected chapter without evidenced issue")
    return review
