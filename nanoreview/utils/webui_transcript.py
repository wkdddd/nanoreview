"""Append-only WebUI display transcript (JSONL), separate from agent session."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanoreview.config.paths import get_webui_dir
from nanoreview.session.manager import SessionManager
from nanoreview.utils.log_sanitization import sanitize_persisted_log_text
from nanoreview.utils.subagent_trace import read_subagent_cards

WEBUI_TRANSCRIPT_SCHEMA_VERSION = 4
_MAX_TRANSCRIPT_FILE_BYTES = 8 * 1024 * 1024


def webui_transcript_path(session_key: str) -> Path:
    stem = SessionManager.safe_key(session_key)
    return get_webui_dir() / f"{stem}.jsonl"


def read_transcript_lines(session_key: str) -> list[dict[str, Any]]:
    path = webui_transcript_path(session_key)
    if not path.is_file():
        return []
    size = path.stat().st_size
    if size > _MAX_TRANSCRIPT_FILE_BYTES:
        logger.warning("webui transcript too large, skipping: {}", path)
        return []
    lines_out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("bad jsonl at {} line {}", path, line_no)
                    continue
                if isinstance(obj, dict):
                    lines_out.append(obj)
    except OSError as e:
        logger.warning("read transcript failed {}: {}", path, e)
        return []
    return lines_out


def append_transcript_object(session_key: str, obj: dict[str, Any]) -> None:
    raw = json.dumps(_sanitize_transcript_value(obj), ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_TRANSCRIPT_FILE_BYTES:
        msg = "webui transcript line too large"
        raise ValueError(msg)
    path = webui_transcript_path(session_key)
    line = raw + "\n"
    line_size = len(line.encode("utf-8"))
    if path.is_file() and path.stat().st_size + line_size > _MAX_TRANSCRIPT_FILE_BYTES:
        logger.warning("webui transcript size limit reached, skipping append: {}", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _sanitize_transcript_value(value: Any) -> Any:
    """Recursively sanitize text before writing a WebUI transcript sidecar."""
    if isinstance(value, str):
        return sanitize_persisted_log_text(value)
    if isinstance(value, list):
        return [_sanitize_transcript_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_transcript_value(item) for key, item in value.items()}
    return value


def delete_webui_transcript(session_key: str) -> bool:
    path = webui_transcript_path(session_key)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        logger.warning("Failed to delete webui transcript {}: {}", path, e)
        return False


def _format_tool_call_trace(call: Any) -> str | None:
    if not call or not isinstance(call, dict):
        return None
    fn = call.get("function")
    name = fn.get("name") if isinstance(fn, dict) else None
    if not isinstance(name, str) or not name:
        raw_name = call.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
    if not name:
        return None
    args = (fn.get("arguments") if isinstance(fn, dict) else None) or call.get("arguments")
    if isinstance(args, str) and args.strip():
        return f"{name}({args})"
    if args and isinstance(args, dict):
        return f"{name}({json.dumps(args, ensure_ascii=False)})"
    return f"{name}()"


def tool_trace_lines_from_events(events: Any) -> list[str]:
    if not isinstance(events, list):
        return []
    lines: list[str] = []
    for event in events:
        if not event or not isinstance(event, dict):
            continue
        if event.get("phase") != "start":
            continue
        t = _format_tool_call_trace(event)
        if t:
            lines.append(t)
    return lines


def _record_created_at_ms(rec: dict[str, Any], idx: int, fallback_base: int) -> int:
    value = rec.get("createdAt")
    if isinstance(value, bool):
        value = None
    if isinstance(value, int | float):
        ms = int(value)
        if ms > 0:
            return ms
    if isinstance(value, str):
        try:
            ms = int(float(value))
        except ValueError:
            ms = 0
        if ms > 0:
            return ms
    return fallback_base + idx


def replay_transcript_to_subagent_cards(
    lines: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach persisted subagent content streams to their display cards."""
    by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []

    def card_for(task_id: str, label: Any, created_at: int) -> dict[str, Any]:
        card = by_id.get(task_id)
        if card is not None:
            return card
        card = {
            "id": task_id,
            "label": str(label or task_id),
            "status": "running",
            "output": "",
            "outputStreaming": False,
            "startedAt": created_at,
        }
        by_id[task_id] = card
        ordered_ids.append(task_id)
        return card

    for card in cards:
        task_id = card.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        copied = {
            "id": task_id,
            "label": str(card.get("label") or task_id),
            "status": card.get("status") if card.get("status") in {"running", "completed", "error"} else "error",
            "output": "",
            "outputStreaming": False,
            "startedAt": int(card.get("startedAt") or 0),
        }
        by_id[task_id] = copied
        ordered_ids.append(task_id)

    for idx, rec in enumerate(lines):
        task_id = rec.get("subagent_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        created_at = _record_created_at_ms(rec, idx, int(time.time() * 1000))
        card = card_for(task_id, rec.get("subagent_label") or rec.get("label"), created_at)
        event = rec.get("event")
        if event == "subagent_status":
            label = rec.get("label")
            if isinstance(label, str) and label:
                card["label"] = label
            status = rec.get("status")
            if status in {"running", "completed", "error"}:
                card["status"] = status
                if status != "running":
                    card["outputStreaming"] = False
            continue
        if event == "delta" and rec.get("kind") == "subagent_content":
            text = rec.get("text")
            if isinstance(text, str) and text:
                card["output"] += text
                card["outputStreaming"] = True
            continue
        if event == "stream_end" and rec.get("kind") == "subagent_content":
            card["outputStreaming"] = False

    return [by_id[task_id] for task_id in ordered_ids]


def replay_transcript_to_ui_messages(
    lines: list[dict[str, Any]],
    *,
    augment_user_media: Callable[[list[str]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Fold JSONL records into ``UIMessage``-shaped dicts for the WebUI.

    Mirrors the core fold in ``useNanobotStream.ts`` (delta, reasoning,
    message+kind, turn_end). ``augment_user_media`` maps persisted filesystem
    paths to ``{url, name?}`` / attachment dicts the client expects.
    """
    messages: list[dict[str, Any]] = []
    buffer_message_id: str | None = None
    buffer_parts: list[str] = []
    review_thinking_parts: list[str] = []
    review_thinking_created_at: int | None = None
    saw_review_thinking = False
    suppress_until_turn_end = False
    _ts_base = int(time.time() * 1000)

    def _new_id(prefix: str, idx: int) -> str:
        return f"{prefix}-{idx}-{uuid.uuid4().hex[:8]}"

    def attach_reasoning_chunk(
        prev: list[dict[str, Any]],
        chunk: str,
        idx: int,
        created_at: int,
    ) -> None:
        for i in range(len(prev) - 1, -1, -1):
            candidate = prev[i]
            if candidate.get("role") == "user":
                break
            if candidate.get("kind") == "trace":
                break
            if candidate.get("role") != "assistant":
                continue
            content = str(candidate.get("content") or "")
            has_answer = len(content) > 0
            if (
                candidate.get("reasoningStreaming")
                or candidate.get("reasoning") is not None
                or has_answer
                or candidate.get("isStreaming")
            ):
                prev[i] = {
                    **candidate,
                    "reasoning": (str(candidate.get("reasoning") or "")) + chunk,
                    "reasoningStreaming": True,
                }
                return
            if not has_answer and candidate.get("isStreaming"):
                prev[i] = {**candidate, "reasoning": chunk, "reasoningStreaming": True}
                return
            break
        prev.append(
            {
                "id": _new_id("as", idx),
                "role": "assistant",
                "content": "",
                "isStreaming": True,
                "reasoning": chunk,
                "reasoningStreaming": True,
                "createdAt": created_at,
            },
        )

    def find_active_placeholder(prev: list[dict[str, Any]]) -> str | None:
        last = prev[-1] if prev else None
        if not last:
            return None
        if last.get("role") != "assistant" or last.get("kind") == "trace":
            return None
        if str(last.get("content") or ""):
            return None
        if not last.get("isStreaming"):
            return None
        return str(last.get("id"))

    def close_reasoning(prev: list[dict[str, Any]]) -> None:
        for i in range(len(prev) - 1, -1, -1):
            if prev[i].get("reasoningStreaming"):
                prev[i] = {**prev[i], "reasoningStreaming": False}
                return

    def is_reasoning_only_placeholder(m: dict[str, Any]) -> bool:
        return (
            m.get("role") == "assistant"
            and m.get("kind") != "trace"
            and not str(m.get("content") or "").strip()
            and bool(m.get("reasoning"))
            and not m.get("reasoningStreaming")
            and not m.get("media")
        )

    def is_tool_trace_at(index: int) -> bool:
        m = messages[index] if 0 <= index < len(messages) else None
        return bool(m and m.get("kind") == "trace")

    def flush_review_thinking(idx: int, created_at: int, *, streaming: bool) -> None:
        nonlocal review_thinking_parts, review_thinking_created_at
        if not review_thinking_parts:
            return
        messages.append(
            {
                "id": _new_id("think", idx),
                "role": "assistant",
                "content": "",
                "isStreaming": streaming,
                "reasoning": "".join(review_thinking_parts),
                "reasoningStreaming": streaming,
                "preserveReasoning": True,
                "createdAt": review_thinking_created_at or created_at,
            },
        )
        review_thinking_parts = []
        review_thinking_created_at = None

    def prune_reasoning_only() -> None:
        nonlocal messages
        kept: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            if (
                is_reasoning_only_placeholder(m)
                and not m.get("preserveReasoning")
                and not is_tool_trace_at(i + 1)
            ):
                continue
            kept.append(m)
        messages = kept

    def stamp_latency(latency_ms: int) -> None:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant" and messages[i].get("kind") != "trace":
                messages[i] = {
                    **messages[i],
                    "latencyMs": latency_ms,
                    "isStreaming": False,
                }
                return

    def absorb_complete(extra: dict[str, Any], idx: int, created_at: int) -> None:
        nonlocal review_thinking_parts, review_thinking_created_at
        is_streamed_review_report = extra.pop("_streamed_review_report", False)
        if is_streamed_review_report and review_thinking_parts and "reasoning" not in extra:
            extra = {
                **extra,
                "reasoning": "".join(review_thinking_parts),
                "reasoningStreaming": False,
            }
            review_thinking_parts = []
            review_thinking_created_at = None
        last = messages[-1] if messages else None
        if last and is_reasoning_only_placeholder(last) and not last.get("preserveReasoning"):
            messages[-1] = {
                **last,
                **extra,
                "isStreaming": False,
                "reasoningStreaming": False,
            }
        else:
            messages.append(
                {
                    "id": _new_id("as", idx),
                    "role": "assistant",
                    "createdAt": created_at,
                    **extra,
                },
                )

    for idx, rec in enumerate(lines):
        ev = rec.get("event")
        created_at = _record_created_at_ms(rec, idx, _ts_base)
        if rec.get("subagent_id"):
            # Subagent content belongs in its own card rather than the main
            # assistant conversation. See ``replay_transcript_to_subagent_cards``.
            continue
        if ev == "user":
            text = rec.get("text")
            text_s = text if isinstance(text, str) else ""
            media_paths = rec.get("media_paths")
            paths: list[str] = []
            if isinstance(media_paths, list):
                paths = [str(p) for p in media_paths if p]
            media_att: list[dict[str, Any]] | None = None
            if paths and augment_user_media is not None:
                media_att = augment_user_media(paths)
            row: dict[str, Any] = {
                "id": _new_id("u", idx),
                "role": "user",
                "content": text_s,
                "createdAt": created_at,
            }
            review = rec.get("review")
            if not isinstance(review, dict):
                review = {
                    "target": rec.get("review_target"),
                    "target_type": rec.get("review_target_type"),
                    "mode": rec.get("review_mode_variant"),
                    "action": rec.get("review_action"),
                    "focus": rec.get("review_focus"),
                }
            if isinstance(review, dict):
                review_target = review.get("target")
                review_target_type = review.get("target_type")
                review_mode = review.get("mode")
                review_action = review.get("action")
                review_focus = review.get("focus")
                review_row: dict[str, Any] = {}
                if isinstance(review_target, str) and review_target.strip():
                    review_row["target"] = review_target.strip()
                if isinstance(review_target_type, str) and review_target_type.strip():
                    review_row["target_type"] = review_target_type.strip()
                if isinstance(review_mode, str) and review_mode.strip():
                    review_row["mode"] = review_mode.strip()
                if isinstance(review_action, str) and review_action.strip():
                    review_row["action"] = review_action.strip()
                if isinstance(review_focus, list):
                    focus = [str(item).strip() for item in review_focus if str(item).strip()]
                    if focus:
                        review_row["focus"] = focus
                if review_row:
                    row["review"] = review_row
            if media_att:
                row["media"] = media_att
                if all(m.get("kind") == "image" for m in media_att):
                    row["images"] = [{"url": m.get("url"), "name": m.get("name")} for m in media_att]
            messages.append(row)
            continue

        if ev == "delta":
            if suppress_until_turn_end:
                continue
            kind = rec.get("kind")
            if kind == "review_thinking":
                saw_review_thinking = True
                continue
            if kind is None and saw_review_thinking:
                continue
            chunk = rec.get("text")
            if not isinstance(chunk, str):
                continue
            adopted = find_active_placeholder(messages) if buffer_message_id is None else None
            if buffer_message_id is None:
                if adopted:
                    buffer_message_id = adopted
                else:
                    buffer_message_id = _new_id("buf", idx)
                    messages.append(
                        {
                            "id": buffer_message_id,
                            "role": "assistant",
                            "content": "",
                            "isStreaming": True,
                            "createdAt": created_at,
                        },
                    )
            buffer_parts.append(chunk)
            combined = "".join(buffer_parts)
            for i, m in enumerate(messages):
                if m.get("id") == buffer_message_id:
                    updated = {**m, "content": combined, "isStreaming": True}
                    messages[i] = updated
                    break
            continue

        if ev == "stream_end":
            if suppress_until_turn_end:
                buffer_message_id = None
                buffer_parts = []
                continue
            if rec.get("kind") == "review_thinking":
                continue
            if rec.get("kind") == "review_report":
                review_thinking_parts = []
                review_thinking_created_at = None
            buffer_message_id = None
            buffer_parts = []
            continue

        if ev == "reasoning_delta":
            continue

        if ev == "reasoning_end":
            continue

        if ev == "message":
            if suppress_until_turn_end and rec.get("kind") in (
                "tool_hint",
                "progress",
                "reasoning",
            ):
                continue
            kind = rec.get("kind")
            if kind == "reasoning":
                continue
            if kind in ("tool_hint", "progress"):
                structured = tool_trace_lines_from_events(rec.get("tool_events"))
                text = rec.get("text")
                trace_lines = structured if structured else ([text] if isinstance(text, str) and text else [])
                if not trace_lines:
                    continue
                last = messages[-1] if messages else None
                if last and last.get("kind") == "trace" and not last.get("isStreaming"):
                    prev_traces = list(last.get("traces") or [last.get("content")])
                    merged_traces = prev_traces + trace_lines
                    messages[-1] = {
                        **last,
                        "traces": merged_traces,
                        "content": trace_lines[-1],
                    }
                else:
                    messages.append(
                        {
                            "id": _new_id("tr", idx),
                            "role": "tool",
                            "kind": "trace",
                            "content": trace_lines[-1],
                            "traces": trace_lines,
                            "createdAt": created_at,
                        },
                    )
                continue

            buffer_message_id = None
            buffer_parts = []
            text = rec.get("text")
            content_s = text if isinstance(text, str) else ""
            flush_review_thinking(idx, created_at, streaming=False)
            media_urls = rec.get("media_urls")
            media: list[dict[str, Any]] = []
            if isinstance(media_urls, list):
                for m in media_urls:
                    if isinstance(m, dict) and m.get("url"):
                        media.append(
                            {
                                "kind": "image",
                                "url": str(m["url"]),
                                "name": str(m.get("name") or ""),
                            },
                        )
            extra: dict[str, Any] = {"content": content_s}
            if media:
                extra["media"] = media
            lat = rec.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                extra["latencyMs"] = int(lat)
            absorb_complete(extra, idx, created_at)
            if media:
                suppress_until_turn_end = True
            continue

        if ev == "turn_end":
            suppress_until_turn_end = False
            saw_review_thinking = False
            flush_review_thinking(idx, created_at, streaming=False)
            for i, m in enumerate(messages):
                if m.get("isStreaming"):
                    messages[i] = {**m, "isStreaming": False}
                records = m.get("permissionRecords")
                if records:
                    updated = False
                    for j, r in enumerate(records):
                        if not r.get("resolved"):
                            records[j] = {**r, "resolved": True, "approved": False}
                            updated = True
                    if updated:
                        messages[i] = {**messages[i], "permissionRecords": list(records)}
            prune_reasoning_only()
            lat = rec.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                stamp_latency(int(lat))
            buffer_message_id = None
            buffer_parts = []
            continue

        if ev == "permission_request":
            request_id = rec.get("request_id")
            if not isinstance(request_id, str):
                continue
            record = {
                "requestId": request_id,
                "toolName": rec.get("tool_name", ""),
                "arguments": rec.get("arguments"),
                "permission": rec.get("permission"),
                "createdAt": created_at,
            }
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if m.get("role") == "user":
                    break
                if m.get("role") == "assistant" and m.get("kind") != "trace":
                    prev_records = list(m.get("permissionRecords") or [])
                    prev_records.append(record)
                    messages[i] = {**m, "permissionRecords": prev_records}
                    break
            continue

        if ev == "permission_response":
            request_id = rec.get("request_id")
            approved = bool(rec.get("approved", False))
            if not isinstance(request_id, str):
                continue
            for m in messages:
                records = m.get("permissionRecords")
                if not records:
                    continue
                for j, r in enumerate(records):
                    if r.get("requestId") == request_id:
                        records[j] = {**r, "resolved": True, "approved": approved}
                        break
            continue

    flush_review_thinking(len(lines), _ts_base + len(lines), streaming=True)
    for m in messages:
        m.pop("isStreaming", None)
        m.pop("reasoningStreaming", None)
    return messages


def build_webui_thread_response(
    session_key: str,
    *,
    augment_user_media: Callable[[list[str]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Return a payload compatible with ``WebuiThreadPersistedPayload``."""
    lines = read_transcript_lines(session_key)
    if not lines:
        return None
    msgs = replay_transcript_to_ui_messages(lines, augment_user_media=augment_user_media)
    response = {
        "schemaVersion": WEBUI_TRANSCRIPT_SCHEMA_VERSION,
        "sessionKey": session_key,
        "messages": msgs,
    }
    response["subagentCards"] = replay_transcript_to_subagent_cards(
        lines,
        read_subagent_cards(session_key),
    )
    return response
