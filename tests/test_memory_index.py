import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Book,
    Chapter,
    ChapterVersion,
    Character,
    CharacterSnapshot,
    ContinuityEvent,
    StoryArc,
)
from app.db.repositories import MemoryEmbeddingRepository
from app.services.embedding import KeywordEmbeddingClient
from app.services.memory_index import (
    collect_memory_sources,
    refresh_memory_index,
    retrieve_memory_evidence,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


class RecordingEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


def test_memory_embedding_upsert_and_book_scope(session):
    session.add_all(
        [
            Book(id="book_a", title="A", platform="fanqie", genre="urban"),
            Book(id="book_b", title="B", platform="fanqie", genre="urban"),
        ]
    )
    session.flush()
    repo = MemoryEmbeddingRepository(session)

    first = repo.upsert(
        book_id="book_a",
        source_type="chapter_summary",
        source_id="version_1",
        source_text="old clue",
        model="fake",
        vector=[1.0, 0.0],
    )
    second = repo.upsert(
        book_id="book_a",
        source_type="chapter_summary",
        source_id="version_1",
        source_text="new clue",
        model="fake",
        vector=[0.0, 1.0],
    )
    repo.upsert(
        book_id="book_b",
        source_type="book_outline",
        source_id="book_b",
        source_text="B",
        model="fake",
        vector=[1.0],
    )
    session.commit()

    assert first.id == second.id
    assert second.source_text == "new clue"
    assert second.vector == [0.0, 1.0]
    assert [item.book_id for item in repo.list_for_book("book_a")] == ["book_a"]
    assert not repo.needs_refresh(
        book_id="book_a",
        source_type="chapter_summary",
        source_id="version_1",
        source_text="new clue",
        model="fake",
    )
    assert repo.needs_refresh(
        book_id="book_a",
        source_type="chapter_summary",
        source_id="version_1",
        source_text="new clue",
        model="new-model",
    )


def test_refresh_is_incremental(session):
    book = Book(
        id="book_a",
        title="A",
        platform="fanqie",
        genre="urban",
        book_outline={"main": "old"},
    )
    arc = StoryArc(
        book_id="book_a",
        arc_type="main",
        chapter_start=1,
        stage_goal="survive",
        outline={"beat": "escape"},
    )
    session.add_all([book, arc])
    session.flush()
    client = RecordingEmbeddingClient()

    first = refresh_memory_index(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        embedding_client=client,
        model_name="fake",
    )
    second = refresh_memory_index(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        embedding_client=client,
        model_name="fake",
    )
    book.book_outline = {"main": "new"}
    third = refresh_memory_index(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        embedding_client=client,
        model_name="fake",
    )

    assert len(first) == len(second) == len(third) == 2
    assert len(client.calls) == 2
    assert len(client.calls[0]) == 2
    assert client.calls[1] == ['{"main": "new"}']


def test_collect_memory_sources_obeys_chapter_boundary_and_book_scope(session):
    session.add_all(
        [
            Book(id="book_a", title="A", platform="fanqie", genre="urban"),
            Book(id="book_b", title="B", platform="fanqie", genre="urban"),
        ]
    )
    chapters = [
        Chapter(book_id="book_a", chapter_number=number, title=str(number))
        for number in (1, 2, 3)
    ]
    other_chapter = Chapter(book_id="book_b", chapter_number=1, title="other")
    session.add_all([*chapters, other_chapter])
    session.flush()
    character = Character(book_id="book_a", name="Hero")
    other_character = Character(book_id="book_b", name="Other")
    session.add_all([character, other_character])
    session.flush()

    for chapter in chapters:
        version = ChapterVersion(chapter_id=chapter.id, source="approved", summary=f"summary-{chapter.chapter_number}")
        session.add(version)
        session.flush()
        chapter.approved_version_id = version.id
        session.add_all(
            [
                CharacterSnapshot(
                    character_id=character.id,
                    chapter_id=chapter.id,
                    state={"chapter": chapter.chapter_number},
                ),
                ContinuityEvent(
                    chapter_id=chapter.id,
                    chapter_version_id=version.id,
                    delta={"chapter": chapter.chapter_number},
                ),
            ]
        )
    session.add_all(
        [
            ChapterVersion(chapter_id=other_chapter.id, summary="other-summary"),
            CharacterSnapshot(
                character_id=other_character.id,
                chapter_id=other_chapter.id,
                state={"other": True},
            ),
            ContinuityEvent(chapter_id=other_chapter.id, delta={"other": True}),
            CharacterSnapshot(
                character_id=character.id,
                chapter_id=other_chapter.id,
                state={"wrong_cross_book_link": True},
            ),
        ]
    )
    session.flush()

    sources = collect_memory_sources(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
    )

    assert {source.source_type for source in sources} == {
        "chapter_summary",
        "character_snapshot",
        "continuity_event",
    }
    assert {source.source_chapter_number for source in sources} == {1}
    assert all(source.book_id == "book_a" for source in sources)
    assert all("other" not in source.text for source in sources)
    assert all("wrong_cross_book_link" not in source.text for source in sources)


def test_refresh_rejects_embedding_count_mismatch(session):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "clue"},
        )
    )
    session.flush()

    class MissingVectorClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return []

    with pytest.raises(
        ValueError,
        match="embedding response count does not match memory source count",
    ):
        refresh_memory_index(
            session=session,
            book_id="book_a",
            target_chapter_number=2,
            embedding_client=MissingVectorClient(),
            model_name="fake",
        )


