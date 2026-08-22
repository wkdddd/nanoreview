"""Embedding client for semantic search via OpenAI-compatible API."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from loguru import logger

from nanoreview.utils.log_style import log_event

_DASHSCOPE_MAX_INPUT_CHARS = 1024


def create_embedding_client_from_config(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> "EmbeddingClient | None":
    if not getattr(config, "enable", False):
        return None
    values = env or os.environ
    api_key = (
        getattr(config, "api_key", "")
        or values.get("DASHSCOPE_API_KEY", "")
    )
    if not api_key:
        return None
    return EmbeddingClient(
        api_key=api_key,
        model=getattr(config, "model", "text-embedding-v3"),
        base_url=getattr(config, "base_url", "")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        dimensions=getattr(config, "dimensions", 1024),
        batch_size=getattr(config, "batch_size", None) or 25,
        max_input_chars=getattr(config, "max_input_chars", None)
        or _DASHSCOPE_MAX_INPUT_CHARS,
    )


class EmbeddingClient:
    """Embedding via OpenAI-compatible API (DashScope, OpenAI, etc.)."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v1",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        dimensions: int = 1024,
        batch_size: int = 25,
        max_input_chars: int = _DASHSCOPE_MAX_INPUT_CHARS,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_input_chars = max(1, int(max_input_chars))
        self._client: Any = None
        self._auth_failed = False
        self._auth_error_reported = False
        self._unavailable = False
        self._unavailable_reported = False
        self._preflight_done = False
        self._omit_dimensions = False

    @property
    def unavailable(self) -> bool:
        return self._unavailable or self._auth_failed

    async def ensure_available(self) -> bool:
        """Run a one-shot lightweight embedding check before expensive indexing."""
        if self.unavailable:
            return False
        if self._preflight_done:
            return True
        self._preflight_done = True
        result = await self.embed_query("nanobot embedding preflight")
        return result is not None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    async def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        """Batch embed texts. Returns None per item on failure (no zero-vector fallback)."""
        if not texts:
            return []
        client = self._get_client()
        all_embeddings: list[list[float] | None] = [None] * len(texts)
        if self._unavailable:
            return all_embeddings
        normalized = [
            (idx, prepared)
            for idx, text in enumerate(texts)
            if (prepared := self._prepare_input(text)) is not None
        ]
        for i in range(0, len(normalized), self.batch_size):
            if self._auth_failed:
                break
            batch = normalized[i: i + self.batch_size]
            batch_indexes = [idx for idx, _ in batch]
            batch_texts = [text for _, text in batch]
            batch_info = _batch_log_info(
                batch_texts,
                batch_index=i // self.batch_size,
                batch_start=i,
                total_batches=(len(normalized) + self.batch_size - 1) // self.batch_size,
                total_inputs=len(texts),
                prepared_inputs=len(normalized),
            )
            try:
                response = await self._create_embeddings(client, batch_texts)
                for original_index, item in zip(batch_indexes, response.data):
                    all_embeddings[original_index] = item.embedding
            except Exception as e:
                if _is_compat_error(e):
                    try:
                        response = await self._retry_compat_embeddings(client, batch_texts)
                        for original_index, item in zip(batch_indexes, response.data):
                            all_embeddings[original_index] = item.embedding
                        continue
                    except Exception as retry_error:
                        e = retry_error
                if _is_auth_error(e):
                    self._auth_failed = True
                    message = (
                        "Embedding API authentication failed; semantic search is disabled "
                        "for this process. Check rag.embedding.api_key or DASHSCOPE_API_KEY. "
                        f"base_url={self.base_url!r} model={self.model!r} "
                        f"dimensions={self.dimensions} api_key={_mask_key(self.api_key)!r} "
                        f"error={e}"
                    )
                    log_event(
                        logger,
                        "error",
                        "rag.embedding.auth_failed",
                        status="failed",
                        base_url=self.base_url,
                        model=self.model,
                        dimensions=self.dimensions,
                        api_key=_mask_key(self.api_key),
                        reason=e,
                        **_error_log_info(e),
                        **batch_info,
                    )
                    self._print_terminal_error_once(message)
                    break
                if _is_unavailable_error(e):
                    self._unavailable = True
                    self._log_unavailable_once(e, batch_info=batch_info)
                    break
                log_event(
                    logger,
                    "warning",
                    "rag.embedding.call_failed",
                        status="failed",
                        base_url=self.base_url,
                        model=self.model,
                        dimensions=self.dimensions,
                        reason=e,
                        **_error_log_info(e),
                        **batch_info,
                    )
        return all_embeddings

    async def _create_embeddings(self, client: Any, batch_texts: list[str]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": batch_texts,
            "encoding_format": "float",
        }
        if not self._omit_dimensions:
            kwargs["dimensions"] = self.dimensions
        return await client.embeddings.create(**kwargs)

    async def _retry_compat_embeddings(self, client: Any, batch_texts: list[str]) -> Any:
        retry_texts = batch_texts[: max(1, min(len(batch_texts), 2))]
        log_event(
            logger,
            "warning",
            "rag.embedding.compat_retry",
            status="retry",
            base_url=self.base_url,
            model=self.model,
            dimensions=self.dimensions,
            batch_size=len(batch_texts),
            retry_batch_size=len(retry_texts),
            omit_dimensions=True,
        )
        response = await client.embeddings.create(
            model=self.model,
            input=retry_texts,
            encoding_format="float",
        )
        self._omit_dimensions = True
        if len(retry_texts) == len(batch_texts):
            return response
        remaining = batch_texts[len(retry_texts):]
        second = await client.embeddings.create(
            model=self.model,
            input=remaining,
            encoding_format="float",
        )
        return SimpleNamespace(data=[*response.data, *second.data])

    async def embed_query(self, query: str) -> list[float] | None:
        """Embed a single query string. Returns None on failure."""
        if self._prepare_input(query) is None:
            return None
        results = await self.embed_texts([query])
        return results[0] if results else None

    def _prepare_input(self, text: str) -> str | None:
        prepared = str(text).strip()
        if not prepared:
            return None
        return prepared[: self.max_input_chars]

    def _log_unavailable_once(self, error: Exception, *, batch_info: dict[str, Any]) -> None:
        if self._unavailable_reported:
            return
        self._unavailable_reported = True
        log_event(
            logger,
            "warning",
            "rag.embedding.unavailable",
            status="failed",
            base_url=self.base_url,
            model=self.model,
            dimensions=self.dimensions,
            api_key=_mask_key(self.api_key),
            reason_type=_unavailable_reason_type(error),
            reason=error,
            **_error_log_info(error),
            **batch_info,
        )

    def _print_terminal_error_once(self, message: str) -> None:
        if self._auth_error_reported:
            return
        self._auth_error_reported = True
        try:
            print(f"[nanobot] ERROR: {message}", file=sys.stderr, flush=True)
        except Exception:
            pass


