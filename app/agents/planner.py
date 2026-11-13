def plan_next_chapter(
    *,
    chapter_number: int,
    chapter_goal: str,
    must_include: list[str],
    must_avoid: list[str],
    target_words: int,
    story_arcs: list[dict] | None = None,
    character_states: list[dict] | None = None,
) -> dict:
    story_arc_goals = [
        arc.get("stage_goal", "")
        for arc in (story_arcs or [])
        if arc.get("stage_goal")
    ]
    character_state_hints = []
    for character in character_states or []:
        name = character.get("name", "")
        mood = (character.get("state") or {}).get("mood", "")
        if name and mood:
            character_state_hints.append(f"{name}:{mood}")

    return {
        "objective": chapter_goal,
        "must_include": must_include,
        "must_avoid": must_avoid,
        "target_words": target_words,
        "chapter_number": chapter_number,
        "context_signals": {
            "story_arc_goals": story_arc_goals,
            "character_state_hints": character_state_hints,
        },
        "execution_beats": list(must_include),
        "continuity_focus": list(must_avoid),
    }
