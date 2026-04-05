"""Tests for tool definition types and JSON Schema conversion."""

from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)


def test_empty_tool_schema() -> None:
    tool = ToolDefinition(name="noop", description="Does nothing")
    schema = tool.to_json_schema()
    assert schema == {"type": "object", "properties": {}}


def test_required_params_in_schema() -> None:
    tool = ToolDefinition(
        name="greet",
        description="Say hello",
        parameters=[
            ToolParameter(
                name="name",
                type=ToolParameterType.STRING,
                description="Who to greet",
                required=True,
            ),
        ],
    )
    schema = tool.to_json_schema()
    assert schema["properties"]["name"] == {
        "type": "string",
        "description": "Who to greet",
    }
    assert schema["required"] == ["name"]


def test_optional_param_not_in_required() -> None:
    tool = ToolDefinition(
        name="search",
        description="Search things",
        parameters=[
            ToolParameter(
                name="query",
                type=ToolParameterType.STRING,
                description="Search query",
                required=True,
            ),
            ToolParameter(
                name="limit",
                type=ToolParameterType.INTEGER,
                description="Max results",
                required=False,
            ),
        ],
    )
    schema = tool.to_json_schema()
    assert schema["required"] == ["query"]
    assert "limit" in schema["properties"]


def test_enum_in_schema() -> None:
    tool = ToolDefinition(
        name="set_mode",
        description="Set a mode",
        parameters=[
            ToolParameter(
                name="mode",
                type=ToolParameterType.STRING,
                description="The mode",
                enum=["fast", "slow", "auto"],
            ),
        ],
    )
    schema = tool.to_json_schema()
    assert schema["properties"]["mode"]["enum"] == ["fast", "slow", "auto"]


def test_multiple_types() -> None:
    tool = ToolDefinition(
        name="configure",
        description="Set config",
        parameters=[
            ToolParameter(name="name", type=ToolParameterType.STRING, description="Name"),
            ToolParameter(name="count", type=ToolParameterType.INTEGER, description="Count"),
            ToolParameter(name="ratio", type=ToolParameterType.NUMBER, description="Ratio"),
            ToolParameter(name="enabled", type=ToolParameterType.BOOLEAN, description="On/off"),
            ToolParameter(name="tags", type=ToolParameterType.ARRAY, description="Tags"),
            ToolParameter(name="meta", type=ToolParameterType.OBJECT, description="Metadata"),
        ],
    )
    schema = tool.to_json_schema()
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["ratio"]["type"] == "number"
    assert schema["properties"]["enabled"]["type"] == "boolean"
    assert schema["properties"]["tags"]["type"] == "array"
    assert schema["properties"]["meta"]["type"] == "object"
    assert len(schema["required"]) == 6
