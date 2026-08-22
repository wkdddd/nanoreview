from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from loguru import logger

from nanoreview.rag.index import RAGIndex
from nanoreview.rag.qdrant_store import QdrantVectorHit, QdrantVectorStore, stable_point_id
from nanoreview.rag.utils import IndexedChunk, IndexedHit


class _EmbeddingClient:
    dimensions = 3

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []
        self.embed_batches: list[list[str]] = []
        self.batch_size = 10

    async def embed_query(self, query: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.embed_batches.append(list(texts))
        self.embedded_texts.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def ensure_available(self) -> bool:
        return True


class _UnavailableEmbeddingClient(_EmbeddingClient):
    unavailable = False

    async def ensure_available(self) -> bool:
        self.unavailable = True
        return False


@dataclass
class _VectorStore:
    fail_search: bool = False
    search_key: tuple[str, int, int, str] = ("src/app.py", 1, 1, "text")
    upsert_count: int | None = None
    existing_point_ids: set[str] | None = None
    existing_payloads: dict[str, str | None] | None = None

    def __post_init__(self) -> None:
        self.upserted: list[IndexedChunk] = []
        self.searched = 0

    def upsert_chunks(
        self,
        *,
        source_type: str,
        chunks: list[IndexedChunk],
        vectors: list[list[float] | None],
    ) -> int:
        if self.upsert_count is not None:
            self.upserted.extend(chunks[: self.upsert_count])
            return self.upsert_count
        self.upserted.extend(chunks)
        return len([vector for vector in vectors if vector is not None])

    def search(
        self,
        *,
        source_type: str,
        query_vector: list[float],
        top_k: int,
    ) -> list[QdrantVectorHit]:
        if self.fail_search:
            raise RuntimeError("qdrant unavailable")
        self.searched += 1
        return [
            QdrantVectorHit(
                key=self.search_key,
                score=0.91,
                payload={},
            )
        ]

    def prune_missing(self, *, source_type: str, keep_point_ids: set[str]) -> int:
        return 0

    def list_point_ids(self, *, source_type: str) -> set[str]:
        return set(self.list_point_payloads(source_type=source_type))

    def list_point_payloads(self, *, source_type: str) -> dict[str, str | None]:
        if self.existing_payloads is not None:
            return dict(self.existing_payloads)
        if self.existing_point_ids is not None:
            return {point_id: None for point_id in self.existing_point_ids}
        return {
            stable_point_id(
                source_type,
                (chunk.path, int(chunk.start_line), int(chunk.end_line), chunk.kind),
            ): chunk.content_hash
            for chunk in self.upserted
        }


class _CapturingReranker:
    def __init__(self) -> None:
        self.documents: list[str] = []

    async def rerank(self, query: str, documents: list[str], top_n: int):
        self.documents = documents
        return [(0, 0.9)]


class _PayloadSchemaType:
    KEYWORD = "keyword"


class _QdrantModels:
    PayloadSchemaType = _PayloadSchemaType


class _QdrantClient:
    def __init__(self) -> None:
        self.created_indexes: list[tuple[str, str, object]] = []

    def collection_exists(self, _collection: str) -> bool:
        return True

    def create_payload_index(
        self,
        *,
        collection_name: str,
        field_name: str,
        field_schema: object,
    ) -> None:
        self.created_indexes.append((collection_name, field_name, field_schema))


@pytest.mark.asyncio
async def test_rerank_document_includes_math_metadata(tmp_path) -> None:
    reranker = _CapturingReranker()
    index = RAGIndex(tmp_path, rerank_client=reranker)
    hit = IndexedHit(
        chunk=IndexedChunk(
            source_type="math",
            path="lesson.md",
            start_line=10,
            end_line=12,
            kind="example_solution",
            text="由重要极限可知答案为 1。",
            title="重要极限例题",
            symbols=["chapter:极限", "example_id:e1"],
        ),
        score=1.0,
        reason=["hybrid"],
    )

    result = await index.rerank("sin x / x", [hit], 1)

    assert result == [hit]
    assert reranker.documents
    doc = reranker.documents[0]
    assert "标题: 重要极限例题" in doc
    assert "来源: lesson.md:10-12" in doc
    assert "类型: example_solution" in doc
    assert "chapter:极限" in doc
    assert "由重要极限可知答案为 1。" in doc


def _write_repo_file(tmp_path: Path, text: str = "def review_target():\n    return 'ok'\n") -> Path:
    path = tmp_path / "src" / "app.py"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _chunk_file(path: Path, text: str) -> list[IndexedChunk]:
    return [
        IndexedChunk(
            source_type="repo",
            path=path.as_posix(),
            start_line=1,
            end_line=1,
            kind="text",
            text=text,
        )
    ]


@pytest.mark.asyncio
async def test_search_prefers_qdrant_dense_candidates(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def dense_only_candidate():\n    return 'ok'\n")
    vector_store = _VectorStore()
    index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=vector_store,
    )
    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    hits = await index.search(source_type="repo", query="unmatched semantic query", max_hits=3)

    assert hits
    assert hits[0].chunk.path == "src/app.py"
    assert "qdrant" in hits[0].reason
    assert vector_store.upserted


@pytest.mark.asyncio
async def test_search_falls_back_to_lexical_when_qdrant_fails(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def lexical_target():\n    return 'ok'\n")
    log_lines: list[str] = []
    sink_id = logger.add(lambda msg: log_lines.append(str(msg)), format="{message}")
    index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=_VectorStore(fail_search=True),
    )
    try:
        index.sync_files(
            source_type="repo",
            files=[path],
            chunker=_chunk_file,
            max_file_chars=1000,
        )

        hits = await index.search(source_type="repo", query="lexical_target", max_hits=3)
    finally:
        logger.remove(sink_id)

    assert hits
    assert hits[0].chunk.path == "src/app.py"
    assert "bm25" in hits[0].reason
    text = "\n".join(log_lines)
    assert "%s" not in text
    assert "source_type=repo" in text
    assert "reason=qdrant unavailable" in text


@pytest.mark.asyncio
async def test_search_orders_qdrant_before_lexical_candidates(tmp_path) -> None:
    dense_path = _write_repo_file(tmp_path, "def dense_candidate():\n    return 'ok'\n")
    lexical_path = tmp_path / "src" / "lexical.py"
    lexical_path.write_text("def lexical_target():\n    return 'ok'\n", encoding="utf-8")
    vector_store = _VectorStore(search_key=("src/app.py", 1, 1, "text"))
    index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=vector_store,
    )
    index.sync_files(
        source_type="repo",
        files=[dense_path, lexical_path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    hits = await index.search(source_type="repo", query="lexical_target", max_hits=2)

    assert [hit.chunk.path for hit in hits][:2] == ["src/app.py", "src/lexical.py"]
    assert hits[0].reason == ["qdrant"]
    assert "bm25" in hits[1].reason


def test_sync_files_backfills_qdrant_for_current_sqlite_chunks(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def existing_sqlite_chunk():\n    return 'ok'\n")
    first_index = RAGIndex(tmp_path, embedding_client=_EmbeddingClient())
    first_index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    vector_store = _VectorStore()
    second_index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=vector_store,
    )
    second_index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    assert [chunk.path for chunk in vector_store.upserted] == ["src/app.py"]


def test_sync_files_skips_qdrant_when_embedding_preflight_fails(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def lexical_only():\n    return 'ok'\n")
    vector_store = _VectorStore()
    log_lines: list[str] = []
    sink_id = logger.add(lambda msg: log_lines.append(str(msg)), format="{message}")
    index = RAGIndex(
        tmp_path,
        embedding_client=_UnavailableEmbeddingClient(),
        vector_store=vector_store,
    )
    try:
        index.sync_files(
            source_type="repo",
            files=[path],
            chunker=_chunk_file,
            max_file_chars=1000,
        )
    finally:
        logger.remove(sink_id)

    assert vector_store.upserted == []
    text = "\n".join(log_lines)
    assert "embedding provider preflight failed" in text
    assert "lexical fallback active" in text


def test_sync_files_skips_unchanged_files_before_chunking(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def unchanged_cache_hit():\n    return 'ok'\n")
    index = RAGIndex(tmp_path)
    calls = 0

    def chunker(path: Path, text: str) -> list[IndexedChunk]:
        nonlocal calls
        calls += 1
        return _chunk_file(path, text)

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=chunker,
        max_file_chars=1000,
    )
    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=chunker,
        max_file_chars=1000,
    )

    assert calls == 1


def test_sync_files_skips_same_content_even_when_mtime_changes(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def same_content_new_mtime():\n    return 'ok'\n")
    index = RAGIndex(tmp_path)
    calls = 0

    def chunker(path: Path, text: str) -> list[IndexedChunk]:
        nonlocal calls
        calls += 1
        return _chunk_file(path, text)

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=chunker,
        max_file_chars=1000,
    )
    path.write_text("def same_content_new_mtime():\n    return 'ok'\n", encoding="utf-8")
    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=chunker,
        max_file_chars=1000,
    )

    assert calls == 1


def test_sync_files_retries_qdrant_backfill_after_incomplete_upsert(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def retry_qdrant_sync():\n    return 'ok'\n")
    first_index = RAGIndex(tmp_path, embedding_client=_EmbeddingClient())
    first_index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    vector_store = _VectorStore(upsert_count=0)
    second_index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=vector_store,
    )
    second_index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )
    vector_store.upsert_count = None
    second_index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    assert [chunk.path for chunk in vector_store.upserted] == ["src/app.py"]
    assert [text.replace("\r\n", "\n") for text in second_index.embedding_client.embedded_texts] == [
        "def retry_qdrant_sync():\n    return 'ok'\n"
    ]