def _is_auth_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return True
    body = str(error).lower()
    return any(
        marker in body
        for marker in (
            "invalid_api_key",
            "incorrect api key",
            "apikey-error",
            "unauthorized",
            "authentication",
        )
    )


def _batch_log_info(
    texts: list[str],
    *,
    batch_index: int,
    batch_start: int,
    total_batches: int,
    total_inputs: int,
    prepared_inputs: int,
) -> dict[str, Any]:
    lengths = [len(text) for text in texts]
    return {
        "batch_index": batch_index,
        "batch_start": batch_start,
        "batch_size": len(texts),
        "batch_chars": sum(lengths),
        "batch_min_chars": min(lengths) if lengths else 0,
        "batch_max_chars": max(lengths) if lengths else 0,
        "total_batches": total_batches,
        "total_inputs": total_inputs,
        "prepared_inputs": prepared_inputs,
    }


def _is_unavailable_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in {400, 404, 408, 409, 422, 429, 500, 502, 503, 504}:
        return True
    body = str(error).lower()
    return any(
        marker in body
        for marker in (
            "unexpected mimetype",
            "text/plain",
            "attempt to decode json",
            "127.0.0.1:11434",
            "jsondecodeerror",
            "connection error",
            "connection refused",
            "read timed out",
            "timeout",
            "bad gateway",
            "service unavailable",
            "internal server error",
        )
    )


def _is_compat_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in {400, 404, 409, 422}:
        return True
    body = str(error).lower()
    return any(
        marker in body
        for marker in (
            "unexpected mimetype",
            "text/plain",
            "attempt to decode json",
            "dimensions",
            "encoding_format",
        )
    )


def _unavailable_reason_type(error: Exception) -> str:
    body = str(error).lower()
    if "127.0.0.1:11434" in body or "unexpected mimetype" in body or "text/plain" in body:
        return "upstream_bad_response"
    status_code = getattr(error, "status_code", None)
    if status_code in {429, 408, 500, 502, 503, 504}:
        return "provider_unavailable"
    if status_code in {400, 404, 409, 422}:
        return "provider_bad_request"
    return "provider_unavailable"


def _error_log_info(error: Exception) -> dict[str, Any]:
    headers = getattr(getattr(error, "response", None), "headers", None)
    content_type = ""
    if headers is not None:
        try:
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
    return {
        "status_code": getattr(error, "status_code", None) or "",
        "content_type": content_type,
        "error_type": type(error).__name__,
    }


def _mask_key(api_key: str) -> str:
    key = str(api_key or "")
    if len(key) <= 8:
        return "***" if key else ""
    return f"{key[:4]}...{key[-4:]}"
