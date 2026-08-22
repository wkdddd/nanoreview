"""RAG subsystem - retrieval-augmented generation with FTS5 + vector search + rerank."""

from nanoreview.rag.chunker import TreeSitterChunker
from nanoreview.rag.config import (
    EmbeddingConfig,
    QdrantConfig,
    RAGConfig,
    RAGRetrievalConfig,
    RerankConfig,
)
from nanoreview.rag.embedding import EmbeddingClient, create_embedding_client_from_config
from nanoreview.rag.index import RAGIndex
from nanoreview.rag.qdrant_store import QdrantVectorHit, QdrantVectorStore
from nanoreview.rag.rerank import RerankClient, create_rerank_client_from_config
from nanoreview.rag.runtime import RAGRuntime, create_rag_runtime
from nanoreview.rag.utils import (
    ChunkerFn,
    ChunkKey,
    IndexedChunk,
    IndexedHit,
    best_snippet,
    chunk_key,
    hit_key,
    query_terms,
    rrf_merge,
)

__all__ = [
    "ChunkKey",
    "ChunkerFn",
    "EmbeddingClient",
    "EmbeddingConfig",
    "IndexedChunk",
    "IndexedHit",
    "QdrantConfig",
    "QdrantVectorHit",
    "QdrantVectorStore",
    "RAGConfig",
    "RAGIndex",
    "RAGRetrievalConfig",
    "RAGRuntime",
    "RerankClient",
    "RerankConfig",
    "TreeSitterChunker",
    "best_snippet",
    "chunk_key",
    "create_embedding_client_from_config",
    "create_rag_runtime",
    "create_rerank_client_from_config",
    "hit_key",
    "query_terms",
    "rrf_merge",
]
