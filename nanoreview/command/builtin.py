"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass

from nanoreview import __version__
from nanoreview.bus.events import OutboundMessage
from nanoreview.command.router import CommandContext, CommandRouter
from nanoreview.utils.helpers import build_status_content
from nanoreview.utils.restart import set_restart_notice_to_env


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Stop the current task and start a fresh conversation.",
        "square-pen",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
    ),
    BuiltinCommandSpec(
        "/restart",
        "Restart nanoreview",
        "Restart the bot process in place.",
        "rotate-cw",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
    ),
    BuiltinCommandSpec(
        "/math-kb",
        "Manage math knowledge base",
        "List, add or convert local math knowledge files.",
        "book-open",
        "[list|add <path>|convert]",
    ),
    BuiltinCommandSpec(
        "/mistake-add",
        "Add to mistake book",
        "Save the latest math QA turn to the mistake book.",
        "square-pen",
        "[reason]",
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
    BuiltinCommandSpec(
        "/pairing",
        "Manage pairing",
        "List, approve, deny or revoke pairing requests.",
        "shield",
        "[list|approve <code>|deny <code>|revoke <user_id>]",
    ),
    BuiltinCommandSpec(
        "/permission",
        "Show deny-list policy",
        "Show tool execution deny-list policy.",
        "lock",
        "[show]",
    ),
)


def builtin_command_palette() -> list[dict[str, str]]:
    """Return structured command metadata for UI command palettes."""
    return [spec.as_dict() for spec in BUILTIN_COMMAND_SPECS]


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(ctx.key)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=dict(msg.metadata or {}),
    )

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanoreview"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)

    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    # Never let usage fetch break /status
    with suppress(Exception):
        from nanoreview.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
            max_completion_tokens=getattr(
                getattr(loop.provider, "generation", None), "max_tokens", 8192
            ),
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and start a fresh session."""
    loop = ctx.loop
    await loop._cancel_active_tasks(ctx.key)
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop) -> list[str]:
    names = set(loop.model_presets)
    names.add("default")
    return ["default", *sorted(name for name in names if name != "default")]


def _active_model_preset_name(loop) -> str:
    return loop.model_preset or "default"


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _model_command_status(loop) -> str:
    names = _model_preset_names(loop)
    active = _active_model_preset_name(loop)
    return "\n".join([
        "## Model",
        f"- Current model: `{loop.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=_model_command_status(loop),
            metadata=metadata,
        )

    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/model [preset]`",
            metadata=metadata,
        )

    name = parts[0]
    try:
        loop.set_model_preset(name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = getattr(getattr(loop.provider, "generation", None), "max_tokens", None)
    lines = [
        f"Switched model preset to `{loop.model_preset}`.",
        f"- Model: `{loop.model}`",
        f"- Context window: {loop.context_window_tokens}",
    ]
    if max_tokens is not None:
        lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_math_kb(ctx: CommandContext) -> OutboundMessage:
    """List, add or convert files for the lightweight math knowledge base."""
    from pathlib import Path

    from nanoreview.agent.math_qa import MathKnowledgeBase

    kb = MathKnowledgeBase(
        ctx.loop.workspace,
        embedding_config=getattr(ctx.loop, "embedding_config", None),
        rerank_config=getattr(ctx.loop, "rerank_config", None),
        qdrant_config=getattr(ctx.loop, "qdrant_config", None),
    )
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    if not args or args.lower() == "list":
        files = kb.list_files()
        if not files:
            content = (
                "Math knowledge base is empty.\n"
                "Add UTF-8 Markdown/TXT/JSON/JSONL/PDF/image files with `/math-kb add <path>`.\n"
                "Convert PDF/image files to Markdown with `/math-kb convert`.\n"
                f"Storage: `{kb.base_dir}`"
            )
        else:
            lines = ["Math knowledge base files:"]
            for path in files:
                try:
                    label = path.relative_to(ctx.loop.workspace).as_posix()
                except ValueError:
                    label = str(path)
                lines.append(f"- `{label}`")
            content = "\n".join(lines)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata=metadata,
        )

    parts = args.split(maxsplit=1)
    action = parts[0].lower()
    if action == "convert":
        try:
            from nanoreview.agent.tools._mathrag.math_knowledge_convert import MathKnowledgeMarkdownConverter

            converter = MathKnowledgeMarkdownConverter(ctx.loop.workspace)
            results = converter.convert_all(write=True)
        except Exception as exc:
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content=f"Could not convert math knowledge files: {exc}",
                metadata=metadata,
            )

        if not results:
            content = (
                "No math knowledge files found to convert.\n"
                f"Storage: `{kb.base_dir}`"
            )
        else:
            lines = ["Math knowledge conversion complete:"]
            for result in results:
                source = result.source_path.relative_to(ctx.loop.workspace).as_posix()
                target = (
                    result.markdown_path.relative_to(ctx.loop.workspace).as_posix()
                    if result.markdown_path else "(not written)"
                )
                status = "ok" if result.ok else "warning"
                suffix = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
                lines.append(f"- `{source}` -> `{target}` [{status}]{suffix}")
                for warning in result.warnings[:3]:
                    lines.append(f"  - {warning}")
            try:
                await kb.async_sync_index()
                lines.append("")
                lines.append("Math RAG index refreshed.")
            except Exception as exc:
                lines.append("")
                lines.append(f"Math RAG index refresh skipped: {exc}")
            content = "\n".join(lines)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata=metadata,
        )

    if action != "add" or len(parts) != 2:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/math-kb [list|add <path>|convert]`",
            metadata=metadata,
        )

    raw_path = parts[1].strip().strip('"')
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (ctx.loop.workspace / path).resolve()
    try:
        target = kb.add_file(path)
    except Exception as exc:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Could not add knowledge file: {exc}",
            metadata=metadata,
        )
    try:
        label = target.relative_to(ctx.loop.workspace).as_posix()
    except ValueError:
        label = str(target)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=f"Added knowledge file: `{label}`",
        metadata=metadata,
    )


async def cmd_mistake_add(ctx: CommandContext) -> OutboundMessage:
    """Save the latest math QA turn to the mistake book."""
    from nanoreview.agent.math_qa import append_mistake_record

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    reason = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    try:
        record = append_mistake_record(
            ctx.loop.workspace,
            session,
            error_reason=reason,
        )
    except Exception as exc:
        content = f"Could not add to mistake book: {exc}"
    else:
        tags = ", ".join(record.get("knowledge_tags") or []) or "未提取"
        content = (
            "Added the latest question to the mistake book.\n"
            f"- 掌握状态：{record['mastery_status']}\n"
            f"- 错误原因：{record['error_reason'] or '未填写'}\n"
            f"- 知识点标签：{tags}"
        )
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=metadata,
    )


_GOAL_PROMPT_TEMPLATE = """The user declared a sustained objective for this thread.

