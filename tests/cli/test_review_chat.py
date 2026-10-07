"""CLI entry tests: ``review`` keeps its admitted session for follow-up chat.

The interactive phase must address the *same* session the review run was
admitted into, otherwise the completed report never reaches the conversation
handoff and the review gate looks up an unrelated session. These tests pin that
wiring plus the shared session runner both CLI entries use.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.runner import AgentRunResult
from nanoreview.bus.events import InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.cli import commands
from nanoreview.config.schema import Config
from nanoreview.events import StreamDeltaEvent
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.result import ReviewHandoffState

REVIEW_SESSION_KEY = "cli:review:deadbeef"


class StubCoordinator:
    """Minimal stand-in for ``SessionCoordinator``: echoes each inbound turn.

    ``run``/``stop`` mirror the real coordinator's bus loop contract: ``stop``
    ends the loop, so the CLI's shutdown gather cannot park forever.
    """

    def __init__(self) -> None:
        self.bus = MessageBus()
        self.channels_config = None
        self.stopped = False
        self.received: list[InboundMessage] = []

    async def run(self) -> None:
        while not self.stopped:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            self.received.append(msg)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"echo:{msg.content}",
                )
            )

    def stop(self) -> None:
        self.stopped = True


def _reader(lines: list[str]):
    pending = list(lines)

    async def _read() -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    return _read


@pytest.mark.parametrize(
    ("chat", "fail_on", "stdin_tty", "stdout_tty", "expected"),
    [
        (True, None, False, False, True),
        (False, "high", True, True, False),
        (None, None, True, True, True),
        (None, None, False, True, False),
        (None, None, True, False, False),
        (None, "high", True, True, False),
    ],
)
def test_resolve_cli_chat_mode(
    monkeypatch: pytest.MonkeyPatch,
    chat: bool | None,
    fail_on: str | None,
    stdin_tty: bool,
    stdout_tty: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: stdin_tty))
    monkeypatch.setattr("sys.stdout", SimpleNamespace(isatty=lambda: stdout_tty))

    assert commands._resolve_cli_chat_mode(chat, fail_on=fail_on) is expected


async def test_cli_session_pins_turns_to_override_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = StubCoordinator()

    await commands._run_cli_session(
        loop,
        config=Config(),
        session_key=REVIEW_SESSION_KEY,
        session_key_override=REVIEW_SESSION_KEY,
        read_input=_reader(["hello", "/exit"]),
    )

    assert loop.stopped is True
    assert [msg.session_key for msg in loop.received] == [REVIEW_SESSION_KEY]
    assert loop.received[0].metadata["_wants_stream"] is True
    assert "echo:hello" in capsys.readouterr().out


async def test_cli_session_without_override_keeps_derived_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = StubCoordinator()

    await commands._run_cli_session(
        loop,
        config=Config(),
        session_key="cli:direct",
        read_input=_reader(["hi", "quit"]),
    )

    assert [msg.session_key for msg in loop.received] == ["cli:direct"]
    assert loop.received[0].session_key_override is None
    assert "echo:hi" in capsys.readouterr().out


async def test_cli_session_discards_stale_outbound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Events an earlier phase left on the bus must not become this turn's reply."""
    loop = StubCoordinator()
    await loop.bus.publish_outbound(
        OutboundMessage(
            channel="cli",
            chat_id="review",
            content="stale reasoning",
            metadata={"_progress": True, "_reasoning": True},
        )
    )
    await loop.bus.publish_outbound(
        OutboundMessage(
            channel="cli",
            chat_id="review",
            content="stale subagent trace",
            metadata={"_subagent_end": True},
        )
    )

    await commands._run_cli_session(
        loop,
        config=Config(),
        session_key=REVIEW_SESSION_KEY,
        session_key_override=REVIEW_SESSION_KEY,
        read_input=_reader(["hello", "/exit"]),
    )

    out = capsys.readouterr().out
    assert "stale reasoning" not in out
    assert "stale subagent trace" not in out
    assert "echo:hello" in out


