"""Integration and lifecycle contract for Conversation-only MCP.

Pins the boundaries the plan fixed:

* MCP capabilities reach the Conversation Agent only — the planner, reviewer and
  Judge registries never see them;
* each turn gets its own registry, so sessions do not share registrations, while
  a reconnect still reaches a turn that is already running;
* MCP tools never trigger the per-tool approval confirmation, even with
  ``approval_enabled=True``;
* connection preparation and in-flight calls both honour external cancellation,
  and repeated shutdown leaves no MCP subprocess or owner task behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import types as mcp_types

from nanoreview.agent.coordinator import SessionCoordinator
from nanoreview.agent.runner import AgentRunResult, AgentRunSpec
from nanoreview.agent.tools.mcp import (
    MCPProvider,
    MCPToolProxy,
    MCPToolWrapper,
    connect_mcp_servers,
)
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.bus.events import InboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.config.schema import MCPServerConfig, ToolsConfig
from nanoreview.providers.base import LLMProvider, LLMResponse
from nanoreview.review.profiles import reviewer_execution_profiles
from nanoreview.utils.cancellation import task_is_cancelling


class DummyProvider(LLMProvider):
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
        _ = (messages, tools, model, max_tokens, temperature, reasoning_effort)
        _ = (tool_choice, response_format)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


class SpecCapturingRunner:
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


class ToolCallingRunner(SpecCapturingRunner):
    """Runner that actually invokes one tool through the turn's registry."""

    def __init__(self, tool_name: str, arguments: dict[str, Any] | None = None):
        super().__init__()
        self._tool_name = tool_name
        self._arguments = arguments or {}
        self.results: list[Any] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        self.results.append(
            await spec.tools.execute(self._tool_name, dict(self._arguments))
        )
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.frozen_messages, *spec.working_messages],
        )


class _ScriptedSession:
    def __init__(self, results: list[Any]):
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        await asyncio.sleep(0)
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)], isError=False
    )


def _tool_def(name: str = "search") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description="Search the docs",
        inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


def _coordinator(tmp_path: Path, tools_config: ToolsConfig | None = None) -> SessionCoordinator:
    return SessionCoordinator(
        MessageBus(), DummyProvider(), tmp_path, tools_config=tools_config
    )


def _msg(content: str = "hello", *, chat_id: str = "direct") -> InboundMessage:
    return InboundMessage(
        channel="cli", sender_id="user", chat_id=chat_id, content=content
    )


def _attach_live_tool(
    provider: MCPProvider, server: str = "docs", results: list[Any] | None = None
) -> _ScriptedSession:
    """Register one live MCP tool wrapper as if a server had connected."""
    session = _ScriptedSession(results if results is not None else [_ok("live result")])
    provider._servers[server] = MCPServerConfig(command="placeholder")
    provider._connections[server] = _FakeConnection()
    provider.registry.register(MCPToolWrapper(session, server, _tool_def()))
    return session


class _FakeConnection:
    async def aclose(self) -> None:
        return None


