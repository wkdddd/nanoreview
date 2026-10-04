"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger

from nanoreview.agent.memory import MemoryStore
from nanoreview.agent.skills import SkillsLoader

from nanoreview.utils.helpers import (
    current_time_str,
    detect_image_mime,
)
from nanoreview.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["COMMON_RULES.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    #: Shared rule file read once per turn/run/batch for the conversation agent,
    #: the review planner, each reviewer and each judge batch.
    COMMON_RULES_FILE = "COMMON_RULES.md"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        channel: str | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build the system prompt from identity, personalization files, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace.

        A missing file is normal; an unreadable one is skipped with a warning so
        a broken ``COMMON_RULES.md`` never interrupts the turn.
        """
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning(
                    "Skipping unreadable bootstrap file {}", file_path, exc_info=True
                )
                continue
            parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @classmethod
    def load_common_rules(cls, workspace: Path) -> str:
        """Read ``COMMON_RULES.md`` once for a run/batch.

        Used by the reviewer ``prompt_builder`` and the judge system prompt,
        which do not build a full ``ContextBuilder`` prompt. A missing or
        unreadable file returns an empty string (the caller skips the section
        and logs), so the read never interrupts the run.
        """
        try:
            return (Path(workspace) / cls.COMMON_RULES_FILE).read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            return ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        frozen, working = self.build_partitioned_messages(
            history,
            current_message,
            media=media,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=sender_id,
            session_summary=session_summary,
            session_metadata=session_metadata,
        )
        return [*frozen, *working]

    def build_partitioned_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the frozen/working partition of the context for one turn.

        Ownership is explicit and never inferred from the history's last role:

        - Frozen is the system prompt plus the current user (or subagent) turn,
          including the runtime-context block. It is copied verbatim into every
          request and run-level compression must never summarize or trim it.
        - Working is the replayable session history. It is the only zone
          run-level compression may summarize; everything the run appends later
          lands at its tail.

        The current turn is never merged back into the last history message, so
        a caller always knows which zone holds the live task.
        """
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        system_prompt = self.build_system_prompt(
            channel=channel, session_summary=session_summary
        )
        return (
            [
                {"role": "system", "content": system_prompt},
                {"role": current_role, "content": merged},
            ],
            [dict(message) for message in history],
        )


    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]