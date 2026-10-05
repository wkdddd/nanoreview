"""Client-side contract for the MCP wrappers and the per-turn proxy."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import types as mcp_types

from nanoreview.agent.tools import mcp as mcp_module
from nanoreview.agent.tools.base import Tool, ToolResult, tool_result_is_error
from nanoreview.agent.tools.mcp import (
    CONNECT_TIMEOUT_SECONDS,
    MCPPromptWrapper,
    MCPProvider,
    MCPResourceWrapper,
    MCPToolProxy,
    MCPToolWrapper,
    _limit_tool_name,
    _mcp_image_tool_result,
    _normalize_schema_for_openai,
    _normalize_windows_stdio_command,
    _redact_url,
    _sanitize_mcp_tool_name,
    _windows_command_basename,
)
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.config.schema import MCPServerConfig


def _tool_def(
    name: str,
    description: str | None = None,
    input_schema: dict | None = None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=input_schema or {"type": "object", "properties": {}},
    )


def _call_result(content: list, is_error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=content, isError=is_error)


class _FakeCallResult:
    def __init__(self, content: list, is_error: bool = False):
        self.content = content
        self.isError = is_error


class _FakeSession:
    """Records calls and replays scripted results/exceptions."""

    def __init__(self, results: list[Any] | None = None):
        self._results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[str] = []

    def _next(self) -> Any:
        if not self._results:
            raise AssertionError("no scripted result left")
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        await asyncio.sleep(0)
        return self._next()

    async def read_resource(self, uri: str) -> Any:
        self.reads.append(str(uri))
        await asyncio.sleep(0)
        return self._next()

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        await asyncio.sleep(0)
        return self._next()


class TestNameHandling:
    def test_illegal_characters_are_replaced_and_runs_collapsed(self):
        assert _sanitize_mcp_tool_name("weird name!!with  spaces") == (
            "weird_name_with_spaces"
        )

    def test_short_names_are_unchanged(self):
        assert _sanitize_mcp_tool_name("mcp_docs_search") == "mcp_docs_search"

    def test_long_names_are_truncated_with_a_stable_hash(self):
        long_name = "mcp_docs_" + "a" * 120

        result = _sanitize_mcp_tool_name(long_name)

        assert len(result) <= 64
        # Truncation is deterministic, so a reconnected server registers the
        # same name and does not orphan an in-flight turn's proxy.
        assert result == _sanitize_mcp_tool_name(long_name)
        assert result != long_name

    def test_limit_tool_name_keeps_short_names(self):
        assert _limit_tool_name("short") == "short"

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("npx", "npx"),
            ("C:\\tools\\node.exe", "node.exe"),
            ("/usr/local/bin/uvx", "uvx"),
            ("server.CMD", "server.cmd"),
        ],
    )
    def test_windows_command_basename(self, command, expected):
        assert _windows_command_basename(command) == expected

    def test_redacted_url_drops_credentials_query_and_path(self):
        redacted = _redact_url("https://user:token@example.com/private/sse?token=abc")

        assert "token" not in redacted
        assert "user" not in redacted
        assert "/private" not in redacted
        assert redacted.startswith("https://example.com")


class TestWindowsStdioLauncher:
    """Windows cannot exec a ``.cmd`` batch file directly.

    The MCP SDK launches stdio servers with a plain ``create_subprocess_exec``,
    so a ``npx``/``.cmd`` command fails on Windows unless it is routed through
    ``COMSPEC /d /c``. These tests patch ``os.name`` so the Windows branch runs
    on every platform.
    """

    @staticmethod
    def _as_windows(monkeypatch: pytest.MonkeyPatch, which: str | None = None):
        monkeypatch.setattr(mcp_module.os, "name", "nt")
        if which is not None:
            monkeypatch.setattr(mcp_module.shutil, "which", lambda *_a, **_k: which)
        monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")

    @pytest.mark.parametrize("command", ["npx", "npm", "pnpm", "yarn", "bunx"])
    def test_shell_launchers_are_wrapped_in_comspec(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ):
        self._as_windows(monkeypatch, which=f"C:\\nodejs\\{command}.cmd")

        resolved, args, _env = _normalize_windows_stdio_command(
            command, ["-y", "some-mcp-server"], None
        )

        assert resolved == r"C:\Windows\system32\cmd.exe"
        assert args == ["/d", "/c", command, "-y", "some-mcp-server"]

    @pytest.mark.parametrize("command", ["server.cmd", "C:\\tools\\server.bat", "x.CMD"])
    def test_batch_scripts_are_wrapped(self, monkeypatch: pytest.MonkeyPatch, command: str):
        self._as_windows(monkeypatch, which=command)

        resolved, args, _env = _normalize_windows_stdio_command(command, [], None)

        assert resolved == r"C:\Windows\system32\cmd.exe"
        assert args == ["/d", "/c", command]

    def test_bare_launcher_resolving_to_cmd_is_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A bare "server" that only exists as server.cmd on PATH still needs
        # the shell, so the resolved name decides too.
        self._as_windows(monkeypatch, which="C:\\tools\\server.cmd")

        resolved, args, _env = _normalize_windows_stdio_command("server", ["--flag"], None)

        assert resolved == r"C:\Windows\system32\cmd.exe"
        assert args == ["/d", "/c", "server", "--flag"]

    @pytest.mark.parametrize(
        "command",
        [
            r"C:\Python312\python.exe",
            r"C:\tools\server.exe",
            "cmd",
            r"C:\Windows\System32\cmd.exe",
            "powershell",
            "pwsh.exe",
        ],
    )
    def test_executables_and_shells_are_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ):
        self._as_windows(monkeypatch, which=command)

        resolved, args, env = _normalize_windows_stdio_command(command, ["a"], {"K": "V"})

        assert resolved == command
        assert args == ["a"]
        assert env == {"K": "V"}

    def test_comspec_comes_from_the_server_env_when_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._as_windows(monkeypatch, which="C:\\nodejs\\npx.cmd")

        resolved, _args, env = _normalize_windows_stdio_command(
            "npx", [], {"COMSPEC": r"D:\custom\cmd.exe", "PATH": "D:\\nodejs"}
        )

        assert resolved == r"D:\custom\cmd.exe"
        assert env == {"COMSPEC": r"D:\custom\cmd.exe", "PATH": "D:\\nodejs"}

    def test_non_windows_platform_is_untouched(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mcp_module.os, "name", "posix")
        monkeypatch.setattr(mcp_module.shutil, "which", lambda *_a, **_k: "/usr/bin/npx")

        resolved, args, env = _normalize_windows_stdio_command("npx", ["-y", "srv"], None)

        assert resolved == "npx"
        assert args == ["-y", "srv"]
        assert env is None

    def test_missing_args_are_normalized_to_a_list(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._as_windows(monkeypatch, which="C:\\nodejs\\npx.cmd")

        _resolved, args, _env = _normalize_windows_stdio_command("npx", None, None)

        assert args == ["/d", "/c", "npx"]


class TestSchemaNormalization:
    def test_nullable_union_collapses_to_single_type(self):
        normalized = _normalize_schema_for_openai(
            {
                "type": "object",
                "properties": {"note": {"type": ["string", "null"]}},
            }
        )

        assert normalized["properties"]["note"]["type"] == "string"
        assert normalized["properties"]["note"]["nullable"] is True

    def test_nullable_anyof_branch_is_flattened(self):
        normalized = _normalize_schema_for_openai(
            {
                "type": "object",
                "properties": {
                    "payload": {"anyOf": [{"type": "object"}, {"type": "null"}]}
                },
            }
        )

        payload = normalized["properties"]["payload"]
        assert payload["type"] == "object"
        assert payload["nullable"] is True
        assert "anyOf" not in payload

    def test_local_ref_is_hoisted_into_defs(self):
        normalized = _normalize_schema_for_openai(
            {
                "type": "object",
                "properties": {"item": {"$ref": "#/$defs/Item"}},
                "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "string"}}}},
            }
        )

        assert normalized["properties"]["item"]["$ref"].startswith("#/$defs/")

    def test_arbitrary_local_pointer_ref_is_hoisted(self):
        normalized = _normalize_schema_for_openai(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/components/schemas/A"}},
                "components": {"schemas": {"A": {"type": "string"}}},
            }
        )

        ref = normalized["properties"]["a"]["$ref"]
        assert ref.startswith("#/$defs/")
        assert normalized["$defs"][ref.rsplit("/", 1)[-1]] == {"type": "string"}

    def test_remote_ref_is_left_alone(self):
        schema = {"type": "object", "properties": {"a": {"$ref": "https://evil/x.json"}}}

        normalized = _normalize_schema_for_openai(schema)

        assert normalized["properties"]["a"]["$ref"] == "https://evil/x.json"

    def test_non_dict_schema_degrades_to_empty_object(self):
        assert _normalize_schema_for_openai("not-a-schema") == {
            "type": "object",
            "properties": {},
        }

    def test_object_schema_gains_properties_and_required(self):
        normalized = _normalize_schema_for_openai({"type": "object"})

        assert normalized["properties"] == {}
        assert normalized["required"] == []


class TestToolWrapperResults:
    def test_text_blocks_are_concatenated(self):
        session = _FakeSession(
            [_FakeCallResult([mcp_types.TextContent(type="text", text="first"), mcp_types.TextContent(type="text", text="second")])]
        )
        wrapper = MCPToolWrapper(session, "docs", _tool_def("search"))

        result = asyncio.run(wrapper.execute(query="x"))

        assert result == "first\nsecond"
        assert session.calls == [("search", {"query": "x"})]

    def test_error_result_is_marked_explicitly(self):
        session = _FakeSession([_FakeCallResult([mcp_types.TextContent(type="text", text="boom")], is_error=True)])
        wrapper = MCPToolWrapper(session, "docs", _tool_def("search"))

        result = asyncio.run(wrapper.execute())

        assert tool_result_is_error(result) is True
        assert "boom" in str(result)

    def test_success_text_starting_with_error_is_not_a_failure(self):
        session = _FakeSession([_FakeCallResult([mcp_types.TextContent(type="text", text="Error: upstream detail")])])
        wrapper = MCPToolWrapper(session, "docs", _tool_def("search"))

        result = asyncio.run(wrapper.execute())

        assert tool_result_is_error(result) is False

    def test_timeout_returns_error_result(self):
        class _SlowSession(_FakeSession):
            async def call_tool(self, name, arguments):
                await asyncio.sleep(5)
                raise AssertionError("should have timed out")

        wrapper = MCPToolWrapper(
            _SlowSession(), "docs", _tool_def("slow"), tool_timeout=1
        )

        result = asyncio.run(wrapper.execute())

        assert tool_result_is_error(result) is True
        assert "timed out" in str(result)

    def test_transient_failure_is_retried_once(self):
        session = _FakeSession(
            [
                ConnectionResetError("peer reset"),
                _FakeCallResult([mcp_types.TextContent(type="text", text="recovered")]),
            ]
        )
        wrapper = MCPToolWrapper(session, "docs", _tool_def("flaky"))

        async def run() -> str:
            return await wrapper.execute()

        result = asyncio.run(_with_patched_sleep(run()))

        assert result == "recovered"
        assert len(session.calls) == 2

    def test_second_transient_failure_gives_up(self):
        session = _FakeSession(
            [ConnectionResetError("first"), ConnectionResetError("second")]
        )
        wrapper = MCPToolWrapper(session, "docs", _tool_def("flaky"))

        async def run() -> str:
            return await wrapper.execute()

        result = asyncio.run(_with_patched_sleep(run()))

        assert tool_result_is_error(result) is True
        assert "after retry" in str(result)

    def test_non_transient_failure_is_not_retried(self):
        session = _FakeSession([ValueError("bad request")])
        wrapper = MCPToolWrapper(session, "docs", _tool_def("search"))

        result = asyncio.run(wrapper.execute())

        assert tool_result_is_error(result) is True
        assert len(session.calls) == 1

    def test_tool_name_is_wrapped_and_sanitized(self):
        wrapper = MCPToolWrapper(
            _FakeSession(), "my docs", _tool_def("search index")
        )

        assert wrapper.name == "mcp_my_docs_search_index"
        assert wrapper.read_only is False


class TestResourceAndPromptWrappers:
    def test_resource_read_returns_text(self):
        resource_def = mcp_types.Resource(
            uri="file:///a.txt", name="readme", description=None
        )
        session = _FakeSession(
            [
                mcp_types.ReadResourceResult(
                    contents=[
                        mcp_types.TextResourceContents(
                            uri="file:///a.txt", text="resource body"
                        )
                    ]
                )
            ]
        )
        wrapper = MCPResourceWrapper(session, "docs", resource_def)

        result = asyncio.run(wrapper.execute())

        assert result == "resource body"
        assert session.reads == ["file:///a.txt"]
        assert wrapper.read_only is True
        assert "readme" in wrapper.description

    def test_binary_resource_is_summarized_not_dumped(self):
        resource_def = mcp_types.Resource(
            uri="file:///a.bin", name="blob", description=None
        )
        session = _FakeSession(
            [
                mcp_types.ReadResourceResult(
                    contents=[
                        mcp_types.BlobResourceContents(
                            uri="file:///a.bin", blob=b"0123456789"
                        )
                    ]
                )
            ]
        )
        wrapper = MCPResourceWrapper(session, "docs", resource_def)

        result = asyncio.run(wrapper.execute())

        assert "Binary resource: 10 bytes" in result

    def test_prompt_arguments_become_parameters(self):
        prompt_def = mcp_types.Prompt(
            name="review",
            description="Review helper",
            arguments=[
                mcp_types.PromptArgument(name="path", description="target", required=True),
                mcp_types.PromptArgument(name="style", required=False),
            ],
        )
        wrapper = MCPPromptWrapper(_FakeSession(), "docs", prompt_def)

        assert wrapper.parameters["required"] == ["path"]
        assert wrapper.parameters["properties"]["path"] == {
            "type": "string",
            "description": "target",
        }
        assert wrapper.read_only is True


class TestToolResultAndRegistryInterop:
    def test_plain_string_result_has_no_explicit_status(self):
        assert tool_result_is_error("Error: plain") is None
        assert tool_result_is_error(ToolResult("Error: flagged")) is False
        assert tool_result_is_error(ToolResult.error("failed")) is True

    def test_registry_prefers_explicit_status_over_text_prefix(self):
        registry = ToolRegistry()
        registry.register(_StubTool("explicit_ok", ToolResult("Error: fine")))
        registry.register(_StubTool("explicit_bad", ToolResult.error("(failed)")))

        ok_result = asyncio.run(registry.execute("explicit_ok", {}))
        bad_result = asyncio.run(registry.execute("explicit_bad", {}))

        assert "[Analyze the error above" not in ok_result
        assert "[Analyze the error above" in bad_result

    def test_registry_keeps_string_prefix_convention_for_plain_tools(self):
        registry = ToolRegistry()
        registry.register(_StubTool("plain", "Error: legacy failure"))

        result = asyncio.run(registry.execute("plain", {}))

        assert "[Analyze the error above" in result


class TestTurnProxy:
    def test_proxy_snapshots_definition_and_delegates_execution(self):
        provider = MCPProvider({})
        session = _FakeSession([_FakeCallResult([mcp_types.TextContent(type="text", text="live")])])
        wrapper = MCPToolWrapper(session, "docs", _tool_def("search", "Search docs"))
        provider.registry.register(wrapper)

        proxy = MCPToolProxy(provider, "docs", wrapper)
        turn_registry = ToolRegistry()
        turn_registry.register(proxy)

        assert proxy.name == wrapper.name
        assert proxy.description == "Search docs"
        assert proxy.parameters == wrapper.parameters
        assert asyncio.run(turn_registry.execute(proxy.name, {"query": "x"})) == "live"
        assert session.calls == [("search", {"query": "x"})]

    def test_proxy_follows_a_replaced_wrapper_after_reconnect(self):
        provider = MCPProvider({})
        stale = MCPToolWrapper(_FakeSession(), "docs", _tool_def("search"))
        provider.registry.register(stale)
        proxy = MCPToolProxy(provider, "docs", stale)

        fresh_session = _FakeSession([_FakeCallResult([mcp_types.TextContent(type="text", text="after reconnect")])])
        provider.registry.register(MCPToolWrapper(fresh_session, "docs", _tool_def("search")))

        result = asyncio.run(proxy.execute(query="x"))

        assert result == "after reconnect"
        assert fresh_session.calls == [("search", {"query": "x"})]

    def test_proxy_reports_missing_connection_instead_of_raising(self):
        provider = MCPProvider({})
        stale = MCPToolWrapper(_FakeSession(), "docs", _tool_def("search"))
        proxy = MCPToolProxy(provider, "docs", stale)
        # Server dropped: nothing live in the registry any more.
        provider.registry.unregister(stale.name)

        result = asyncio.run(proxy.execute())

        assert tool_result_is_error(result) is True
        assert "not connected" in str(result)

    def test_proxy_refuses_a_wrapper_from_another_server(self):
        provider = MCPProvider({})
        wrapper = MCPToolWrapper(_FakeSession(), "docs", _tool_def("search"))
        provider.registry.register(wrapper)
        proxy = MCPToolProxy(provider, "other", wrapper)

        result = asyncio.run(proxy.execute())

        assert tool_result_is_error(result) is True


class TestProviderLifecycle:
    def test_no_configured_servers_means_nothing_to_connect(self):
        provider = MCPProvider({})

        assert provider.configured_server_names == set()
        assert provider.connected_server_names == set()
        assert asyncio.run(provider.connect()) is None
        assert provider.runtime_status() == {}

    def test_aclose_is_idempotent(self):
        provider = MCPProvider({"docs": MCPServerConfig(command="x")})

        asyncio.run(provider.aclose())
        asyncio.run(provider.aclose())

        assert provider.connected_server_names == set()

    def test_runtime_status_only_reports_configured_servers(self):
        provider = MCPProvider({"docs": MCPServerConfig(command="x")})
        provider._runtime_statuses.update({"docs": "failed", "gone": "connected"})

        assert provider.runtime_status() == {"docs": "failed"}

    def test_connect_timeout_is_bounded(self):
        assert CONNECT_TIMEOUT_SECONDS == 30.0


class _StubTool(Tool):
    """Minimal Tool returning a fixed value, for registry-level assertions."""

    _plugin_discoverable = False

    def __init__(self, name: str, result):
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return self._result


async def _with_patched_sleep(coro):
    """Run *coro* with the retry backoff removed to keep tests fast."""
    original = asyncio.sleep

    async def _no_sleep(_delay, *args, **kwargs):
        return await original(0)

    asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        return await coro
    finally:
        asyncio.sleep = original  # type: ignore[assignment]


def test_image_result_payload_keeps_base64_out_of_the_model_context():
    payload = json.loads(
        _mcp_image_tool_result(["done"], [{"path": "generated/a.png"}])
    )

    assert payload["artifacts"] == [{"path": "generated/a.png"}]
    assert payload["text"] == "done"
    # The instruction mentions base64, but no encoded image data is present.
    assert "iVBORw0KGgo" not in json.dumps(payload)
    assert set(payload["artifacts"][0]) == {"path"}