async def test_cli_session_stops_on_eof() -> None:
    loop = StubCoordinator()

    await commands._run_cli_session(
        loop,
        config=Config(),
        session_key="cli:direct",
        read_input=_reader([]),
    )

    assert loop.stopped is True
    assert loop.received == []


class _FakeAdmission:
    def __init__(self, target: str) -> None:
        self.session_key = REVIEW_SESSION_KEY
        self.run_id = "run-1"
        self.target = target
        self.target_type = "local"
        self.action = SimpleNamespace(value="diff")
        self.content = f"review {target}"
        self.metadata: dict[str, Any] = {}


class _FakeLoop:
    def __init__(
        self,
        report_chunks: list[str] | None = None,
        response_content: str = "",
        handoff: str = "complete",
        review_result: Any = "default",
    ) -> None:
        self.bus = MessageBus()
        self.channels_config = None
        self.closed = False
        self.review_requests: list[Any] = []
        self.direct_calls: list[dict[str, Any]] = []
        self._report_chunks = report_chunks or []
        self._response_content = response_content
        # The review command reads the terminal handoff state to decide its
        # exit code: anything short of ``complete`` exits non-zero.
        self.handoff = handoff
        self._review_result = review_result
        self.review_loop = SimpleNamespace(result=self._result)

    def _result(self, session_key: str) -> Any:
        if self._review_result == "default":
            return SimpleNamespace(handoff=ReviewHandoffState(self.handoff))
        return self._review_result

    def admit_review(self, request: Any) -> _FakeAdmission:
        self.review_requests.append(request)
        return _FakeAdmission(str(request.target))

    async def process_direct(self, content: str, **kwargs: Any) -> Any:
        self.direct_calls.append({"content": content, **kwargs})
        events = kwargs.get("events")
        if events is not None:
            for chunk in self._report_chunks:
                await events.emit(StreamDeltaEvent(content=chunk))
        return SimpleNamespace(content=self._response_content, metadata={})

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def review_stubs(monkeypatch: pytest.MonkeyPatch):
    """Patch the review command's runtime seams and record the chat phase."""

    def _install(loop: _FakeLoop) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []

        monkeypatch.setattr(
            commands, "_load_runtime_config", lambda config, workspace: Config()
        )
        monkeypatch.setattr(commands, "sync_workspace_templates", lambda workspace: None)
        monkeypatch.setattr(
            commands.SessionCoordinator,
            "from_config",
            staticmethod(lambda config, bus: loop),
        )
        monkeypatch.setattr(commands, "_install_cli_signal_handlers", lambda: None)

        async def _fake_session(agent_loop: Any, **kwargs: Any) -> None:
            sessions.append({"loop": agent_loop, **kwargs})

        monkeypatch.setattr(commands, "_run_cli_session", _fake_session)
        return sessions

    return _install


def test_review_chat_continues_the_admitted_session(
    review_stubs, tmp_path
) -> None:
    loop = _FakeLoop()
    sessions = review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff", "--chat"]
    )

    assert result.exit_code == 0, result.output
    assert loop.closed is True
    assert loop.direct_calls[0]["session_key"] == REVIEW_SESSION_KEY
    assert len(sessions) == 1
    assert sessions[0]["loop"] is loop
    assert sessions[0]["session_key"] == REVIEW_SESSION_KEY
    assert sessions[0]["session_key_override"] == REVIEW_SESSION_KEY


def test_review_no_chat_stays_one_shot(review_stubs, tmp_path) -> None:
    loop = _FakeLoop()
    sessions = review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff", "--no-chat"]
    )

    assert result.exit_code == 0, result.output
    assert sessions == []
    assert loop.closed is True


def test_review_prints_the_returned_report(review_stubs, tmp_path) -> None:
    """The report comes back as the direct response, not as stream deltas."""
    loop = _FakeLoop(response_content="## Report\n\nUnused import of json.")
    review_stubs(loop)

    result = CliRunner().invoke(
        commands.app,
        ["review", str(tmp_path), "--action", "diff", "--no-chat", "--no-markdown"],
    )

    assert result.exit_code == 0, result.output
    assert "Unused import of json." in result.output