Inspect or clarify if needed, then call `long_task` with the refined objective (and optional short ui_summary). Work proceeds as normal assistant turns using your usual tools. When the objective is fully done and verified, call `complete_goal` with a brief recap. If the user later cancels or changes direction, still call `complete_goal` with an honest recap (then `long_task` again only after there is no active goal). Do not use `long_task` / `complete_goal` for trivial one-shot answers.

Goal:
{goal}
"""


async def cmd_goal(ctx: CommandContext) -> OutboundMessage | None:
    """Rewrite /goal into a normal agent turn that nudges long_task use."""
    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if ctx.session is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "A task is already running for this chat. "
                "Use `/stop` first, then send `/goal <long-running task description>` again."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    ctx.msg.metadata = {
        **dict(ctx.msg.metadata or {}),
        "original_command": "/goal",
        "original_content": ctx.raw,
        "goal_started_at": time.time(),
    }
    ctx.msg.content = _GOAL_PROMPT_TEMPLATE.format(goal=goal)
    return None


async def cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    """List, approve, deny or revoke pairing requests."""
    from nanoreview.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command

    reply = handle_pairing_command(ctx.msg.channel, ctx.args)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=reply,
        metadata={PAIRING_COMMAND_META_KEY: True},
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["🐈 nanoreview commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


async def cmd_permission(ctx: CommandContext) -> OutboundMessage:
    """Show the active tool deny-list policy."""
    msg = ctx.msg
    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    args = ctx.args.strip().lower()
    meta = {"render_as": "text", **(msg.metadata or {})}

    if not args or args == "show":
        exec_cfg = getattr(ctx.loop, "exec_config", None)
        user_deny = list(getattr(exec_cfg, "deny_patterns", []) or [])
        user_allow = list(getattr(exec_cfg, "allow_patterns", []) or [])
        lines = [
            f"Tool deny-list policy (session: {session.key})",
            "  default: allow tool calls",
            "  blocked: built-in exec deny-list, tools.exec.denyPatterns, SSRF/internal URLs, workspace boundaries",
            f"  configured denyPatterns: {len(user_deny)}",
            f"  configured allowPatterns: {len(user_allow)}",
        ]
        if user_deny:
            lines.append("  denyPatterns:")
            lines.extend(f"    - {pattern}" for pattern in user_deny)
        if user_allow:
            lines.append("  allowPatterns:")
            lines.extend(f"    - {pattern}" for pattern in user_allow)
        content = "\n".join(lines)

    else:
        content = "Usage: /permission [show]"

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
    )


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/math-kb", cmd_math_kb)
    router.prefix("/math-kb ", cmd_math_kb)
    router.exact("/mistake-add", cmd_mistake_add)
    router.prefix("/mistake-add ", cmd_mistake_add)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/help", cmd_help)
    router.exact("/pairing", cmd_pairing)
    router.prefix("/pairing ", cmd_pairing)
    router.exact("/permission", cmd_permission)
    router.prefix("/permission ", cmd_permission)
