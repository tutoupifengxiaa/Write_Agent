import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, secret_value
from app.services.llm_client import (
    LLMConfigurationError,
    OpenAIResponsesLLMClient,
    create_llm_clients_from_settings,
    validate_worker_settings,
)


def test_settings_expose_openai_compatible_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.openai_base_url == "https://api.openai.com"
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_review_model == "gpt-5.5"
    assert settings.openai_reasoning_effort == "xhigh"
    assert settings.openai_stream_reasoning_effort == "medium"
    assert settings.openai_fallback_model == ""
    assert settings.openai_fallback_reasoning_effort == ""
    assert settings.openai_store_response is False


def test_worker_and_provider_read_the_secret_from_settings(monkeypatch) -> None:
    placeholder = "YOUR_API_KEY_HERE"
    settings = Settings(
        _env_file=None, openai_api_key=placeholder, openai_base_url="https://provider.example",
        openai_model="writer", openai_review_model="reviewer",
    )
    validate_worker_settings(settings)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {placeholder}"
        return httpx.Response(200, json={"output_text": "{}"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: transport)
        client = OpenAIResponsesLLMClient.from_settings(settings)
        assert client.generate_json("prompt") == {}


def test_responses_client_sends_prompt_to_configured_provider() -> None:
    captured_request = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "output_text": '{"content":"chapter draft","summary":"short"}',
            },
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        reasoning_effort="xhigh",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.generate_json("Write chapter one")

    assert result == {"content": "chapter draft", "summary": "short"}
    assert captured_request is not None
    assert captured_request.method == "POST"
    assert str(captured_request.url) == "https://wuzuapi.com/responses"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert json.loads(captured_request.read()) == {
        "model": "gpt-5.5",
        "input": "Write chapter one",
        "store": False,
        "text": {"format": {"type": "json_object"}},
        "reasoning": {"effort": "xhigh"},
    }


def test_responses_client_supports_nested_output_text_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"approved":true,"review_score":93}',
                            },
                        ],
                    },
                ],
            },
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com/",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.generate_json("Review") == {"approved": True, "review_score": 93}


def test_responses_client_retries_once_when_upstream_temporarily_fails() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, request=request, json={"error": "bad gateway"})
        return httpx.Response(
            200,
            json={
                "output_text": '{"content":"chapter draft","summary":"short"}',
            },
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.generate_json("Write chapter one")

    assert attempts == 2
    assert result == {"content": "chapter draft", "summary": "short"}


def test_responses_client_raises_after_retry_budget_is_exhausted() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502, request=request, json={"error": "bad gateway"})

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.generate_json("Write chapter one")

    assert attempts == 3