def test_refresh_rejects_empty_vector_without_persisting_it(session):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "clue"},
        )
    )
    session.flush()

    class EmptyVectorClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[] for _ in texts]

    with pytest.raises(ValueError, match="non-empty numeric list"):
        refresh_memory_index(
            session=session,
            book_id="book_a",
            target_chapter_number=2,
            embedding_client=EmptyVectorClient(),
            model_name="fake",
        )

    assert MemoryEmbeddingRepository(session).list_for_book("book_a") == []


def test_refresh_rejects_mismatched_vector_dimensions_atomically(session):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "clue"},
        )
    )
    session.add(
        StoryArc(
            book_id="book_a",
            arc_type="main",
            chapter_start=1,
            stage_goal="survive",
        )
    )
    session.flush()

    class MismatchedDimensionsClient:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0], [1.0, 2.0]]

    with pytest.raises(ValueError, match="dimensions must match"):
        refresh_memory_index(
            session=session,
            book_id="book_a",
            target_chapter_number=2,
            embedding_client=MismatchedDimensionsClient(),
            model_name="fake",
        )

    assert MemoryEmbeddingRepository(session).list_for_book("book_a") == []


class DeterministicSemanticClient:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail is not None:
            raise RuntimeError(self.fail)
        return [
            [1.0, 0.0] if "warehouse" in text else [0.0, 1.0]
            for text in texts
        ]