def test_sync_files_reuses_embedding_cache_after_incomplete_qdrant_upsert(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def cached_vector_retry():\n    return 'ok'\n")
    vector_store = _VectorStore(upsert_count=0)
    embedding_client = _EmbeddingClient()
    index = RAGIndex(
        tmp_path,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )
    embedding_client.embedded_texts.clear()
    vector_store.upsert_count = None
    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    assert [chunk.path for chunk in vector_store.upserted] == ["src/app.py"]
    assert embedding_client.embedded_texts == []


def test_sync_files_backfills_only_missing_qdrant_points(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def first_chunk():\n    pass\n\ndef second_chunk():\n    pass\n")
    embedding_client = _EmbeddingClient()
    first_key = ("src/app.py", 1, 2, "text")
    existing = {stable_point_id("repo", first_key)}
    vector_store = _VectorStore(existing_point_ids=existing)
    index = RAGIndex(
        tmp_path,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )

    def two_chunks(path: Path, text: str) -> list[IndexedChunk]:
        return [
            IndexedChunk(
                source_type="repo",
                path=path.as_posix(),
                start_line=1,
                end_line=2,
                kind="text",
                text="first chunk has enough useful review content\nwith another line",
            ),
            IndexedChunk(
                source_type="repo",
                path=path.as_posix(),
                start_line=3,
                end_line=4,
                kind="text",
                text="second chunk has enough useful review content\nwith another line",
            ),
        ]

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=two_chunks,
        max_file_chars=1000,
    )

    assert [(chunk.start_line, chunk.text) for chunk in vector_store.upserted] == [
        (3, "second chunk has enough useful review content\nwith another line")
    ]
    assert embedding_client.embedded_texts == [
        "second chunk has enough useful review content\nwith another line"
    ]


def test_sync_files_backfills_stale_qdrant_point_hash(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "def changed_chunk():\n    return 'new'\n")
    point_id = stable_point_id("repo", ("src/app.py", 1, 1, "text"))
    vector_store = _VectorStore(existing_payloads={point_id: "old-hash"})
    index = RAGIndex(
        tmp_path,
        embedding_client=_EmbeddingClient(),
        vector_store=vector_store,
    )

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=_chunk_file,
        max_file_chars=1000,
    )

    assert [chunk.path for chunk in vector_store.upserted] == ["src/app.py"]


