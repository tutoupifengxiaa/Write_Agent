from __future__ import annotations

from abc import ABC, abstractmethod
import math
import re

import httpx
from pydantic import SecretStr

from app.config import secret_value

class EmbeddingClient(ABC):
    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class NullEmbeddingClient(EmbeddingClient):
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class KeywordEmbeddingClient(EmbeddingClient):
    def __init__(self, *, dimensions: int = 16) -> None:
        self.dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _tokenize(text):
                index = _stable_hash(token) % self.dimensions
                vector[index] += 1.0
            vectors.append(vector)
        return vectors


class OpenAICompatibleEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        *,
        api_key: str | SecretStr,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        api_key_value = secret_value(api_key).strip()
        normalized_base_url = base_url.strip().rstrip("/")
        normalized_model = model.strip()

        if not api_key_value:
            raise ValueError("embedding API key is required")
        if not normalized_base_url:
            raise ValueError("embedding base URL is required")
        if not normalized_model:
            raise ValueError("embedding model is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("embedding timeout must be greater than zero")

        self._api_key = SecretStr(api_key_value)
        self.base_url = normalized_base_url
        self.model = normalized_model
        self._owns_http_client = http_client is None
        self._client = (
            http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        response = self._client.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {secret_value(self._api_key)}"
            },
            json={"model": self.model, "input": texts},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("embedding response data must be a list")

        rows = body["data"]
        if len(rows) != len(texts):
            raise ValueError("embedding response count does not match input count")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("embedding response row must be an object")
            if type(row.get("index")) is not int:
                raise ValueError("embedding response index must be an integer")

        if {row["index"] for row in rows} != set(range(len(texts))):
            raise ValueError("embedding response indexes must match input positions")

        vectors: list[list[float]] = []
        for row in sorted(rows, key=lambda item: item["index"]):
            embedding = row.get("embedding")
            if (
                not isinstance(embedding, list)
                or not embedding
                or any(type(value) not in (int, float) for value in embedding)
            ):
                raise ValueError(
                    "embedding response vector must be a non-empty numeric list"
                )
            try:
                vectors.append([float(value) for value in embedding])
            except (OverflowError, ValueError) as exc:
                raise ValueError(
                    "embedding response vector must be a non-empty numeric list"
                ) from exc

        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise ValueError("embedding response vector dimensions must match")

        return vectors

    def close(self) -> None:
        if self._owns_http_client:
            self._client.close()

    def __enter__(self) -> "OpenAICompatibleEmbeddingClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0

    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=False))
    score = dot / (left_norm * right_norm)
    return round(score, 6)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower())


def _stable_hash(token: str) -> int:
    value = 0
    for char in token:
        value = (value * 131 + ord(char)) % 2147483647
    return value
