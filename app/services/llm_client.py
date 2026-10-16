from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx


class LLMClient(ABC):
    def close(self) -> None:
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
            api_key=getattr(settings, "openai_api_key", ""),
            model=model,
            base_url=getattr(settings, "openai_base_url", "https://api.openai.com"),
            reasoning_effort=getattr(settings, "openai_reasoning_effort", ""),
            fallback_model=getattr(settings, "openai_fallback_model", ""),
            fallback_reasoning_effort=getattr(settings, "openai_fallback_reasoning_effort", ""),
            store_response=getattr(settings, "openai_store_response", False),
            timeout_seconds=getattr(settings, "openai_timeout_seconds", 60.0),
        )

    def generate_json(self, prompt: str) -> dict:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

        response = self._post_with_retry(
            prompt,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
        )
        return self._parse_json_response(response.json())

    def stream_text(self, prompt: str, *, reasoning_effort: str | None = None):
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

        with self._client.stream(
            "POST",
            f"{self.base_url}/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=self._build_payload(
                prompt,
                model=self.model,
                reasoning_effort=reasoning_effort if reasoning_effort is not None else self.reasoning_effort,
                stream=True,
            ),
            timeout=None,
        ) as response:
            response.raise_for_status()
            for event in self._iter_sse_events(response):
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta")
                    if delta:
                        yield delta

    def _post_with_retry(
        self,
        prompt: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> httpx.Response:
        last_error: httpx.HTTPStatusError | None = None
        last_timeout: httpx.ReadTimeout | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._build_payload(
                        prompt,
                        model=model,
                        reasoning_effort=reasoning_effort,
                    ),
                )
                response.raise_for_status()
                return response
            except httpx.ReadTimeout as exc:
                if attempt >= self._max_retries:
                    last_timeout = exc
                    break
                last_timeout = exc
                time.sleep(0.6 * (attempt + 1))
            except httpx.HTTPStatusError as exc:
                if not self._should_retry(exc.response.status_code) or attempt >= self._max_retries:
                    last_error = exc
                    break
                last_error = exc
                time.sleep(0.6 * (attempt + 1))
        if self._should_use_fallback(last_error, last_timeout):
            fallback_reasoning_effort = self._resolve_fallback_reasoning_effort()
            if model != self.fallback_model or reasoning_effort != fallback_reasoning_effort:
                return self._post_with_retry(
                    prompt,
                    model=self.fallback_model,
                    reasoning_effort=fallback_reasoning_effort,
                )
        if last_error is not None:
            raise last_error
        if last_timeout is not None:
            raise last_timeout
        raise RuntimeError("unreachable")

    def _should_retry(self, status_code: int) -> bool:
        return status_code in {502, 503, 504}

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

    def _should_use_fallback(
        self,
        error: httpx.HTTPStatusError | None,
        timeout_error: httpx.ReadTimeout | None = None,
    ) -> bool:
        if not self.fallback_model:
            return False
        if timeout_error is not None:
            return True
        if error is None:
            return False
        return self._should_retry(error.response.status_code)

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
        event_name = ""
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    try:
                        yield json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        pass
                event_name = ""
                data_lines = []
                continue
            if line == ":" or line.startswith(":"):
                continue
            if line.startswith("event: "):
                event_name = line[7:]
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