def test_review_output_saves_the_returned_report(review_stubs, tmp_path) -> None:
    loop = _FakeLoop(response_content="## Report\n\nUnused import of json.")
    review_stubs(loop)
    report_path = tmp_path / "report.md"

    result = CliRunner().invoke(
        commands.app,
        [
            "review",
            str(tmp_path),
            "--action",
            "diff",
            "--no-chat",
            "--output",
            str(report_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert report_path.read_text(encoding="utf-8") == "## Report\n\nUnused import of json."


def test_review_piped_without_chat_flag_stays_one_shot(review_stubs, tmp_path) -> None:
    """CliRunner stdin/stdout are not a TTY, so the default stays one-shot."""
    loop = _FakeLoop()
    sessions = review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff"]
    )

    assert result.exit_code == 0, result.output
    assert sessions == []


def test_review_fail_on_exits_before_opening_a_session(review_stubs, tmp_path) -> None:
    loop = _FakeLoop(report_chunks=["## Critical\n\nboom"])
    sessions = review_stubs(loop)

    result = CliRunner().invoke(
        commands.app,
        ["review", str(tmp_path), "--action", "diff", "--fail-on", "high"],
    )

    assert result.exit_code == 1
    assert sessions == []


def test_review_fail_on_keeps_gate_status_after_explicit_chat(
    review_stubs, tmp_path
) -> None:
    loop = _FakeLoop(report_chunks=["## High\n\nboom"])
    sessions = review_stubs(loop)

    result = CliRunner().invoke(
        commands.app,
        ["review", str(tmp_path), "--action", "diff", "--chat", "--fail-on", "high"],
    )

    assert result.exit_code == 1
    assert len(sessions) == 1


@pytest.mark.parametrize("handoff", ["partial", "failed"])
def test_review_exits_non_zero_on_an_incomplete_handoff(
    review_stubs, tmp_path, handoff: str
) -> None:
    """A partial/failed review is a non-zero result even without --fail-on."""
    loop = _FakeLoop(response_content="## Report\n\nok", handoff=handoff)
    review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff", "--no-chat"]
    )

    assert result.exit_code == 1, result.output


def test_review_exits_non_zero_when_no_review_result_exists(
    review_stubs, tmp_path
) -> None:
    """An unsettled run (no terminal result) is reported as a failure."""
    loop = _FakeLoop(response_content="## Report\n\nok", review_result=None)
    review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff", "--no-chat"]
    )

    assert result.exit_code == 1, result.output


def test_review_complete_handoff_without_findings_exits_zero(
    review_stubs, tmp_path
) -> None:
    """A complete review with no findings still exits 0."""
    loop = _FakeLoop(response_content="## Report\n\nNo actionable issues found.")
    review_stubs(loop)

    result = CliRunner().invoke(
        commands.app, ["review", str(tmp_path), "--action", "diff", "--no-chat"]
    )

    assert result.exit_code == 0, result.output


class _EchoProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="answer")

    def get_default_model(self) -> str:
        return "echo"


class _CapturingRunner:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    async def run(self, spec: Any) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="answer",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


async def test_chat_phase_lands_in_the_review_session(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real coordinator + bus: a follow-up turn is written to the review session.

    This is the behaviour the ``session_key_override`` exists for — without it
    the chat would open a sibling ``cli:review`` session and the completed
    report would never reach the conversation.
    """
    coordinator = SessionCoordinator(MessageBus(), _EchoProvider(), tmp_path)
    coordinator.conversation_loop._runner = _CapturingRunner()

    await commands._run_cli_session(
        coordinator,
        config=Config(),
        session_key=REVIEW_SESSION_KEY,
        session_key_override=REVIEW_SESSION_KEY,
        read_input=_reader(["报告里最严重的问题是什么？", "/exit"]),
    )

    history = coordinator.sessions.get_or_create(REVIEW_SESSION_KEY).get_history(
        max_messages=0
    )
    assert any("报告里最严重的问题是什么？" in str(m.get("content")) for m in history)
    assert "answer" in capsys.readouterr().out
    assert coordinator.sessions.get_or_create("cli:review").get_history(
        max_messages=0
    ) == []
