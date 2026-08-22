"""RAG runtime construction — instantiates clients from config."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanoreview.rag.config import RAGConfig, RAGRetrievalConfig
from nanoreview.rag.embedding import create_embedding_client_from_config
from nanoreview.rag.qdrant_store import QdrantVectorStore
from nanoreview.rag.rerank import create_rerank_client_from_config


@dataclass(frozen=True, slots=True)
class RAGRuntime:
    """Constructed RAG runtime clients and strategy settings."""

    embedding_client: Any | None = None
    rerank_client: Any | None = None
    vector_store: Any | None = None
    retrieval: RAGRetrievalConfig = field(default_factory=RAGRetrievalConfig)


def create_rag_runtime(config: RAGConfig | None) -> RAGRuntime:
    """Create embedding, rerank, and vector-store clients from RAG config."""

    cfg = config or RAGConfig()
    embedding_client = create_embedding_client_from_config(cfg.embedding)
    rerank_client = create_rerank_client_from_config(cfg.rerank)
    vector_store = QdrantVectorStore.from_config(
        cfg.qdrant,
        dimensions=getattr(embedding_client, "dimensions", cfg.embedding.dimensions),
    )
    return RAGRuntime(
        embedding_client=embedding_client,
        rerank_client=rerank_client,
        vector_store=vector_store,
        retrieval=cfg.retrieval,
    )