class RecordingKeywordClient(KeywordEmbeddingClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return super().embed_texts(texts)


class SourceBatchFailingSemanticClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if texts == ["warehouse search"]:
            return [[1.0, 0.0]]
        raise RuntimeError("provider down")


class QueryFailingSemanticClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if texts == ["warehouse search"]:
            raise RuntimeError("query down")
        return [[1.0, 0.0] for _ in texts]


def _add_summary(
    session,
    *,
    book_id: str,
    chapter_number: int,
    chapter_id: str,
    version_id: str,
    summary: str,
) -> None:
    session.add(
        Chapter(
            id=chapter_id,
            book_id=book_id,
            chapter_number=chapter_number,
            title=str(chapter_number),
            approved_version_id=version_id,
        )
    )
    session.flush()
    session.add(
        ChapterVersion(
            id=version_id,
            source="approved",
            chapter_id=chapter_id,
            summary=summary,
        )
    )


def test_retrieve_memory_evidence_ranks_semantically_with_book_and_chapter_isolation(
    session,
):
    session.add_all(
        [
            Book(id="book_a", title="A", platform="fanqie", genre="urban"),
            Book(id="book_b", title="B", platform="fanqie", genre="urban"),
        ]
    )
    session.flush()
    _add_summary(
        session,
        book_id="book_a",
        chapter_number=1,
        chapter_id="a1",
        version_id="matching_summary",
        summary="warehouse clue",
    )
    _add_summary(
        session,
        book_id="book_a",
        chapter_number=2,
        chapter_id="a2",
        version_id="future_summary",
        summary="warehouse future",
    )
    _add_summary(
        session,
        book_id="book_b",
        chapter_number=1,
        chapter_id="b1",
        version_id="other_book_summary",
        summary="warehouse clue",
    )
    session.flush()
    client = DeterministicSemanticClient()

    result = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse search",
        embedding_client=client,
        model_name="fake-semantic",
        fallback_client=KeywordEmbeddingClient(),
        embedding_fallback_mode="keyword",
        limit=3,
    )

    assert result["mode"] == "semantic"
    assert result["degradation_reason"] is None
    assert [item["source_id"] for item in result["items"]] == ["matching_summary"]
    assert all(item["book_id"] == "book_a" for item in result["items"])
    assert client.calls.count(["warehouse search"]) == 1


def test_list_for_book_filters_exact_source_keys_and_preserves_unfiltered_listing(session):
    repo = MemoryEmbeddingRepository(session)
    for book_id in ("local", "other"):
        session.add(Book(id=book_id, title=book_id, platform="local", genre="fiction"))
        session.flush()
        for source_type, source_id in (
            ("story_arc", "a"), ("story_arc", "b"), ("chapter_summary", "a"), ("chapter_summary", "b")
        ):
            repo.upsert(book_id=book_id, source_type=source_type, source_id=source_id,
                        source_text="clue", model="fake", vector=[1.0])
    keys = [("story_arc", "a"), ("chapter_summary", "b")]
    selected = repo.list_for_book("local", source_keys=keys)
    assert {(row.source_type, row.source_id) for row in selected} == set(keys)
    assert all(row.book_id == "local" for row in selected)
    assert repo.list_for_book("local", source_keys=[]) == []
    assert len(repo.list_for_book("local")) == 4


def test_retrieval_bounds_sql_sources_and_embeddings_before_ranking(session):
    session.add(Book(id="a", title="A", platform="local", genre="fiction", book_outline={"plot": "main"}))
    session.flush()
    session.add(Character(id="hero", book_id="a", name="Hero"))
    for number in (1, 7, 8, 39, 40):
        _add_summary(session, book_id="a", chapter_id=f"c{number}", version_id=f"v{number}",
                     chapter_number=number, summary=f"Summary for chapter {number}")
    session.add_all([
        StoryArc(id="old_arc", book_id="a", chapter_start=1, arc_type="main"),
        CharacterSnapshot(id="old_snapshot", chapter_id="c1", character_id="hero", state={"role": "guide"}),
        CharacterSnapshot(id="future_snapshot", chapter_id="c40", character_id="hero", state={"role": "rival"}),
        ContinuityEvent(id="old_event", chapter_id="c7", chapter_version_id="v7", delta={"old": True}),
    ])
    for number in range(70):
        session.add(ContinuityEvent(id=f"recent_{number}", chapter_id="c39", chapter_version_id="v39",
                                    delta={"clue": number}))
    repo = MemoryEmbeddingRepository(session)
    repo.upsert(book_id="a", source_type="chapter_summary", source_id="deleted_version",
                source_text="stale", model="fake", vector=[1.0, 0.0])
    session.flush()

    queries = []
    def record_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            queries.append(" ".join(statement.lower().split()))

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record_select)
    try:
        result = retrieve_memory_evidence(
            session=session, book_id="a", target_chapter_number=40, query="clue",
            embedding_client=RecordingEmbeddingClient(), model_name="fake", fallback_client=None,
            embedding_fallback_mode="disabled", limit=999,
        )
    finally:
        event.remove(bind, "before_cursor_execute", record_select)
    ids = {item["source_id"] for item in result["items"]}
    assert result["mode"] == "semantic"
    assert {"v8", "v39", "a", "old_arc", "old_snapshot"} <= ids
    assert not {"v1", "v7", "v40", "old_event", "future_snapshot", "deleted_version"} & ids
    assert len([source_id for source_id in ids if source_id.startswith("recent_")]) == 64
    for table in ("chapter_versions", "continuity_events", "story_arcs"):
        reads = [query for query in queries if f" from {table} " in query]
        assert reads and all(" limit " in query for query in reads)
    snapshot_reads = [query for query in queries if " from character_snapshots " in query]
    assert snapshot_reads and all("row_number() over" in query for query in snapshot_reads)
    vector_reads = [query for query in queries if " from memory_embeddings " in query]
    assert vector_reads
    assert all(
        "memory_embeddings.id =" in query or
        ("memory_embeddings.source_id" in query.partition(" where ")[2].partition(" order by ")[0])
        for query in vector_reads
    )
    full_index = refresh_memory_index(
        session=session, book_id="a", target_chapter_number=40,
        embedding_client=RecordingEmbeddingClient(), model_name="fake",
    )
    assert {"v1", "v7", "old_event"} <= {row.source_id for row in full_index}