def test_responses_client_switches_to_fallback_model_after_primary_5xx() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        requests.append(payload)
        if payload["model"] == "primary-model":
            return httpx.Response(502, request=request, json={"error": "bad gateway"})
        return httpx.Response(
            200,
            request=request,
            json={
                "output_text": '{"content":"fallback draft","summary":"fallback short"}',
            },
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="primary-model",
        base_url="https://wuzuapi.com",
        reasoning_effort="xhigh",
        fallback_model="fallback-model",
        fallback_reasoning_effort="medium",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.generate_json("Write chapter one")

    assert result == {"content": "fallback draft", "summary": "fallback short"}
    assert [payload["model"] for payload in requests] == [
        "primary-model",
        "primary-model",
        "primary-model",
        "fallback-model",
    ]
    assert requests[-1]["reasoning"] == {"effort": "medium"}


def test_responses_client_retries_after_read_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("The read operation timed out")
        return httpx.Response(
            200,
            request=request,
            json={
                "output_text": '{"content":"chapter draft","summary":"short"}',
            },
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.generate_json("Write chapter one")

    assert attempts == 2
    assert result == {"content": "chapter draft", "summary": "short"}


def test_responses_client_streams_output_text_deltas() -> None:
    stream_lines = [
        'event: response.created\n',
        'data: {"type":"response.created"}\n',
        '\n',
        'event: response.output_text.delta\n',
        'data: {"type":"response.output_text.delta","delta":"第一段"}\n',
        '\n',
        'event: response.output_text.delta\n',
        'data: {"type":"response.output_text.delta","delta":"第二段"}\n',
        '\n',
        'event: response.completed\n',
        'data: {"type":"response.completed"}\n',
        '\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["reasoning"] == {"effort": "medium"}
        assert payload["stream"] is True
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text="".join(stream_lines),
        )

    client = OpenAIResponsesLLMClient(
        api_key="test-key",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    events = list(client.stream_text("Write chapter one", reasoning_effort="medium"))

    assert events == ["第一段", "第二段"]


def test_stream_request_uses_configured_read_timeout() -> None:
    timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, text='data: {"type":"response.completed"}\n\n')

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIResponsesLLMClient(
            api_key="YOUR_API_KEY_HERE",
            model="writer-model",
            base_url="https://provider.example",
            timeout_seconds=17.5,
            http_client=http_client,
        )
        list(client.stream_text("Write"))

    assert timeouts == [{"connect": 10, "read": 17.5, "write": 30, "pool": 10}]


def test_responses_client_raises_clear_error_when_key_missing() -> None:
    client = OpenAIResponsesLLMClient(
        api_key="",
        model="gpt-5.5",
        base_url="https://wuzuapi.com",
        http_client=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        client.generate_json("Write")


def test_create_writer_and_reviewer_clients_from_settings() -> None:
    settings = SimpleNamespace(
        openai_api_key="test-key",
        openai_model="writer-model",
        openai_review_model="review-model",
        openai_base_url="https://wuzuapi.com",
        openai_reasoning_effort="xhigh",
        openai_stream_reasoning_effort="medium",
        openai_fallback_model="fallback-model",
        openai_fallback_reasoning_effort="medium",
        openai_store_response=False,
        openai_timeout_seconds=120.0,
    )

    writer = OpenAIResponsesLLMClient.from_settings(settings)
    reviewer = OpenAIResponsesLLMClient.from_settings(settings, review=True)

    assert writer.model == "writer-model"
    assert reviewer.model == "review-model"
    assert writer.base_url == "https://wuzuapi.com"
    assert reviewer.base_url == "https://wuzuapi.com"
    assert writer.fallback_model == "fallback-model"
    assert writer.fallback_reasoning_effort == "medium"


def test_create_llm_clients_from_settings_returns_writer_and_reviewer() -> None:
    settings = SimpleNamespace(
        openai_api_key="test-key",
        openai_model="writer-model",
        openai_review_model="review-model",
        openai_base_url="https://wuzuapi.com",
        openai_reasoning_effort="xhigh",
        openai_stream_reasoning_effort="medium",
        openai_fallback_model="fallback-model",
        openai_fallback_reasoning_effort="medium",
        openai_store_response=False,
        openai_timeout_seconds=120.0,
    )

    clients = create_llm_clients_from_settings(settings)

    assert set(clients) == {"writer", "reviewer"}
    assert clients["writer"].model == "writer-model"
    assert clients["reviewer"].model == "review-model"


@pytest.mark.parametrize("failure", ["rate_limit", "server_error", "connect", "protocol"])
@pytest.mark.parametrize("stream", [False, True])
def test_transient_failures_retry_then_fall_back(monkeypatch, failure, stream):
    sleeps = []
    models = []
    monkeypatch.setattr("app.services.llm_client.time.sleep", sleeps.append)

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "primary":
            if failure == "rate_limit":
                return httpx.Response(429, headers={"Retry-After": "45"})
            if failure == "server_error":
                return httpx.Response(500)
            if failure == "connect":
                raise httpx.ConnectError("offline", request=request)
            raise httpx.RemoteProtocolError("disconnected", request=request)
        if stream:
            return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"body"}\n\n')
        return httpx.Response(200, json={"output_text": '{"content":"body"}'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = OpenAIResponsesLLMClient(api_key="YOUR_API_KEY_HERE", model="primary",
                                          fallback_model="secondary", http_client=transport)
        if stream:
            assert list(client.stream_text("prompt")) == ["body"]
        else:
            assert client.generate_json("prompt") == {"content": "body"}
    assert models == ["primary", "primary", "primary", "secondary"]
    assert sleeps == ([30, 30, 30] if failure == "rate_limit" else [0.6, 1.2])


def test_rate_limit_retries_after_http_date(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.services.llm_client.time.time", lambda: 0)
    monkeypatch.setattr("app.services.llm_client.time.sleep", sleeps.append)
    replies = iter([
        httpx.Response(429, headers={"Retry-After": "Thu, 01 Jan 1970 00:00:07 GMT"}),
        httpx.Response(200, json={"output_text": "{}"}),
    ])
    with httpx.Client(transport=httpx.MockTransport(lambda request: next(replies))) as transport:
        client = OpenAIResponsesLLMClient(api_key="YOUR_API_KEY_HERE", model="primary", http_client=transport)
        assert client.generate_json("prompt") == {}
    assert sleeps == [7]


def test_stream_does_not_retry_after_first_delta():
    attempts = []

    class InterruptedBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"first"}\n\n'
            raise httpx.RemoteProtocolError("interrupted")

    def handler(request):
        attempts.append(1)
        return httpx.Response(200, stream=InterruptedBody())

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = OpenAIResponsesLLMClient(api_key="YOUR_API_KEY_HERE", model="primary",
                                          fallback_model="secondary", http_client=transport)
        deltas = client.stream_text("prompt")
        assert next(deltas) == "first"
        with pytest.raises(httpx.RemoteProtocolError):
            next(deltas)
    assert len(attempts) == 1


@pytest.mark.parametrize("field", ["api_key", "base_url", "model", "review_model"])
def test_missing_model_configuration_is_typed_before_requests(field):
    settings = SimpleNamespace(openai_api_key="YOUR_API_KEY_HERE", openai_base_url="https://provider.example",
                               openai_model="primary", openai_review_model="reviewer")
    setattr(settings, f"openai_{field}", " ")
    with pytest.raises(LLMConfigurationError, match=f"OPENAI_{field.upper()} is required"):
        validate_worker_settings(settings)

    if field != "review_model":
        with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as transport:
            client = OpenAIResponsesLLMClient(api_key=secret_value(settings.openai_api_key), base_url=settings.openai_base_url,
                                              model=settings.openai_model, http_client=transport)
            with pytest.raises(LLMConfigurationError):
                client.generate_json("prompt")
            with pytest.raises(LLMConfigurationError):
                list(client.stream_text("prompt"))
