"""RAG configuration models."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class BaseRAGConfig(BaseModel):
    """Base RAG config accepting camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class EmbeddingConfig(BaseRAGConfig):
    """Embedding model configuration for vector generation."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    enable: bool = False
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "BAAI/bge-m3"
    dimensions: int = 1024
    batch_size: int = 10
    max_input_chars: int = 2048


class RerankConfig(BaseRAGConfig):
    """Reranking model configuration for retrieval refinement."""

    enable: bool = False
    api_key: str = ""
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-api/v1",
        validation_alias=AliasChoices("baseUrl", "apiBase", "base_url"),
        serialization_alias="baseUrl",
    )
    model: str = "qwen3-rerank"
    top_n: int = 20
    instruct: str | None = None


class QdrantConfig(BaseRAGConfig):
    """Qdrant vector store configuration for RAG."""

    enable: bool = False
    url: str = "http://localhost:6333"
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("apiKey", "api_key"),
        serialization_alias="apiKey",
    )
    collection: str = "nanobot_rag_chunks"
    timeout: float = 30.0
    check_compatibility: bool = Field(
        default=False,
        validation_alias=AliasChoices("checkCompatibility", "check_compatibility"),
        serialization_alias="checkCompatibility",
    )


class RAGRetrievalConfig(BaseRAGConfig):
    """Retrieval strategy configuration shared by RAG consumers."""

    semantic_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    enable_rrf: bool = True
    enable_chonkie: bool = True
    max_results: int = Field(default=8, ge=1, le=30)
    snippet_lines: int = Field(default=8, ge=1, le=80)
    chunk_lines: int = Field(default=80, ge=1, le=1000)
    chunk_overlap: int = Field(default=12, ge=0, le=500)
    max_chunks_per_file: int = Field(default=40, ge=1, le=1000)
    max_file_chars: int = Field(default=80_000, ge=1000)
    max_files: int = Field(default=2000, ge=1)


class RAGConfig(BaseRAGConfig):
    """Top-level RAG configuration."""

    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    retrieval: RAGRetrievalConfig = Field(default_factory=RAGRetrievalConfig)
