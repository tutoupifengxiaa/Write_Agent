import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.writer import stream_write_chapter
from app.application import create_fastapi_app
from app.services.llm_client import LLMConfigurationError


def test_creation_generate_endpoint_calls_writer_with_page_input() -> None:
    captured_context = None

    def fake_writer(context: dict) -> dict:
        nonlocal captured_context
        captured_context = context
        return {
            "title": "第35章 暗线回收",
            "content": "生成后的章节正文",
            "summary": "章节摘要",
            "state_deltas": [{"relationship": "advanced"}],
        }

    app = create_fastapi_app(writer=fake_writer)
    client = TestClient(app)

    response = client.post(
        "/api/creation/generate",
        json={
            "chapter_title": "第35章 暗线回收",
            "chapter_goal": "推进第一次正面交锋",
            "must_include": "主角识破陷阱\n配角财富状态变化",
            "must_avoid": "不要重复解释系统设定",
            "style_rules": "短段落、强节奏",
            "target_words": 2500,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "title": "第35章 暗线回收",
        "content": "生成后的章节正文",
        "summary": "章节摘要",
        "state_deltas": [{"relationship": "advanced"}],
    }
    assert captured_context == {
        "chapter_title": "第35章 暗线回收",
        "chapter_goal": {
            "objective": "推进第一次正面交锋",
            "must_include": ["主角识破陷阱", "配角财富状态变化"],
            "must_avoid": ["不要重复解释系统设定"],
            "target_words": 2500,
            "style_rules": "短段落、强节奏",
        },
        "world_state": {},
        "character_states": [],
        "recent_summaries": [],
    }


def test_creation_generate_endpoint_returns_clear_error_when_writer_missing() -> None:
    app = create_fastapi_app()
    client = TestClient(app)

    response = client.post(
        "/api/creation/generate",
        json={
            "chapter_title": "第1章",
            "chapter_goal": "开篇",
            "must_include": "",
            "must_avoid": "",
            "style_rules": "",
            "target_words": 2000,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "creation writer is not configured"


def test_creation_generate_endpoint_returns_clear_error_when_api_key_missing() -> None:
    def missing_key_writer(context: dict) -> dict:
        raise LLMConfigurationError("OPENAI_API_KEY is required")

    app = create_fastapi_app(writer=missing_key_writer)
    client = TestClient(app)

    response = client.post(
        "/api/creation/generate",
        json={
            "chapter_title": "第1章",
            "chapter_goal": "开篇",
            "must_include": "",
            "must_avoid": "",
            "style_rules": "",
            "target_words": 2000,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "OPENAI_API_KEY is required"


def test_creation_generate_endpoint_returns_clear_error_when_upstream_fails() -> None:
    def failing_writer(context: dict) -> dict:
        request = httpx.Request("POST", "https://wuzuapi.com/responses")
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError(
            "Server error '502 Bad Gateway' for url 'https://wuzuapi.com/responses'",
            request=request,
            response=response,
        )

    app = create_fastapi_app(writer=failing_writer)
    client = TestClient(app)

    response = client.post(
        "/api/creation/generate",
        json={
            "chapter_title": "第1章",
            "chapter_goal": "开篇",
            "must_include": "",
            "must_avoid": "",
            "style_rules": "",
            "target_words": 2000,
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "模型服务暂时不可用：502 Bad Gateway"


def test_creation_generate_endpoint_returns_clear_error_when_upstream_times_out() -> None:
    def timeout_writer(context: dict) -> dict:
        raise httpx.ReadTimeout("The read operation timed out")

    app = create_fastapi_app(writer=timeout_writer)
    client = TestClient(app)

    response = client.post(
        "/api/creation/generate",
        json={
            "chapter_title": "第1章",
            "chapter_goal": "开篇",
            "must_include": "",
            "must_avoid": "",
            "style_rules": "",
            "target_words": 2000,
        },
    )

    assert response.status_code == 504
    assert response.json()["detail"] == "模型服务响应超时，请稍后重试"


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (LLMConfigurationError("OPENAI_MODEL is required"), 503, "OPENAI_MODEL is required"),
        (httpx.ReadTimeout("timeout"), 504, "模型服务响应超时，请稍后重试"),
        (httpx.ConnectError("connection failed"), 502, "模型服务暂时不可用，请稍后重试"),
        (httpx.RemoteProtocolError("connection closed"), 502, "模型服务暂时不可用，请稍后重试"),
        (
            httpx.HTTPStatusError("rate limit", request=httpx.Request("POST", "https://provider.invalid"),
                                  response=httpx.Response(429)),
            502, "模型服务请求失败：429 Too Many Requests",
        ),
    ],
)
def test_generation_errors_have_matching_http_and_sse_details(error, code, detail):
    def failing_writer(context):
        raise error

    app = create_fastapi_app(writer=failing_writer, stream_writer=failing_writer)
    payload = {"chapter_title": "章节", "chapter_goal": "开篇"}
    with TestClient(app) as client:
        direct = client.post("/api/creation/generate", json=payload)
        stream = client.post("/api/creation/generate-stream", json=payload)

    assert (direct.status_code, direct.json()) == (code, {"detail": detail})
    assert stream.status_code == 200
    errors = [json.loads(event.split("data: ", 1)[1]) for event in stream.text.split("\n\n")
              if event.startswith("event: error\n")]
    assert errors == [{"status_code": code, "detail": detail}]
    assert "event: done" not in stream.text


def test_creation_generate_stream_endpoint_emits_sse_events() -> None:
    def fake_writer(context: dict) -> dict:
        return {
            "title": "第35章 暗线回收",
            "content": "第一段。\n\n第二段。",
            "summary": "章节摘要",
            "state_deltas": [{"relationship": "advanced"}],
        }

    app = create_fastapi_app(writer=fake_writer)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/creation/generate-stream",
        json={
            "chapter_title": "第35章 暗线回收",
            "chapter_goal": "推进第一次正面交锋",
            "must_include": "主角识破陷阱\n配角财富状态变化",
            "must_avoid": "不要重复解释系统设定",
            "style_rules": "短段落、强节奏",
            "target_words": 2500,
        },
    ) as response:
        body = "".join(chunk for chunk in response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: status\ndata: {"stage":"started"}' in body
    assert 'event: title\ndata: {"value":"第35章 暗线回收"}' in body
    assert 'event: delta\ndata: {"value":"第一段。\\n\\n"}' in body
    assert 'event: delta\ndata: {"value":"第二段。"}' in body
    assert 'event: summary\ndata: {"value":"章节摘要"}' in body
    assert 'event: done\ndata: {"ok":true,' in body


def test_creation_generate_stream_endpoint_uses_streaming_writer_when_available() -> None:
    def fake_writer(context: dict) -> dict:
        return {
            "title": "同步标题",
            "content": "同步正文",
            "summary": "同步摘要",
            "state_deltas": [],
        }

    def fake_stream_writer(context: dict):
        yield {"event": "title", "data": {"value": "流式标题"}}
        yield {"event": "delta", "data": {"value": "流式第一段"}}
        yield {"event": "delta", "data": {"value": "流式第二段"}}
        yield {"event": "summary", "data": {"value": "流式摘要"}}
        yield {"event": "done", "data": {"ok": True, "state_deltas": []}}

    app = create_fastapi_app(writer=fake_writer)
    app.state.creation_stream_writer = fake_stream_writer
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/creation/generate-stream",
        json={
            "chapter_title": "测试章节",
            "chapter_goal": "测试目标",
            "must_include": "",
            "must_avoid": "",
            "style_rules": "",
            "target_words": 800,
        },
    ) as response:
        body = "".join(chunk for chunk in response.iter_text())

    assert response.status_code == 200
    assert 'event: title\ndata: {"value":"流式标题"}' in body
    assert 'event: delta\ndata: {"value":"流式第一段"}' in body
    assert 'event: summary\ndata: {"value":"流式摘要"}' in body


def _stream_events(stream_writer):
    app = create_fastapi_app(stream_writer=stream_writer)
    with TestClient(app) as client:
        response = client.post("/api/creation/generate-stream", json={"chapter_title": "题", "chapter_goal": "目标"})
    assert response.status_code == 200
    events = []
    for block in response.text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_model_json_fragments_reach_stream_endpoint_as_decoded_chapter_events():
    chapter = {
        "title": "真标题", "content": '人物说："走吧"\n第二段 😀',
        "summary": "已动身", "state_deltas": [{"type": "location", "change": "离开"}],
    }

    class FakeModel:
        def stream_text(self, prompt, *, reasoning_effort):
            yield from json.dumps(chapter, ensure_ascii=True)

    events = _stream_events(lambda context: stream_write_chapter(FakeModel(), context))
    assert "".join(data["value"] for name, data in events if name == "delta") == chapter["content"]
    assert ("title", {"value": chapter["title"]}) in events
    assert ("summary", {"value": chapter["summary"]}) in events
    assert events[-1] == ("done", {"ok": True, "state_deltas": chapter["state_deltas"]})


@pytest.mark.parametrize("explicit_error", [False, True])
def test_incomplete_stream_never_finishes_successfully(explicit_error):
    def interrupted_writer(context):
        yield {"event": "delta", "data": {"value": "未完成"}}
        if explicit_error:
            yield {"event": "error", "data": {"detail": "解析失败", "status_code": 502}}
            raise AssertionError("Stream continued after its error event")

    events = _stream_events(interrupted_writer)
    assert events[-1][0] == "error"
    assert all(name != "done" for name, _ in events)
