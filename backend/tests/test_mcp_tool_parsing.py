"""
Regression tests for MCP tool-list parsing and per-server connection reuse.

Root cause #1 — SDK field rename (the recurring "'Tool' object has no attribute
'inputSchema'" bug)
------------------------------------------------------------------------------
``_parse_tools`` used to read ``t.inputSchema`` via direct attribute access.
``pyproject.toml`` only floors the ``mcp`` dependency (``mcp>=1.27.0``, no upper
bound), and the Docker image installed it with plain ``pip install -e .`` — which
reads that loose floor directly and ignores ``uv.lock``'s resolved pin entirely.
``mcp`` 2.0.0 renamed ``Tool``'s Python field from ``inputSchema`` to
``input_schema`` (``inputSchema`` is now only the pydantic serialization alias), so
a Docker rebuild silently picked up 2.0.0 and every ``list_mcp_tools`` call started
raising ``AttributeError: 'Tool' object has no attribute 'inputSchema'`` — with zero
corresponding change in this repo, and no reproduction locally where ``uv run``
stayed pinned to the ``uv.lock``-resolved 1.27.0. The fix reads tools via
``model_dump(by_alias=True)`` instead, which is stable across the rename because it
follows the wire-format alias rather than the SDK's internal field name.

Root cause #2 — duplicate actor/subprocess under concurrent discovery
-----------------------------------------------------------------------
``list_mcp_tools`` is ``parallel_safe`` and dispatched via ``asyncio.gather``. Two
concurrent calls for the *same* server_id used to race in ``_get_actor``'s
check-then-set on ``self._actors``, each spawning its own subprocess; the second
write silently orphaned the first, which ``close_all()`` never tears down. The fix
serializes actor creation per server_id with an ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from typing import Any, Dict, List

import pytest

from mcp_infra.mcp_client import MCPClientManager, _parse_tools
from mcp_infra.registry import MCPServerDef

# ---------------------------------------------------------------------------
# Root cause #1 — SDK field rename
# ---------------------------------------------------------------------------


class _RenamedAttrTool:
    """Simulates mcp>=2.0.0's Tool shape: the real field is ``input_schema``;
    ``inputSchema`` is only the pydantic serialization alias, so accessing it as
    a plain attribute raises AttributeError — exactly like pydantic v2's
    ``BaseModel.__getattr__`` does for a genuinely unknown attribute name.
    """

    def __init__(self, name: str, description: str, input_schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema

    def __getattr__(self, item: str) -> Any:
        raise AttributeError(f"'Tool' object has no attribute {item!r}")

    def model_dump(self, *, by_alias: bool = False, exclude_none: bool = False) -> Dict[str, Any]:
        key = "inputSchema" if by_alias else "input_schema"
        return {"name": self.name, "description": self.description, key: self.input_schema}


class _FakeListToolsResult:
    def __init__(self, tools: List[Any]):
        self.tools = tools


def test_renamed_attr_tool_reproduces_the_reported_error():
    """Sanity check that the fake actually reproduces the exact bug, so the
    regression test below is provably testing the real failure mode."""
    tool = _RenamedAttrTool("search", "Search Drive files", {"type": "object"})
    with pytest.raises(AttributeError, match="inputSchema"):
        _ = tool.inputSchema


def test_parse_tools_survives_sdk_field_rename():
    """_parse_tools must not break when the SDK renames Tool.inputSchema to
    Tool.input_schema — it should read the wire-format alias instead of a
    hardcoded attribute name."""
    tool = _RenamedAttrTool(
        name="search",
        description="Search Drive files",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    parsed = _parse_tools(_FakeListToolsResult([tool]))

    assert parsed == [
        {
            "name": "search",
            "description": "Search Drive files",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]


def test_parse_tools_still_works_with_current_sdk_shape():
    """Backward-compat: a Tool where inputSchema is the real field name (today's
    installed mcp version) must keep working through the same alias-based read."""

    class _CurrentShapeTool:
        def __init__(self, name, description, inputSchema):  # noqa: N803 — mirrors SDK field name
            self.name = name
            self.description = description
            self.inputSchema = inputSchema

        def model_dump(self, *, by_alias=False, exclude_none=False):
            return {
                "name": self.name,
                "description": self.description,
                "inputSchema": self.inputSchema,
            }

    tool = _CurrentShapeTool("list_folder", "List a Drive folder", {"type": "object"})
    parsed = _parse_tools(_FakeListToolsResult([tool]))
    assert parsed == [
        {
            "name": "list_folder",
            "description": "List a Drive folder",
            "inputSchema": {"type": "object"},
        }
    ]


def test_parse_tools_falls_back_to_dict_when_no_model_dump():
    """Defensive fallback for a tool object that isn't a pydantic model at all."""

    class _PlainDictTool(dict):
        pass

    tool = _PlainDictTool(name="ping", description="p", inputSchema={"type": "object"})
    parsed = _parse_tools(_FakeListToolsResult([tool]))
    assert parsed == [{"name": "ping", "description": "p", "inputSchema": {"type": "object"}}]


# ---------------------------------------------------------------------------
# Root cause #2 — duplicate actor/subprocess under concurrent discovery
# ---------------------------------------------------------------------------

# Minimal newline-delimited JSON-RPC MCP server (no node/npx dependency) that
# records one line to __MARKER__ every time it's spawned, so the test can count
# how many subprocesses actually started.
_COUNTING_SERVER = textwrap.dedent(
    """
    import sys, json
    with open(r"{marker}", "a") as f:
        f.write("spawned\\n")
    def send(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        mid, method = m.get("id"), m.get("method")
        if method == "initialize":
            send({{"jsonrpc":"2.0","id":mid,"result":{{
                "protocolVersion":"2024-11-05","capabilities":{{"tools":{{}}}},
                "serverInfo":{{"name":"counting","version":"0.0.1"}}}}}})
        elif method == "tools/list":
            send({{"jsonrpc":"2.0","id":mid,"result":{{"tools":[
                {{"name":"ping","description":"p","inputSchema":{{"type":"object","properties":{{}}}}}}]}}}})
    """
)


def _server_def(script_path: str) -> MCPServerDef:
    return MCPServerDef(
        server_id="counting",
        name="counting",
        description="test",
        transport="stdio",
        stdio_cmd=[sys.executable, script_path],
        required_credentials=[],
        optional_credentials=[],
        auth_style=None,
    )


async def test_concurrent_list_tools_same_server_reuses_one_actor(tmp_path):
    """The exact parallel_safe race: two concurrent list_mcp_tools calls for the
    SAME server_id (asyncio.gather, as the ReAct engine dispatches parallel-safe
    tool calls) must share a single actor/subprocess instead of each spawning
    their own and orphaning one that close_all() never tears down."""
    marker = tmp_path / "spawn_count.txt"
    script = tmp_path / "counting.py"
    script.write_text(_COUNTING_SERVER.format(marker=str(marker)))
    sdef = _server_def(str(script))

    mgr = MCPClientManager()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                mgr.list_tools(sdef, {}),
                mgr.list_tools(sdef, {}),
            ),
            timeout=30,
        )
        assert (
            results[0]
            == results[1]
            == [
                {
                    "name": "ping",
                    "description": "p",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )
        spawn_count = marker.read_text().count("spawned\n") if marker.exists() else 0
        assert spawn_count == 1, f"expected exactly one subprocess spawn, got {spawn_count}"
    finally:
        await mgr.close_all()