def test_retrieval_keeps_latest_snapshot_for_every_character(session):
    session.add(Book(id="a", title="A", platform="local", genre="fiction"))
    session.flush()
    session.add_all([
        Character(id="hermit", book_id="a", name="Hermit"),
        Character(id="hero", book_id="a", name="Hero"),
    ])
    for number in range(1, 81):
        session.add(Chapter(id=f"c{number}", book_id="a", chapter_number=number, title=str(number)))
    session.flush()
    session.add_all([
        CharacterSnapshot(id="hermit_old", chapter_id="c1", character_id="hermit", state={"v": 1}),
        CharacterSnapshot(id="hermit_latest", chapter_id="c2", character_id="hermit", state={"v": 2}),
    ])
    for number in range(3, 81):
        session.add(CharacterSnapshot(id=f"hero_{number}", chapter_id=f"c{number}",
                                      character_id="hero", state={"v": number}))
    session.add(CharacterSnapshot(id="hero_future", chapter_id="c80", character_id="hermit", state={"v": 0}))
    session.flush()

    sources = collect_memory_sources(
        session=session, book_id="a", target_chapter_number=80, for_retrieval=True,
    )

    snapshot_ids = {source.source_id for source in sources if source.source_type == "character_snapshot"}
    assert snapshot_ids == {"hermit_latest", "hero_79"}


def test_retrieve_memory_evidence_prefers_nearer_chapter_for_equal_similarity(session):
    session.add(Book(id="book_a", title="A", platform="fanqie", genre="urban"))
    session.flush()
    _add_summary(
        session,
        book_id="book_a",
        chapter_number=1,
        chapter_id="a1",
        version_id="older_summary",
        summary="warehouse clue old",
    )
    _add_summary(
        session,
        book_id="book_a",
        chapter_number=3,
        chapter_id="a3",
        version_id="nearer_summary",
        summary="warehouse clue near",
    )
    session.flush()

    result = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=4,
        query="warehouse clue",
        embedding_client=DeterministicSemanticClient(),
        model_name="fake-semantic",
        fallback_client=None,
        embedding_fallback_mode="disabled",
        limit=2,
    )

    assert [item["source_id"] for item in result["items"]] == [
        "nearer_summary",
        "older_summary",
    ]


