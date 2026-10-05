import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def test_stdio_tool_discovery_without_browser():
    async with Client(
        StdioServerParameters(command=sys.executable, args=["-m", "forms_mcp.cli"])
    ) as client:
        result = await client.list_tools()
        tools = {tool.name: tool for tool in result.tools}
        assert len(tools) == 19
        for name in (
            "forms_list",
            "forms_inspect",
            "forms_verify",
            "forms_fleet_plan",
            "forms_workbook_status",
        ):
            assert tools[name].annotations.read_only_hint is True
        for name in (
            "forms_delete_question",
            "forms_delete_section",
            "forms_delete_form",
            "forms_fleet_apply",
        ):
            assert tools[name].annotations.destructive_hint is True
            assert tools[name].annotations.idempotent_hint is False
        assert "ctx" not in tools["forms_delete_question"].input_schema.get("properties", {})
