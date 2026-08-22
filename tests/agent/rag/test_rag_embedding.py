from __future__ import annotations

import pytest
from loguru import logger
from pydantic import ValidationError

from nanoreview.rag.config import EmbeddingConfig
from nanoreview.rag.embedding import EmbeddingClient, create_embedding_client_from_config


class _Embedding:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _EmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "EmbeddingResponse",
            (),
            {"data": [_Embedding([float(i)]) for i, _ in enumerate(kwargs["input"])]},
        )()


class _AuthFailEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        error = RuntimeError("Incorrect API key provided invalid_api_key")
        error.status_code = 401
        raise error


class _UnavailableEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        error = RuntimeError(
            "500, message='Attempt to decode JSON with unexpected mimetype: text/plain; charset=utf-8'"
        )
        error.status_code = 400
        raise error


class _CompatRetryEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if "dimensions" in kwargs:
            error = RuntimeError(
                "400, message='Attempt to decode JSON with unexpected mimetype: text/plain; charset=utf-8'"
            )
            error.status_code = 400
            raise error
        return type(
            "EmbeddingResponse",
            (),
            {"data": [_Embedding([float(i)]) for i, _ in enumerate(kwargs["input"])]},
        )()


class _OpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _EmbeddingsAPI()


class _AuthFailOpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _AuthFailEmbeddingsAPI()


class _UnavailableOpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _UnavailableEmbeddingsAPI()


class _CompatRetryOpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _CompatRetryEmbeddingsAPI()


@pytest.mark.asyncio
async def test_embed_texts_skips_blank_inputs_and_preserves_positions() -> None:
    api = _OpenAIClient()
    client = EmbeddingClient(api_key="sk-test", batch_size=10)
    client._client = api

    results = await client.embed_texts([" first ", "", " \n\t ", "second"])

    assert api.embeddings.calls[0]["input"] == ["first", "second"]
    assert api.embeddings.calls[0]["encoding_format"] == "float"
    assert results == [[0.0], None, None, [1.0]]


@pytest.mark.asyncio
async def test_embed_texts_truncates_long_inputs_before_request() -> None:
    api = _OpenAIClient()
    client = EmbeddingClient(api_key="sk-test", max_input_chars=8)
    client._client = api

    results = await client.embed_texts(["abcdefghijk"])

    assert api.embeddings.calls[0]["input"] == ["abcdefgh"]
    assert results == [[0.0]]


@pytest.mark.asyncio
async def test_embed_query_returns_none_for_blank_query_without_api_call() -> None:
    api = _OpenAIClient()
    client = EmbeddingClient(api_key="sk-test")
    client._client = api

    result = await client.embed_query("  ")

    assert result is None
    assert api.embeddings.calls == []


@pytest.mark.asyncio
async def test_auth_failure_disables_embedding_client_for_process(capsys) -> None:
    api = _AuthFailOpenAIClient()
    client = EmbeddingClient(api_key="bad-key-123456", model="bad-model", base_url="https://bad.example/v1")
    client._client = api

    first = await client.embed_texts(["first"])
    second = await client.embed_texts(["second"])

    assert first == [None]
    assert second == [None]
    assert api.embeddings.calls == 1
    stderr = capsys.readouterr().err
    assert "Embedding API authentication failed" in stderr
    assert "https://bad.example/v1" in stderr
    assert "bad-model" in stderr
    assert "bad-...3456" in stderr
    assert "bad-key-123456" not in stderr


@pytest.mark.asyncio
async def test_unavailable_embedding_service_fuses_after_one_failed_batch() -> None:
    api = _UnavailableOpenAIClient()
    client = EmbeddingClient(api_key="sk-test", batch_size=2)
    client._client = api
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), format="{message}")

    try:
        first = await client.embed_texts(["first", "second secret body", "third"])
        second = await client.embed_texts(["fourth"])
    finally:
        logger.remove(sink_id)

    assert first == [None, None, None]
    assert second == [None]
    assert api.embeddings.calls == 2
    text = "\n".join(lines)
    assert "rag.embedding.unavailable" in text
    assert "batch_index=0" in text
    assert "batch_start=0" in text
    assert "batch_size=2" in text
    assert "total_batches=2" in text
    assert "prepared_inputs=3" in text
    assert "status_code=400" in text
    assert "error_type=RuntimeError" in text
    assert "second secret body" not in text


@pytest.mark.asyncio
async def test_embedding_retries_without_dimensions_for_provider_compat_error() -> None:
    api = _CompatRetryOpenAIClient()
    client = EmbeddingClient(api_key="sk-test", batch_size=10)
    client._client = api

    results = await client.embed_texts(["first", "second"])

    assert results == [[0.0], [1.0]]
    assert len(api.embeddings.calls) == 2
    assert "dimensions" in api.embeddings.calls[0]
    assert "dimensions" not in api.embeddings.calls[1]


@pytest.mark.asyncio
async def test_embedding_preflight_marks_unavailable_before_large_sync() -> None:
    api = _UnavailableOpenAIClient()
    client = EmbeddingClient(api_key="sk-test", batch_size=10)
    client._client = api

    assert await client.ensure_available() is False
    assert client.unavailable is True
    assert await client.ensure_available() is False
    assert api.embeddings.calls == 2


def test_create_embedding_client_reads_explicit_fields_only() -> None:
    config = EmbeddingConfig(
        enable=True,
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v2",
        max_input_chars=1024,
    )

    client = create_embedding_client_from_config(config)

    assert client is not None
    assert client.api_key == "sk-test"
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert client.model == "text-embedding-v2"
    assert client.max_input_chars == 1024


def test_create_embedding_client_reads_modelscope_base_url() -> None:
    config = EmbeddingConfig(
        enable=True,
        api_key="ms-test",
        base_url="https://api-inference.modelscope.cn/v1",
        model="Qwen/Qwen3-Embedding-8B",
    )

    client = create_embedding_client_from_config(config)

    assert client is not None
    assert client.base_url == "https://api-inference.modelscope.cn/v1"
    assert client.model == "Qwen/Qwen3-Embedding-8B"


def test_embedding_config_rejects_legacy_field_names() -> None:
    with pytest.raises(ValidationError):
        EmbeddingConfig(
            enable=True,
            apiKey="sk-test",
            apiBase="https://legacy.example/v1",
            maxInputChars=1024,
        )
