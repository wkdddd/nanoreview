"""Shared data classes and utility functions for RAG retrieval."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(slots=True)
class IndexedChunk:
    source_type: str
    path: str
    start_line: int
    end_line: int
    text: str
    kind: str = "text"
    symbols: list[str] = field(default_factory=list)
    title: str = ""
    url: str = ""
    query: str = ""
    fetched_at: str = ""
    mtime: float = 0.0
    content_hash: str = ""


@dataclass(slots=True)
class IndexedHit:
    chunk: IndexedChunk
    score: float
    reason: list[str] = field(default_factory=list)


ChunkKey = tuple[str, int, int, str]
ChunkerFn = Callable[[Path, str], list[IndexedChunk]]


def chunk_key(chunk: IndexedChunk) -> ChunkKey:
    return (chunk.path, int(chunk.start_line), int(chunk.end_line), chunk.kind)


def hit_key(hit: IndexedHit) -> ChunkKey:
    return chunk_key(hit.chunk)


_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that",
    "怎么", "如何", "什么", "一个", "这个", "那个",
}


def query_terms(query: str) -> list[str]:
    '''把用户查询字符串拆成用于检索的关键词列表。'''
    raw = re.findall(
        r"[A-Za-z_][A-Za-z0-9_./:-]*|[一-鿿]+",
        query.lower(),
    )
    terms = [t for t in raw if len(t) >= 2 and t not in _STOP_WORDS]
    return list(dict.fromkeys(terms))


def best_snippet(text: str, terms: list[str], *, start_line: int, snippet_lines: int) -> str:
    lines = text.replace("\r\n", "\n").splitlines()
    if not lines:
        return ""

    best_index = 0
    best_score = 0
    lowered_terms = [t.lower() for t in terms]
    for i, line in enumerate(lines):
        low = line.lower()
        score = sum(1 for t in lowered_terms if t in low)
        if score > best_score:
            best_score = score
            best_index = i

    half = max(1, snippet_lines // 2)
    start = max(0, best_index - half)
    end = min(len(lines), start + snippet_lines)
    return "\n".join(
        f"{start_line + n}| {lines[n]}" for n in range(start, end)
    )


def chunk_from_row(row: tuple) -> IndexedChunk:
    symbols_raw = row[6] or "[]"
    try:
        symbols = json.loads(symbols_raw)
    except json.JSONDecodeError:
        symbols = []
    if not isinstance(symbols, list):
        symbols = []
    return IndexedChunk(
        source_type=str(row[0]),
        path=str(row[1]),
        start_line=int(row[2]),
        end_line=int(row[3]),
        kind=str(row[4]),
        text=str(row[5]),
        symbols=[str(s) for s in symbols],
        title=str(row[7] or ""),
        url=str(row[8] or ""),
        query=str(row[9] or ""),
        fetched_at=str(row[10] or ""),
        mtime=float(row[11] or 0.0),
        content_hash=str(row[12] or ""),
    )


def rrf_merge(
    ranked_lists: list[tuple[str, list[IndexedHit]]],
    *,
    limit: int,
    k: int = 60,
) -> list[IndexedHit]:
    """Merge ranked retrieval lanes with reciprocal-rank fusion."""

    scores: dict[ChunkKey, float] = {}
    hits: dict[ChunkKey, IndexedHit] = {}
    reasons: dict[ChunkKey, list[str]] = {}
    for lane_name, lane_hits in ranked_lists:
        for rank, hit in enumerate(lane_hits, start=1):
            key = hit_key(hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            hits.setdefault(key, hit)
            merged_reasons = reasons.setdefault(key, [])
            if lane_name not in merged_reasons:
                merged_reasons.append(lane_name)
            for reason in hit.reason:
                if reason not in merged_reasons:
                    merged_reasons.append(reason)

    merged: list[IndexedHit] = []
    for key, score in scores.items():
        original = hits[key]
        merged.append(IndexedHit(chunk=original.chunk, score=score, reason=reasons.get(key, [])))
    merged.sort(key=lambda hit: -hit.score)
    return merged[:limit]
