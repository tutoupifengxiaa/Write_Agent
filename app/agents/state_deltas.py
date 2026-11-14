"""Shared state-delta schema for model prompts, and the rule for spotting character changes."""

from __future__ import annotations

# Prompt description of one state_deltas item. Character changes must carry the character's name in
# `target`, so approved chapters can keep per-character state for later chapters.
STATE_DELTA_SCHEMA = {
    "type": "character|world|item|relationship|plot",
    "target": "for type=character: the character's exact name; otherwise the affected entity",
    "change": "what changed in this chapter (status, location, abilities, injuries, goals, etc.)",
}

STATE_DELTA_RULE = (
    "Record every change to a named character (status, location, abilities, injuries, relationships, goals) "
    "as a separate state_deltas item with type=character and target=the character's exact name."
)

CHARACTER_DELTA_TYPES = {"character", "character_state", "角色", "人物"}


def character_changes(state_deltas: list) -> dict[str, list[str]]:
    """Group character state changes by name, keeping the order they were reported in."""
    changes: dict[str, list[str]] = {}
    for delta in state_deltas or []:
        if not isinstance(delta, dict):
            continue
        if str(delta.get("type") or "").strip().lower() not in CHARACTER_DELTA_TYPES:
            continue
        name = delta.get("target")
        change = delta.get("change")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(change, str):
            change = "" if change is None else str(change)
        if change.strip():
            changes.setdefault(name.strip()[:255], []).append(change.strip())
    return changes
