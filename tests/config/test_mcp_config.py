"""Configuration contract for ``tools.mcpServers``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nanoreview.config.loader import resolve_config_env_vars
from nanoreview.config.schema import Config, MCPServerConfig, ToolsConfig


def _tools_config(**kwargs) -> ToolsConfig:
    return ToolsConfig(**kwargs)


class TestMCPServerConfig:
    def test_defaults_are_off_and_fully_allowed(self):
        cfg = ToolsConfig()

        assert cfg.mcp_servers == {}

        server = MCPServerConfig()
        assert server.type is None
        assert server.auth is None
        assert server.command == ""
        assert server.args == []
        assert server.env == {}
        assert server.cwd == ""
        assert server.url == ""
        assert server.headers == {}
        assert server.tool_timeout == 30
        assert server.enabled_tools == ["*"]

    def test_camel_case_keys_are_accepted(self):
        server = MCPServerConfig.model_validate(
            {
                "type": "streamableHttp",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer abc"},
                "toolTimeout": 90,
                "enabledTools": ["search"],
            }
        )

        assert server.type == "streamableHttp"
        assert server.tool_timeout == 90
        assert server.enabled_tools == ["search"]
        assert server.headers == {"Authorization": "Bearer abc"}

    def test_snake_case_keys_are_accepted(self):
        server = MCPServerConfig.model_validate(
            {"type": "stdio", "command": "npx", "tool_timeout": 5, "enabled_tools": []}
        )

        assert server.tool_timeout == 5
        assert server.enabled_tools == []

    def test_serialization_uses_camel_case(self):
        cfg = _tools_config(
            mcp_servers={
                "docs": {
                    "command": "npx",
                    "args": ["-y", "server"],
                    "env": {"TOKEN": "x"},
                    "toolTimeout": 12,
                    "enabledTools": ["*"],
                }
            }
        )

        dumped = cfg.model_dump(by_alias=True, exclude_none=True)["mcpServers"]["docs"]

        assert dumped["toolTimeout"] == 12
        assert dumped["enabledTools"] == ["*"]
        assert dumped["command"] == "npx"

    def test_env_and_header_keys_keep_their_original_spelling(self):
        cfg = _tools_config(
            mcp_servers={
                "docs": {
                    "command": "server",
                    "env": {"Mixed_Case": "1", "lower": "2"},
                    "headers": {"X-Trace-Id": "abc", "authorization": "Bearer t"},
                }
            }
        )

        server = cfg.mcp_servers["docs"]

        assert server.env == {"Mixed_Case": "1", "lower": "2"}
        assert server.headers == {"X-Trace-Id": "abc", "authorization": "Bearer t"}
        # Round-tripping must not normalize the dictionary keys.
        assert MCPServerConfig.model_validate_json(server.model_dump_json()).env == server.env

    def test_round_trip_through_json(self):
        cfg = _tools_config(
            mcp_servers={"remote": {"type": "sse", "url": "https://host/sse"}}
        )

        raw = json.loads(cfg.model_dump_json(by_alias=True))
        restored = ToolsConfig.model_validate(raw)

        assert restored.mcp_servers["remote"].type == "sse"
        assert restored.mcp_servers["remote"].url == "https://host/sse"

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "carrier-pigeon"},
            {"type": "stdio", "toolTimeout": "soon"},
            {"type": "sse", "enabledTools": "search"},
            {"type": "sse", "headers": ["Authorization"]},
        ],
    )
    def test_invalid_config_is_rejected(self, payload):
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate(payload)

    def test_unknown_transport_is_rejected(self):
        with pytest.raises(ValidationError):
            ToolsConfig.model_validate({"mcpServers": {"x": {"type": "grpc"}}})

    def test_auth_oauth_parses_but_is_rejected_at_connect_time(self):
        # The field exists so unsupported configuration fails loudly during
        # connection setup instead of silently connecting without credentials.
        server = MCPServerConfig.model_validate(
            {"type": "streamableHttp", "url": "https://host/mcp", "auth": "oauth"}
        )

        assert server.auth == "oauth"

    def test_unknown_auth_value_is_rejected(self):
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate({"type": "sse", "url": "https://h/sse", "auth": "basic"})


class TestMCPEnvVarResolution:
    def test_env_references_resolve_in_command_and_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MCP_TOKEN", "secret-value")
        cfg = Config.model_validate(
            {
                "tools": {
                    "mcpServers": {
                        "docs": {
                            "command": "${MCP_TOKEN}",
                            "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                            "env": {"STATIC": "kept"},
                        }
                    }
                }
            }
        )

        resolved = resolve_config_env_vars(cfg)
        server = resolved.tools.mcp_servers["docs"]

        assert server.command == "secret-value"
        assert server.headers == {"Authorization": "Bearer secret-value"}
        # Dict keys are never rewritten, only values.
        assert server.env == {"STATIC": "kept"}

    def test_missing_env_reference_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("MCP_ABSENT", raising=False)
        cfg = Config.model_validate(
            {"tools": {"mcpServers": {"docs": {"command": "${MCP_ABSENT}"}}}}
        )

        with pytest.raises(ValueError, match="MCP_ABSENT"):
            resolve_config_env_vars(cfg)
