"""What the registered server sends in a tool result besides its text.

Every tool returns JSON text. Left to infer from a `-> str` annotation, `mcp`
also declares an output schema and sends the same text again as
`structuredContent={"result": <text>}`, an escaped copy some clients show the
model in place of the text. These go through `mcp.list_tools` and
`mcp.call_tool`, the layer that builds the result, since the tool functions
themselves return the same string either way.
"""

import asyncio
import importlib
import pkgutil

from withings_mcp import tools
from withings_mcp.mcp_instance import mcp


def _registered_tools():
    for info in pkgutil.iter_modules(tools.__path__, f"{tools.__name__}."):
        importlib.import_module(info.name)
    registered = asyncio.run(mcp.list_tools())
    assert registered, "no tools registered, so nothing here was checked"
    return registered


def test_no_tool_declares_an_output_schema():
    declared = [t.name for t in _registered_tools() if t.output_schema is not None]
    assert not declared, f"these declare an output schema: {declared}"


def test_a_tool_result_is_text_only(monkeypatch, tmp_path):
    # With no client file, `require_auth` answers before any body runs, so
    # every tool is called with no database, sync or network behind it.
    monkeypatch.setattr("withings_mcp.helpers.WITHINGS_CLIENT_PATH", tmp_path / "missing")

    structured = []
    for tool in _registered_tools():
        result = asyncio.run(mcp.call_tool(tool.name, {}))
        assert [block.type for block in result.content] == ["text"], tool.name
        assert "withings-mcp auth" in result.content[0].text, tool.name
        if result.structured_content is not None:
            structured.append(tool.name)
    assert not structured, f"these send structured content: {structured}"
