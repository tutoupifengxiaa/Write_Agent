"""Turn a book outline's acts into story arcs that the writing context can follow."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete

from app.db.models import StoryArc

ACT_ARC_TYPE = "act"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _chapter(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _seed_starts(outline: dict, count: int) -> list[int | None]:
    """Spread the outline's seed chapters evenly over the acts, returning each act's first chapter."""
    seeds = sorted({
        number for seed in outline.get("chapter_seed_plan") or []
        if isinstance(seed, dict) and (number := _chapter(seed.get("chapter"))) is not None
    })
    if not seeds:
        return [None] * count
    size = -(-len(seeds) // count)
    starts = [seeds[i * size] if i * size < len(seeds) else None for i in range(count)]
    # Acts left without seed chapters share the previous act's start.
    for index in range(1, count):
        if starts[index] is None:
            starts[index] = starts[index - 1]
    return starts


def build_story_arcs(book_outline: Any) -> list[dict]:
    """Return one arc per act: explicit chapter ranges win, otherwise ranges come from the seed plan.

    Acts may be plain strings (older outlines) or objects with name, goal, chapter_start and chapter_end.
    An act whose range cannot be determined covers the whole book (start 1, open end).
    """
    if not isinstance(book_outline, dict) or not isinstance(book_outline.get("acts"), list):
        return []
    acts = []
    for act in book_outline["acts"]:
        if isinstance(act, dict):
            name, goal = _text(act.get("name")), _text(act.get("goal"))
            start, end = _chapter(act.get("chapter_start")), _chapter(act.get("chapter_end"))
        elif isinstance(act, str):
            name = goal = act.strip()
            start = end = None
        else:
            continue
        if name or goal:
            acts.append({"name": name or goal, "goal": goal or name, "start": start, "end": end, "derived": False})
    if not acts:
        return []

    if any(act["start"] is None for act in acts):
        starts = _seed_starts(book_outline, len(acts))
        for index, act in enumerate(acts):
            if act["start"] is None and starts[index] is not None:
                act["start"], act["derived"] = starts[index], True
        # A derived act ends right before the next act begins; the last one stays open.
        for index, act in enumerate(acts):
            if act["derived"] and act["end"] is None and index + 1 < len(acts):
                following = acts[index + 1]["start"]
                if following is not None and following > act["start"]:
                    act["end"] = following - 1

    arcs = []
    for index, act in enumerate(acts):
        start = act["start"] or 1
        end = act["end"] if act["end"] is not None and act["end"] >= start else None
        arcs.append({
            "arc_type": ACT_ARC_TYPE,
            "chapter_start": start,
            "chapter_end": end,
            "stage_goal": act["goal"],
            "outline": {"name": act["name"], "act_index": index},
        })
    return arcs


def sync_story_arcs(session, book) -> None:
    """Replace the book's act arcs with ones built from its current outline; other arc types are kept."""
    session.execute(delete(StoryArc).where(StoryArc.book_id == book.id, StoryArc.arc_type == ACT_ARC_TYPE))
    for arc in build_story_arcs(book.book_outline):
        session.add(StoryArc(book_id=book.id, **arc))


def arcs_covering(story_arcs: list, chapter_number: int) -> list:
    """Arcs whose range includes the chapter; all arcs when none match, so the model keeps the big picture."""
    covering = [
        arc for arc in story_arcs
        if arc.chapter_start <= chapter_number and (arc.chapter_end is None or arc.chapter_end >= chapter_number)
    ]
    return covering or list(story_arcs)
