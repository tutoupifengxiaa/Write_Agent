import pytest
from pydantic import SecretStr

import app.services.embedding as embedding_module
from app.agents.reviewer import render_reviewer_prompt
from app.agents.reviser import render_reviser_prompt
from app.agents.writer import render_writer_prompt
from app.services.embedding import (
    EmbeddingClient,
    KeywordEmbeddingClient,
    NullEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
    cosine_similarity,
)


def test_writer_prompt_loads_template_content():
    prompt = render_writer_prompt(
        {
            "chapter_title": "Chapter Two",
            "chapter_goal": {"objective": "push the conflict"},
            "world_state": {},
            "character_states": [],
            "recent_summaries": [],
        }
    )

    assert "Return a JSON object only." in prompt
    assert "push the conflict" in prompt


def test_reviewer_prompt_loads_template_content():
    prompt = render_reviewer_prompt(
        "chapter body",
        {"chapter_goal": {"objective": "solve the case"}},
    )

    assert "Review a chapter draft for continuity" in prompt
    assert "chapter body" in prompt
    assert "solve the case" in prompt
    assert "revision_priorities" in prompt


def test_reviser_prompt_loads_template_content():
    prompt = render_reviser_prompt(
        "chapter body",
        {"issues": [{"type": "continuity", "message": "fix tone drift"}]},
    )

    assert "Revise a chapter draft" in prompt
    assert "fix tone drift" in prompt
    assert "change_summary" in prompt


def test_null_embedding_client_returns_empty_vectors():
    client: EmbeddingClient = NullEmbeddingClient()

    result = client.embed_texts(["one", "two"])

    assert result == [[], []]


def test_keyword_embedding_client_counts_token_matches():
    client: EmbeddingClient = KeywordEmbeddingClient()

    result = client.embed_texts(["find the clue", "quiet rest"])

    assert len(result) == 2
    assert len(result[0]) == 16
    assert cosine_similarity(result[0], result[0]) == 1.0


def test_keyword_embedding_client_ranks_related_texts_higher_than_unrelated_texts():
    client = KeywordEmbeddingClient()

    vectors = client.embed_texts(
        [
            "find the clue in the warehouse",
            "the hero finds a clue before dawn",
            "the hero takes a quiet rest at home",
        ]
    )

    related_score = cosine_similarity(vectors[0], vectors[1])
    unrelated_score = cosine_similarity(vectors[0], vectors[2])

    assert related_score > unrelated_score


def test_openai_embedding_client_returns_ordered_float_vectors():
    requests = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"index": 1, "embedding": [0.5, 0.5]},
                    {"index": 0, "embedding": [1, 0]},
                ]
            }

    class FakeHttpClient:
        def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            return FakeResponse()

    client = OpenAICompatibleEmbeddingClient(
        api_key=SecretStr(" test-key "),
        base_url=" https://provider.example/v1/ ",
        model=" embed-model ",
        http_client=FakeHttpClient(),
    )

    assert client.embed_texts([]) == []
    assert client.embed_texts(["first", "second"]) == [[1.0, 0.0], [0.5, 0.5]]
    assert requests == [
        (
            "https://provider.example/v1/embeddings",
            {"Authorization": "Bearer test-key"},
            {"model": "embed-model", "input": ["first", "second"]},
        )
    ]
    assert not hasattr(client, "api_key")


def test_openai_embedding_client_rejects_missing_configuration_and_bad_count():
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": []}

    class FakeHttpClient:
        def post(self, url, *, headers, json):
            return FakeResponse()

    with pytest.raises(ValueError, match="embedding API key is required"):
        OpenAICompatibleEmbeddingClient(
            api_key="",
            base_url="https://provider.example/v1",
            model="embed-model",
        )

    with pytest.raises(ValueError, match="embedding base URL is required"):
        OpenAICompatibleEmbeddingClient(
            api_key="test-key",
            base_url="",
            model="embed-model",
        )

    with pytest.raises(ValueError, match="embedding model is required"):
        OpenAICompatibleEmbeddingClient(
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="   ",
        )

    with pytest.raises(ValueError, match="embedding timeout must be greater than zero"):
        OpenAICompatibleEmbeddingClient(
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="embed-model",
            timeout_seconds=0,
        )

    client = OpenAICompatibleEmbeddingClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="embed-model",
        http_client=FakeHttpClient(),
    )
    with pytest.raises(ValueError, match="embedding response count does not match input count"):
        client.embed_texts(["first"])


@pytest.mark.parametrize(
    ("payload", "texts", "message"),
    [
        ([], ["first"], "embedding response data must be a list"),
        ({"data": {}}, ["first"], "embedding response data must be a list"),
        ({"data": ["bad-row"]}, ["first"], "embedding response row must be an object"),
        (
            {"data": [{"index": True, "embedding": [1]}]},
            ["first"],
            "embedding response index must be an integer",
        ),
        (
            {
                "data": [
                    {"index": 0, "embedding": [1]},
                    {"index": 0, "embedding": [2]},
                ]
            },
            ["first", "second"],
            "embedding response indexes must match input positions",
        ),
        (
            {"data": [{"index": 0, "embedding": []}]},
            ["first"],
            "embedding response vector must be a non-empty numeric list",
        ),
        (
            {"data": [{"index": 0, "embedding": ["bad"]}]},
            ["first"],
            "embedding response vector must be a non-empty numeric list",
        ),
        (
            {
                "data": [
                    {"index": 0, "embedding": [1]},
                    {"index": 1, "embedding": [1, 2]},
                ]
            },
            ["first", "second"],
            "embedding response vector dimensions must match",
        ),
    ],
)
def test_openai_embedding_client_rejects_malformed_responses(payload, texts, message):
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return payload

    class FakeHttpClient:
        def post(self, url, *, headers, json):
            return FakeResponse()

    client = OpenAICompatibleEmbeddingClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="embed-model",
        http_client=FakeHttpClient(),
    )

    with pytest.raises(ValueError, match=message):
        client.embed_texts(texts)


def test_openai_embedding_client_closes_only_owned_http_client(monkeypatch):
    class FakeHttpClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    owned_client = FakeHttpClient()
    injected_client = FakeHttpClient()
    monkeypatch.setattr(
        embedding_module.httpx,
        "Client",
        lambda *, timeout: owned_client,
    )

    with OpenAICompatibleEmbeddingClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="embed-model",
    ) as client:
        assert client is not None
    with OpenAICompatibleEmbeddingClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="embed-model",
        http_client=injected_client,
    ):
        pass

    assert owned_client.closed is True
    assert injected_client.closed is False