def test_retrieve_memory_evidence_falls_back_incrementally_after_provider_failure(
    session,
):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "warehouse clue"},
        )
    )
    session.flush()
    fallback = RecordingKeywordClient()
    failing = SourceBatchFailingSemanticClient()

    first = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse search",
        embedding_client=failing,
        model_name="fake-semantic",
        fallback_client=fallback,
        embedding_fallback_mode="keyword",
        limit=3,
    )
    second = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse search",
        embedding_client=failing,
        model_name="fake-semantic",
        fallback_client=fallback,
        embedding_fallback_mode="keyword",
        limit=3,
    )

    assert first["mode"] == second["mode"] == "keyword"
    assert first["degradation_reason"] == second["degradation_reason"] == "provider down"
    assert fallback.calls == [
        ["warehouse search"],
        ['{"main": "warehouse clue"}'],
        ["warehouse search"],
    ]


def test_query_failure_does_not_write_semantic_sources_before_keyword_fallback(session):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "warehouse clue"},
        )
    )
    session.flush()
    primary = QueryFailingSemanticClient()
    fallback = RecordingKeywordClient()

    results = [
        retrieve_memory_evidence(
            session=session,
            book_id="book_a",
            target_chapter_number=2,
            query="warehouse search",
            embedding_client=primary,
            model_name="fake-semantic",
            fallback_client=fallback,
            embedding_fallback_mode="keyword",
            limit=3,
        )
        for _ in range(2)
    ]

    assert [result["mode"] for result in results] == ["keyword", "keyword"]
    assert [result["degradation_reason"] for result in results] == [
        "query down",
        "query down",
    ]
    assert primary.calls == [["warehouse search"], ["warehouse search"]]
    assert fallback.calls == [
        ["warehouse search"],
        ['{"main": "warehouse clue"}'],
        ["warehouse search"],
    ]


def test_retrieve_refreshes_same_model_rows_when_query_dimensions_change(session):
    session.add(Book(id="book_a", title="A", platform="fanqie", genre="urban"))
    session.flush()
    _add_summary(
        session,
        book_id="book_a",
        chapter_number=1,
        chapter_id="a1",
        version_id="summary_1",
        summary="warehouse clue",
    )
    session.flush()
    MemoryEmbeddingRepository(session).upsert(
        book_id="book_a",
        source_type="chapter_summary",
        source_id="summary_1",
        source_chapter_number=1,
        source_text="warehouse clue",
        model="fake-semantic",
        vector=[1.0, 0.0],
    )

    class ThreeDimensionalClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[1.0, 0.0, 0.0] for _ in texts]

    client = ThreeDimensionalClient()

    result = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse search",
        embedding_client=client,
        model_name="fake-semantic",
        fallback_client=None,
        embedding_fallback_mode="disabled",
        limit=3,
    )

    row = MemoryEmbeddingRepository(session).list_for_book("book_a")[0]
    assert result["mode"] == "semantic"
    assert client.calls == [["warehouse search"], ["warehouse clue"]]
    assert row.dimensions == 3
    assert row.vector == [1.0, 0.0, 0.0]


def test_retrieve_memory_evidence_honors_disabled_mode_and_missing_key_reason(session):
    session.add(
        Book(
            id="book_a",
            title="A",
            platform="fanqie",
            genre="urban",
            book_outline={"main": "warehouse clue"},
        )
    )
    session.flush()
    fallback = RecordingKeywordClient()

    disabled = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse clue",
        embedding_client=DeterministicSemanticClient(fail="provider down"),
        model_name="fake-semantic",
        fallback_client=fallback,
        embedding_fallback_mode="disabled",
        limit=3,
    )
    missing_key = retrieve_memory_evidence(
        session=session,
        book_id="book_a",
        target_chapter_number=2,
        query="warehouse clue",
        embedding_client=None,
        model_name="keyword-hash",
        fallback_client=fallback,
        embedding_fallback_mode="keyword",
        limit=3,
    )

    assert disabled == {
        "mode": "none",
        "degradation_reason": "provider down",
        "items": [],
    }
    assert fallback.calls == [["warehouse clue"], ['{"main": "warehouse clue"}']]
    assert missing_key["mode"] == "keyword"
    assert missing_key["degradation_reason"] == "embedding API key is not configured"