def test_sync_files_batches_embedding_backfill(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "one\ntwo\nthree\n")
    embedding_client = _EmbeddingClient()
    embedding_client.batch_size = 2
    vector_store = _VectorStore(existing_point_ids=set())
    index = RAGIndex(
        tmp_path,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )

    def three_chunks(path: Path, text: str) -> list[IndexedChunk]:
        return [
            IndexedChunk(source_type="repo", path=path.as_posix(), start_line=1, end_line=2, kind="text", text="chunk one has enough useful review content\nand a second line"),
            IndexedChunk(source_type="repo", path=path.as_posix(), start_line=3, end_line=4, kind="text", text="chunk two has enough useful review content\nand a second line"),
            IndexedChunk(source_type="repo", path=path.as_posix(), start_line=5, end_line=6, kind="text", text="chunk three has enough useful review content\nand a second line"),
        ]

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=three_chunks,
        max_file_chars=1000,
    )

    assert embedding_client.embed_batches == [
        [
            "chunk one has enough useful review content\nand a second line",
            "chunk two has enough useful review content\nand a second line",
        ],
        ["chunk three has enough useful review content\nand a second line"],
    ]
    assert len(vector_store.upserted) == 3


def test_bounded_dense_backfill_does_not_mark_full_sync(tmp_path) -> None:
    path = _write_repo_file(tmp_path, "one\ntwo\n")
    embedding_client = _EmbeddingClient()
    vector_store = _VectorStore(existing_point_ids=set())
    index = RAGIndex(
        tmp_path,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )

    def two_chunks(path: Path, text: str) -> list[IndexedChunk]:
        return [
            IndexedChunk(source_type="repo", path=path.as_posix(), start_line=1, end_line=2, kind="text", text="chunk one has enough useful review content\nand a second line"),
            IndexedChunk(source_type="repo", path=path.as_posix(), start_line=3, end_line=4, kind="text", text="chunk two has enough useful review content\nand a second line"),
        ]

    index.sync_files(
        source_type="repo",
        files=[path],
        chunker=two_chunks,
        max_file_chars=1000,
        dense_limit=1,
    )

    assert len(vector_store.upserted) == 1
    collection = getattr(vector_store, "collection", "")
    meta_key = f"qdrant_sync:repo:{collection}:{embedding_client.dimensions}"
    assert index._meta_get(meta_key) == ""


def test_qdrant_existing_collection_ensures_source_type_payload_index() -> None:
    client = _QdrantClient()
    store = QdrantVectorStore(url="http://localhost:6333", collection="chunks")
    store._client = client

    store._ensure_payload_indexes(client, _QdrantModels)

    assert client.created_indexes == [("chunks", "source_type", "keyword")]
