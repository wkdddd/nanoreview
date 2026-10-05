"""Contract for JSON Schema validation and coercion in ``tools/base.py``.

Union types are the interesting case: the model may legitimately send any
branch, so validation must judge the value against the branch that actually
matched instead of the first listed one. Getting this wrong silently rejects
valid MCP tool arguments (``{"type": ["string", "integer"]}`` rejecting ``7``).
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoreview.agent.tools.base import Schema, Tool


class _FakeTool(Tool):
    """Minimal Tool exposing the coercion helpers."""

    name = "fake"
    description = "fake"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        return None


@pytest.fixture
def tool() -> _FakeTool:
    return _FakeTool()


class TestUnionValidation:
    @pytest.mark.parametrize(
        ("types", "value"),
        [
            (["string", "integer"], "text"),
            (["string", "integer"], 7),
            (["integer", "string"], "text"),
            (["integer", "string"], 7),
            (["string", "boolean"], True),
            (["number", "boolean"], 1.5),
            (["string", "array"], ["a"]),
            (["string", "object"], {"a": 1}),
        ],
    )
    def test_matching_branch_is_accepted_regardless_of_order(
        self, types: list[str], value: Any
    ) -> None:
        schema = {"type": types}

        assert Schema.validate_json_schema_value(value, schema) == []

    @pytest.mark.parametrize(
        ("types", "value"),
        [
            (["string", "integer"], 7.5),
            (["string", "integer"], None),
            (["integer", "boolean"], "text"),
            (["array", "object"], "text"),
        ],
    )
    def test_value_matching_no_branch_is_rejected(
        self, types: list[str], value: Any
    ) -> None:
        schema = {"type": types}

        assert Schema.validate_json_schema_value(value, schema)

    def test_boolean_is_not_accepted_as_integer_branch(self) -> None:
        # ``True`` is an ``int`` in Python but the model asked for an integer.
        assert Schema.validate_json_schema_value(True, {"type": ["integer", "string"]})

    def test_nullable_union_still_accepts_none(self) -> None:
        assert Schema.validate_json_schema_value(None, {"type": ["string", "null"]}) == []

    def test_error_message_lists_the_union_members(self) -> None:
        errors = Schema.validate_json_schema_value(7.5, {"type": ["string", "integer"]})

        assert errors
        assert "string" in errors[0] and "integer" in errors[0]

    def test_union_branch_constraints_apply_to_the_matched_branch(self) -> None:
        # The constraint belongs to the string branch; an integer must not be
        # measured against it, and a short string must be.
        schema = {"type": ["string", "integer"], "minLength": 5}

        assert Schema.validate_json_schema_value(7, schema) == []
        assert Schema.validate_json_schema_value("abc", schema)


class TestUnionCoercion:
    @pytest.mark.parametrize(
        "value",
        ["text", 7, True, 1.5, ["a"], {"a": 1}],
    )
    def test_union_values_are_never_coerced(self, tool: _FakeTool, value: Any) -> None:
        """A union has no preferred target type, so the value stays as sent."""
        schema = {"type": ["string", "integer", "boolean", "number", "array", "object"]}

        assert tool._cast_value(value, schema) == value

    def test_single_type_still_coerces_numeric_strings(self, tool: _FakeTool) -> None:
        assert tool._cast_value("42", {"type": "integer"}) == 42

    def test_nullable_single_type_coerces(self, tool: _FakeTool) -> None:
        assert tool._cast_value("42", {"type": ["integer", "null"]}) == 42


class TestMatchJsonSchemaType:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("text", "string"),
            (7, "integer"),
            (7.5, "number"),
            (True, "boolean"),
            (["a"], "array"),
            ({"a": 1}, "object"),
        ],
    )
    def test_reports_the_matching_member(self, value: Any, expected: str) -> None:
        types = ["string", "integer", "number", "boolean", "array", "object"]

        assert Schema.match_json_schema_type(value, types) == expected

    def test_bool_does_not_match_integer_or_number(self) -> None:
        assert Schema.match_json_schema_type(True, ["integer"]) is None
        assert Schema.match_json_schema_type(True, ["number"]) is None

    def test_integer_matches_before_number(self) -> None:
        # Declaration order wins when several members could match.
        assert Schema.match_json_schema_type(7, ["number", "integer"]) == "number"

    def test_unknown_members_are_ignored(self) -> None:
        assert Schema.match_json_schema_type("x", ["not-a-type", "string"]) == "string"
        assert Schema.match_json_schema_type("x", ["not-a-type"]) is None