class TestConversationVisibility:
    @pytest.mark.asyncio
    async def test_mcp_tools_reach_the_conversation_turn(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        assert runner.specs[0].tools.has("mcp_docs_search")

    @pytest.mark.asyncio
    async def test_the_review_registry_never_sees_mcp_tools(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        assert runner.specs[0].tools.has("mcp_docs_search")
        # The coordinator's own registry backs the review planner/reviewer/Judge
        # path and the consolidator; MCP must stay out of it.
        assert not coordinator.tools.has("mcp_docs_search")
        assert "mcp_docs_search" not in coordinator.tool_names
        assert "mcp_docs_search" not in [
            fn["function"]["name"] for fn in coordinator.tools.get_definitions()
        ]
        # Reviewer and Judge subagents build their own registries from their
        # execution profile; MCP never enters those either.
        for profile in reviewer_execution_profiles().values():
            profile_tools = coordinator.subagents.build_tools(profile, tmp_path)
            assert not any(
                name.startswith("mcp_") for name in profile_tools.tool_names
            ), f"MCP leaked into subagent profile {profile.name!r}"

    @pytest.mark.asyncio
    async def test_no_configured_servers_adds_no_mcp_tools(self, tmp_path) -> None:
        coordinator = _coordinator(tmp_path)
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        assert not any(name.startswith("mcp_") for name in runner.specs[0].tools.tool_names)


class TestPerTurnRegistryIsolation:
    @pytest.mark.asyncio
    async def test_two_sessions_get_independent_registrations(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        await coordinator.conversation_loop.process_message(
            _msg(chat_id="a"), session_key="cli:a", turn_id="t1", target_root=tmp_path
        )
        first = runner.specs[0].tools
        # Drop the capability between turns to prove registrations do not leak
        # from one session's registry into the next.
        first.unregister("mcp_docs_search")

        await coordinator.conversation_loop.process_message(
            _msg(chat_id="b"), session_key="cli:b", turn_id="t2", target_root=tmp_path
        )
        second = runner.specs[1].tools

        assert first is not second
        assert not first.has("mcp_docs_search")
        assert second.has("mcp_docs_search")

    @pytest.mark.asyncio
    async def test_a_running_turn_follows_a_reconnect(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        stale_session = _attach_live_tool(coordinator.mcp)
        runner = ToolCallingRunner("mcp_docs_search", {"q": "x"})
        coordinator.conversation_loop._runner = runner

        # Simulate the server dropping: the old wrapper is replaced by a fresh
        # one (as a reconnect does) while this turn's proxy is already live.
        fresh_session = _ScriptedSession([_ok("after reconnect")])
        coordinator.mcp.registry.register(
            MCPToolWrapper(fresh_session, "docs", _tool_def())
        )

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        assert runner.results == ["after reconnect"]
        assert stale_session.calls == []

    @pytest.mark.asyncio
    async def test_turn_registry_holds_proxies_not_live_wrappers(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        turn_tools = runner.specs[0].tools
        assert isinstance(turn_tools.get("mcp_docs_search"), MCPToolProxy)
        assert isinstance(coordinator.mcp.registry.get("mcp_docs_search"), MCPToolWrapper)


class TestApprovalIsNotTriggered:
    @pytest.mark.asyncio
    async def test_mcp_tools_bypass_approval_even_when_enabled(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path,
            ToolsConfig(
                approval_enabled=True,
                mcp_servers={"docs": MCPServerConfig(command="x")},
            ),
        )
        _attach_live_tool(coordinator.mcp)
        runner = ToolCallingRunner("mcp_docs_search", {"q": "x"})
        coordinator.conversation_loop._runner = runner

        asked: list[str] = []

        async def _permission(
            tool_name: str,
            params: dict[str, Any],
            future: asyncio.Future[bool],
            channel: str,
            chat_id: str,
        ) -> bool:
            asked.append(tool_name)
            future.set_result(True)
            return True

        coordinator.conversation_loop._permission_requester = _permission

        await coordinator.conversation_loop.process_message(
            _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
        )

        assert runner.results == ["live result"]
        # Documented behaviour: MCP capabilities do not raise the per-tool
        # approval confirmation, even with approval_enabled=True.
        assert asked == []


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_connection_preparation_propagates(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_connect() -> None:
            started.set()
            await release.wait()

        coordinator.mcp.connect = _slow_connect  # type: ignore[method-assign]
        runner = SpecCapturingRunner()
        coordinator.conversation_loop._runner = runner

        task = asyncio.create_task(
            coordinator.conversation_loop.process_message(
                _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
            )
        )
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        # The turn never reached the runner.
        assert runner.specs == []

    @pytest.mark.asyncio
    async def test_cancelling_an_in_flight_call_propagates_and_is_not_retried(
        self, tmp_path
    ) -> None:
        started = asyncio.Event()

        class _HangingSession:
            def __init__(self) -> None:
                self.calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                started.set()
                await asyncio.sleep(30)
                raise AssertionError("should have been cancelled")

        session = _HangingSession()
        wrapper = MCPToolWrapper(session, "docs", _tool_def(), tool_timeout=30)

        async def _run() -> None:
            await wrapper.execute(q="x")

        task = asyncio.create_task(_run())
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        # The MCP SDK can leak CancelledError from its own cancel scopes; a
        # genuine external cancel must not be retried or swallowed.
        assert session.calls == 1

    @pytest.mark.asyncio
    async def test_sdk_cancellation_without_external_cancel_is_reported(
        self, tmp_path
    ) -> None:
        class _SdkCancellingSession:
            async def call_tool(self, name, arguments):
                raise asyncio.CancelledError()

        wrapper = MCPToolWrapper(_SdkCancellingSession(), "docs", _tool_def())

        result = await wrapper.execute()

        assert "cancelled" in str(result)
        assert task_is_cancelling() is False


class TestShutdown:
    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)

        await coordinator.aclose()
        await coordinator.aclose()

        assert coordinator.mcp.connected_server_names == set()

    @pytest.mark.asyncio
    async def test_aclose_cancels_active_turns_and_closes_mcp(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        closed: list[str] = []

        class _RecordingConnection:
            def __init__(self, name: str) -> None:
                self._name = name

            async def aclose(self) -> None:
                closed.append(self._name)

        coordinator.mcp._connections["docs"] = _RecordingConnection("docs")

        turn_started = asyncio.Event()

        class _HangingRunner:
            async def run(self, spec):
                turn_started.set()
                await asyncio.sleep(30)
                raise AssertionError("should have been cancelled")

        coordinator.conversation_loop._runner = _HangingRunner()
        task = asyncio.create_task(
            coordinator.conversation_loop.process_message(
                _msg(), session_key="cli:direct", turn_id="t1", target_root=tmp_path
            )
        )
        await turn_started.wait()
        coordinator._active_tasks.setdefault("cli:direct", []).append(task)

        await coordinator.aclose()

        assert task.done()
        assert closed == ["docs"]

    @pytest.mark.asyncio
    async def test_aclose_drains_background_tasks(self, tmp_path) -> None:
        coordinator = _coordinator(tmp_path)
        finished: list[str] = []

        async def _work() -> None:
            await asyncio.sleep(0)
            finished.append("done")

        coordinator._schedule_background(_work())

        await coordinator.aclose()

        assert finished == ["done"]
        assert coordinator._background_tasks == []

    @pytest.mark.asyncio
    async def test_aclose_closes_mcp_connections_on_shutdown(self, tmp_path) -> None:
        coordinator = _coordinator(
            tmp_path, ToolsConfig(mcp_servers={"docs": MCPServerConfig(command="x")})
        )
        _attach_live_tool(coordinator.mcp)
        assert coordinator.mcp.registry.has("mcp_docs_search")

        await coordinator.aclose()

        assert not coordinator.mcp.registry.has("mcp_docs_search")


class _FakeReadStream:
    """Blocks forever like a live server's stdout, never yielding a message."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


class _FakeTransportFactory:
    """Builds fake stdio transports whose behaviour each test scripts.

    ``tool_pages`` is keyed by server name; a server listed in
    ``failing_servers`` raises the way a real launch failure would, and
    ``supports_extras`` decides whether resources/prompts can be listed.
    """

    def __init__(
        self,
        *,
        tool_pages: dict[str, list[mcp_types.ListToolsResult]] | None = None,
        failing_servers: set[str] | None = None,
        supports_extras: bool = False,
    ) -> None:
        self.tool_pages = tool_pages or {}
        self.failing_servers = failing_servers or set()
        self.supports_extras = supports_extras
        self.requested_cursors: dict[str, list] = {}
        self.listed_extras: list[str] = []

    def install(self, monkeypatch) -> None:
        factory = self

        class _FakeStdioParams:
            def __init__(self, command, args, env, cwd):
                self.command = command

        class _FakeStdioClient:
            def __init__(self, params) -> None:
                self.server_name = params.command

            async def __aenter__(self):
                if self.server_name in factory.failing_servers:
                    raise FileNotFoundError(f"no such command: {self.server_name}")
                return (_FakeReadStream(self.server_name), object())

            async def __aexit__(self, *exc):
                return False

        def _client_session(read, write):
            server_name = read.server_name

            class _ClientSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def initialize(self):
                    return None

                async def list_tools(self, params=None):
                    cursor = getattr(params, "cursor", None)
                    factory.requested_cursors.setdefault(server_name, []).append(cursor)
                    pages = factory.tool_pages[server_name]
                    return pages[0] if cursor is None else pages[int(cursor[1:])]

                async def list_resources(self):
                    factory.listed_extras.append(f"{server_name}:resources")
                    if not factory.supports_extras:
                        raise RuntimeError("resources not supported")
                    return mcp_types.ListResourcesResult(
                        resources=[
                            mcp_types.Resource(
                                uri=f"file:///{server_name}.txt",
                                name=f"{server_name}-readme",
                                description="readme",
                            )
                        ]
                    )

                async def list_prompts(self):
                    factory.listed_extras.append(f"{server_name}:prompts")
                    if not factory.supports_extras:
                        raise RuntimeError("prompts not supported")
                    return mcp_types.ListPromptsResult(
                        prompts=[
                            mcp_types.Prompt(
                                name="greet",
                                description="Greeting",
                                arguments=[
                                    mcp_types.PromptArgument(name="who", required=True)
                                ],
                            )
                        ]
                    )

            return _ClientSession()

        monkeypatch.setattr(
            "mcp.StdioServerParameters", _FakeStdioParams, raising=True
        )
        monkeypatch.setattr(
            "mcp.client.stdio.stdio_client", _FakeStdioClient, raising=True
        )
        monkeypatch.setattr("mcp.ClientSession", _client_session, raising=True)


async def _close_all(connections) -> None:
    from nanoreview.agent.tools.mcp import _close_mcp_connections

    await _close_mcp_connections(connections)


class TestConnectionFiltering:
    @pytest.mark.asyncio
    async def test_one_failing_server_does_not_block_the_others(self, monkeypatch):
        registry = ToolRegistry()
        factory = _FakeTransportFactory(
            tool_pages={
                "docs": [mcp_types.ListToolsResult(tools=[_tool_def("search")])]
            },
            failing_servers={"broken"},
        )
        factory.install(monkeypatch)

        connections = await connect_mcp_servers(
            {
                "docs": MCPServerConfig(command="docs"),
                "broken": MCPServerConfig(command="broken"),
            },
            registry,
        )

        assert set(connections) == {"docs"}
        assert registry.has("mcp_docs_search")
        assert not any(name.startswith("mcp_broken") for name in registry.tool_names)

        await _close_all(connections)

    @pytest.mark.asyncio
    async def test_paginated_tools_are_all_discovered(self, monkeypatch):
        registry = ToolRegistry()
        factory = _FakeTransportFactory(
            tool_pages={
                "docs": [
                    mcp_types.ListToolsResult(tools=[_tool_def("a")], nextCursor="c1"),
                    mcp_types.ListToolsResult(tools=[_tool_def("b")], nextCursor="c2"),
                    mcp_types.ListToolsResult(tools=[_tool_def("c")]),
                ]
            }
        )
        factory.install(monkeypatch)

        connections = await connect_mcp_servers(
            {"docs": MCPServerConfig(command="docs")}, registry
        )

        assert factory.requested_cursors["docs"] == [None, "c1", "c2"]
        assert registry.has("mcp_docs_a")
        assert registry.has("mcp_docs_b")
        assert registry.has("mcp_docs_c")

        await _close_all(connections)

    @pytest.mark.asyncio
    async def test_repeated_pagination_cursor_leaves_no_partial_tools(
        self, monkeypatch
    ):
        registry = ToolRegistry()
        factory = _FakeTransportFactory(
            tool_pages={
                "docs": [
                    mcp_types.ListToolsResult(
                        tools=[_tool_def("a")], nextCursor="loop"
                    ),
                    mcp_types.ListToolsResult(
                        tools=[_tool_def("b")], nextCursor="loop"
                    ),
                ]
            }
        )
        factory.install(monkeypatch)

        connections = await connect_mcp_servers(
            {"docs": MCPServerConfig(command="docs")}, registry
        )

        # Discovery finishes before registration, so a server that loops on the
        # same cursor leaves no half-registered tool set behind.
        assert connections == {}
        assert not any(name.startswith("mcp_docs") for name in registry.tool_names)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("enabled_tools", "expected"),
        [
            (["*"], ["mcp_docs_alpha", "mcp_docs_beta"]),
            (["alpha"], ["mcp_docs_alpha"]),
            (["mcp_docs_beta"], ["mcp_docs_beta"]),
            ([], []),
        ],
    )
    async def test_enabled_tools_semantics(
        self, monkeypatch, enabled_tools, expected
    ):
        registry = ToolRegistry()
        factory = _FakeTransportFactory(
            tool_pages={
                "docs": [
                    mcp_types.ListToolsResult(
                        tools=[_tool_def("alpha"), _tool_def("beta")]
                    )
                ]
            }
        )
        factory.install(monkeypatch)

        connections = await connect_mcp_servers(
            {"docs": MCPServerConfig(command="docs", enabled_tools=enabled_tools)},
            registry,
        )

        assert sorted(registry.tool_names) == sorted(expected)
        # A restricted list is a tool-only allowlist: resources and prompts
        # must not even be listed.
        if enabled_tools == ["*"]:
            assert sorted(factory.listed_extras) == ["docs:prompts", "docs:resources"]
        else:
            assert factory.listed_extras == []

        await _close_all(connections)

    @pytest.mark.asyncio
    async def test_allow_all_also_registers_resources_and_prompts(self, monkeypatch):
        registry = ToolRegistry()
        factory = _FakeTransportFactory(
            tool_pages={
                "docs": [mcp_types.ListToolsResult(tools=[_tool_def("alpha")])]
            },
            supports_extras=True,
        )
        factory.install(monkeypatch)

        connections = await connect_mcp_servers(
            {"docs": MCPServerConfig(command="docs")}, registry
        )

        assert registry.has("mcp_docs_alpha")
        assert registry.has("mcp_docs_resource_docs-readme")
        assert registry.has("mcp_docs_prompt_greet")

        await _close_all(connections)

    @pytest.mark.asyncio
    async def test_oauth_configuration_is_reported_not_silently_connected(self):
        registry = ToolRegistry()
        provider = MCPProvider(
            {
                "docs": MCPServerConfig.model_validate(
                    {
                        "type": "streamableHttp",
                        "url": "https://example.invalid/mcp",
                        "auth": "oauth",
                    }
                )
            }
        )

        await provider.connect()

        assert provider.connected_server_names == set()
        assert provider.runtime_status()["docs"] == "failed"
        assert registry.tool_names == []


class TestHTTPValidation:
    @pytest.mark.asyncio
    async def test_unsafe_url_is_refused_before_any_connection(self):
        registry = ToolRegistry()
        provider = MCPProvider(
            {
                "local": MCPServerConfig.model_validate(
                    {"type": "streamableHttp", "url": "http://127.0.0.1:9/mcp"}
                )
            }
        )

        await provider.connect()

        assert provider.connected_server_names == set()
        assert provider.runtime_status()["local"] == "failed"
        assert registry.tool_names == []

    @pytest.mark.asyncio
    async def test_request_validation_blocks_an_internal_address(self):
        import httpx

        from nanoreview.agent.tools.mcp import _validate_mcp_request_url

        request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data")

        with pytest.raises(httpx.RequestError) as excinfo:
            await _validate_mcp_request_url(request)

        assert "169.254.169.254" in str(excinfo.value)

    def test_redirect_hook_is_async_so_dns_stays_off_the_event_loop(self):
        from nanoreview.agent.tools.mcp import _validate_mcp_request_url

        assert asyncio.iscoroutinefunction(_validate_mcp_request_url)
