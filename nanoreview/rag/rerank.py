"""Cross-encoder reranking via external API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from loguru import logger

_DASHSCOPE_COMPATIBLE_MODE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_QWEN3_RERANK_BASE = "https://dashscope.aliyuncs.com/compatible-api/v1"
_DASHSCOPE_TEXT_RERANK_BASE = "https://dashscope.aliyuncs.com"
_DASHSCOPE_TEXT_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
_RERANK_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query."
)


def create_rerank_client_from_config(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> "RerankClient | None":
    if not getattr(config, "enable", False):
        return None
    values = env or os.environ
    api_key = (
        _config_value(config, "api_key", "apiKey", "apikey")
        or values.get("DASHSCOPE_API_KEY", "")
    )
    if not api_key:
        return None
    return RerankClient(
        api_key=api_key,
        model=getattr(config, "model", "qwen3-vl-rerank"),
        base_url=_config_value(config, "base_url", "baseUrl", "apiBase")
        or _DASHSCOPE_COMPATIBLE_MODE_BASE,
        top_n=int(_config_value(config, "top_n", "topN") or 20),
        instruct=_config_value(config, "instruct"),
    )


def _config_value(config: Any, *names: str) -> Any:
    for name in names:
        value = getattr(config, name, None)
        if value:
            return value
    return None


class RerankClient:
    """Rerank candidates using DashScope reranking APIs."""

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3-vl-rerank",
        base_url: str = _DASHSCOPE_COMPATIBLE_MODE_BASE,
        top_n: int = 20,
        instruct: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.top_n = top_n
        self.instruct = instruct
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self._effective_base_url(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
        return self._client

    def _effective_base_url(self) -> str:
        if self.model == "qwen3-rerank":
            if self.base_url.rstrip("/") == _DASHSCOPE_COMPATIBLE_MODE_BASE:
                return _DASHSCOPE_QWEN3_RERANK_BASE
            return self.base_url
        if self.model in {"qwen3-vl-rerank", "gte-rerank-v2"}:
            if self.base_url.rstrip("/") in {
                _DASHSCOPE_COMPATIBLE_MODE_BASE,
                _DASHSCOPE_QWEN3_RERANK_BASE,
            }:
                return _DASHSCOPE_TEXT_RERANK_BASE
            return self.base_url
        return self.base_url

    def _endpoint_path(self) -> str:
        if self.model == "qwen3-rerank":
            return "/reranks"
        if self.model in {"qwen3-vl-rerank", "gte-rerank-v2"}:
            return _DASHSCOPE_TEXT_RERANK_PATH
        return "/rerank"

    def _request_body(self, query: str, documents: list[str], top_n: int) -> dict[str, Any]:
        if self.model == "qwen3-rerank":
            body: dict[str, Any] = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            }
            body["instruct"] = self.instruct or _RERANK_INSTRUCT
            return body

        if self.model in {"qwen3-vl-rerank", "gte-rerank-v2"}:
            body = {
                "model": self.model,
                "input": {
                    "query": query,
                    "documents": documents,
                },
                "parameters": {
                    "top_n": top_n,
                },
            }
            if self.model == "qwen3-vl-rerank" and self.instruct:
                body["parameters"]["instruct"] = self.instruct
            return body

        return {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance. Returns [(original_index, score)] descending.

        On failure, returns empty list (caller should keep original order).
        """
        if not documents:
            return []
        n = min(top_n or self.top_n, len(documents))
        client = self._get_client()
        try:
            resp = await client.post(
                self._endpoint_path(),
                json=self._request_body(query, documents, n),
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or data.get("output", {}).get("results", [])
            return [
                (int(r["index"]), float(r.get("relevance_score", 0.0)))
                for r in sorted(results, key=lambda x: -x.get("relevance_score", 0.0))
            ]
        except Exception as e:
            logger.warning("Rerank API call failed: {}", e)
            return []
