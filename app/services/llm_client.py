from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import secret_value


class LLMConfigurationError(ValueError):
    pass


def _validate_configuration(api_key: str, base_url: str, model: str, model_field: str) -> None:
    for field, value in (("OPENAI_API_KEY", api_key), ("OPENAI_BASE_URL", base_url), (model_field, model)):
        if not value or not value.strip():
            raise LLMConfigurationError(f"{field} is required")


def validate_worker_settings(settings: Any) -> None:
    api_key = secret_value(getattr(settings, "openai_api_key", ""))
    base_url = getattr(settings, "openai_base_url", "")
    _validate_configuration(api_key, base_url, getattr(settings, "openai_model", ""), "OPENAI_MODEL")
    _validate_configuration(api_key, base_url, getattr(settings, "openai_review_model", ""), "OPENAI_REVIEW_MODEL")


def _retry_after(error: httpx.HTTPError) -> float | None:
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code != 429:
        return None
    value = error.response.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdigit():
        return min(float(value), 30.0)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(max(date.timestamp() - time.time(), 0.0), 30.0)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class LLMClient(ABC):
    def close(self) -> None:  # noqa: B027 - Clients without owned resources can inherit this no-op.
        pass

    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def stream_text(self, prompt: str, *, reasoning_effort: str | None = None):
        raise NotImplementedError


class OpenAIResponsesLLMClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com",
        reasoning_effort: str = "",
        fallback_model: str = "",
        fallback_reasoning_effort: str = "",
        store_response: bool = False,
        timeout_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self.fallback_model = fallback_model
        self.fallback_reasoning_effort = fallback_reasoning_effort
        self.store_response = store_response
        self._stream_timeout = httpx.Timeout(connect=10, read=timeout_seconds, write=30, pool=10)
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._max_retries = 2

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    @classmethod
    def from_settings(cls, settings: Any, *, review: bool = False) -> "OpenAIResponsesLLMClient":
        model = (
            getattr(settings, "openai_review_model", "")
            if review
            else getattr(settings, "openai_model", "")
        )
        return cls(
            api_key=secret_value(getattr(settings, "openai_api_key", "")),
            model=model,
            base_url=getattr(settings, "openai_base_url", "https://api.openai.com"),
            reasoning_effort=getattr(settings, "openai_reasoning_effort", ""),
            fallback_model=getattr(settings, "openai_fallback_model", ""),
            fallback_reasoning_effort=getattr(settings, "openai_fallback_reasoning_effort", ""),
            store_response=getattr(settings, "openai_store_response", False),
            timeout_seconds=getattr(settings, "openai_timeout_seconds", 60.0),
        )

    def validate_configuration(self) -> None:
        _validate_configuration(self.api_key, self.base_url, self.model, "OPENAI_MODEL")

    def generate_json(self, prompt: str) -> dict:
        self.validate_configuration()
        return next(self._request_with_retry(prompt, reasoning_effort=self.reasoning_effort, stream=False))

    def stream_text(self, prompt: str, *, reasoning_effort: str | None = None):
        self.validate_configuration()
        effort = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        yield from self._request_with_retry(prompt, reasoning_effort=effort, stream=True)

    def _request_with_retry(self, prompt: str, *, reasoning_effort: str, stream: bool):
        models = [(self.model, reasoning_effort)]
        fallback = (self.fallback_model, self._resolve_fallback_reasoning_effort())
        if self.fallback_model and fallback != models[0]:
            models.append(fallback)

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/responses"
        for index, (model, effort) in enumerate(models):
            payload = self._build_payload(prompt, model=model, reasoning_effort=effort, stream=stream)
            for attempt in range(self._max_retries + 1):
                emitted = False
                try:
                    if stream:
                        with self._client.stream(
                            "POST", url, headers=headers, json=payload, timeout=self._stream_timeout
                        ) as response:
                            response.raise_for_status()
                            for event in self._iter_sse_events(response):
                                if event.get("type") == "response.output_text.delta" and event.get("delta"):
                                    emitted = True
                                    yield event["delta"]
                    else:
                        response = self._client.post(url, headers=headers, json=payload)
                        response.raise_for_status()
                        yield self._parse_json_response(response.json())
                    return
                except (
                    httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.HTTPStatusError
                ) as exc:
                    if emitted:
                        raise
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                        raise
                    last_attempt = attempt == self._max_retries
                    if last_attempt and index == len(models) - 1:
                        raise
                    delay = _retry_after(exc)
                    if delay is not None or not last_attempt:
                        time.sleep(delay if delay is not None else 0.6 * (attempt + 1))

    def _build_payload(
        self,
        prompt: str,
        *,
        model: str,
        reasoning_effort: str,
        stream: bool = False,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "store": self.store_response,
            "text": {"format": {"type": "json_object"}},
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if stream:
            payload["stream"] = True
        return payload

    def _resolve_fallback_reasoning_effort(self) -> str:
        if self.fallback_reasoning_effort:
            return self.fallback_reasoning_effort
        return self.reasoning_effort

    def _parse_json_response(self, body: dict) -> dict:
        text = self._extract_output_text(body)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM response did not contain valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
        return parsed

    def _extract_output_text(self, body: dict) -> str:
        output_text = body.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return content["text"]

        raise ValueError("LLM response missing output text")

    def _iter_sse_events(self, response: httpx.Response):
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    try:
                        yield json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        pass
                data_lines = []
                continue
            if line == ":" or line.startswith(":"):
                continue
            if line.startswith("data: "):
                data_lines.append(line[6:])
        if data_lines:
            try:
                yield json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                return


def create_llm_clients_from_settings(settings: Any) -> dict[str, OpenAIResponsesLLMClient]:
    return {
        "writer": OpenAIResponsesLLMClient.from_settings(settings),
        "reviewer": OpenAIResponsesLLMClient.from_settings(settings, review=True),
    }
