import json

import pytest

from app.agents.writer import render_writer_prompt, stream_write_chapter


def test_render_writer_prompt_returns_structured_json_prompt() -> None:
    prompt = render_writer_prompt(
        {
            "chapter_title": "Chapter 12 Wind Rises",
            "chapter_goal": {
                "objective": "push the conflict upward",
                "must_include": ["streetlight", "late-night call"],
                "must_avoid": ["repeat exposition"],
                "style_rules": "short sentences, heavy pressure",
                "target_words": 1800,
            },
            "world_state": {"weather": "storm"},
            "character_states": [{"name": "Lin Ce", "mood": "tense"}],
            "recent_summaries": ["the lead discovered a clue"],
        }
    )

    parsed = json.loads(prompt)

    assert "Write a Chinese web-fiction chapter" in parsed["task"]
    assert parsed["requirements"]["return_json_only"] is True
    assert parsed["context"]["chapter_title"] == "Chapter 12 Wind Rises"
    assert parsed["context"]["must_include"] == ["streetlight", "late-night call"]
    assert parsed["context"]["target_words"] == 1800

class StreamingClient:
    def __init__(self, parts):
        self.parts = parts

    def stream_text(self, prompt, *, reasoning_effort):
        yield from self.parts


def test_stream_writer_decodes_split_escapes_and_only_top_level_body():
    changes = [{"type": "move", "target": "lead", "change": "home"}]
    body = 'First\n"paragraph"\\第二段🌊'
    chapter = {
        "extra": {"content": "nested body", "title": "nested title", "list": ["}", ","]},
        "summary": "summary",
        "content": body,
        "title": "Title🌊",
        "state_deltas": changes,
    }
    events = list(stream_write_chapter(StreamingClient(iter(json.dumps(chapter))), {}))
    assert "".join(event["data"]["value"] for event in events if event["event"] == "delta") == body
    assert [event["data"]["value"] for event in events if event["event"] == "title"] == ["Title🌊"]
    assert events[-2:] == [
        {"event": "summary", "data": {"value": "summary"}},
        {"event": "done", "data": {"ok": True, "state_deltas": changes}},
    ]


def test_stream_writer_emits_body_before_provider_finishes():
    consumed = []
    def parts():
        consumed.append(1)
        yield '{"content":"already'
        consumed.append(2)
        yield ' visible","summary":"finished"}'
    events = iter(stream_write_chapter(StreamingClient(parts()), {}))
    assert next(events) == {"event": "delta", "data": {"value": "already"}}
    assert consumed == [1]
    assert list(events)[-1]["event"] == "done"


@pytest.mark.parametrize("raw", [
    '{"content":"incomplete', '{"state_deltas":[]}', '{"content":[]}',
    '{"content":"body","state_deltas":{}}', '{"content":"body","summary":1}',
    '{"content":"first","content":"different"}',
])
def test_stream_writer_invalid_output_is_error_without_done(raw):
    events = list(stream_write_chapter(StreamingClient([raw]), {}))
    assert [event["event"] for event in events if event["event"] in {"error", "done"}] == ["error"]
    assert events[-1]["data"]["status_code"] == 502
