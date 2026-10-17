"""
Regression tests for MCP cached-transport (stdio/sse) teardown isolation.

Root cause these guard against
------------------------------
anyio cancel scopes are task-affine. The MCP stdio/sse transport pushed its cancel
scopes onto the *caller's* task and kept them there across the whole agent turn.
When the subprocess teardown misbehaved (a lingering process → cancelled
``_process.wait()``), the half-exited, still-cancelled scope was left on the caller's
task and anyio re-delivered ``CancelledError`` at the *next* checkpoint — cancelling
the following LLM/httpx call ("interrupted unexpectedly"), even though the tool call
had already succeeded.

The fix runs each session's whole lifecycle (connect → calls → teardown) on a
dedicated ``_SessionActor`` task, so a bad teardown can only cancel that dying task —
never the caller. These tests use a REAL python stdio MCP subprocess (no node/npx
dependency) that lingers on stdin close, driven through ``MCPClientManager`` under
``asyncio.wait_for`` exactly like the production request worker.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from mcp_infra.mcp_client import MCPClientManager
from mcp_infra.registry import MCPServerDef

# Minimal newline-delimited JSON-RPC MCP server. On stdin EOF it LINGERS instead of
# exiting, which forces anyio's teardown ``await _process.wait()`` to be cancelled by
# the closing task-group scope — the exact production condition.
_LINGER_SERVER = textwrap.dedent(
    """
    import sys, json, time
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
            send({"jsonrpc":"2.0","id":mid,"result":{
                "protocolVersion":"2024-11-05","capabilities":{"tools":{}},
                "serverInfo":{"name":"linger","version":"0.0.1"}}})
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
                {"name":"ping","description":"p","inputSchema":{"type":"object","properties":{}}}]}})
        elif method == "tools/call":
            send({"jsonrpc":"2.0","id":mid,"result":{
                "content":[{"type":"text","text":"pong"}],"isError":False}})
    time.sleep(30)  # linger on EOF
    """
)

# Exits immediately without speaking MCP → the handshake (initialize) fails.
_QUICK_EXIT_SERVER = "import sys; sys.exit(0)"


def _server_def(script_path: str) -> MCPServerDef:
    return MCPServerDef(
        server_id="linger",
        name="linger",
        description="test",
        transport="stdio",
        stdio_cmd=[sys.executable, script_path],
        required_credentials=[],
        optional_credentials=[],
        auth_style=None,
    )


async def test_teardown_cancel_does_not_poison_caller(tmp_path):
    """The core guarantee: after close_all tears down a lingering subprocess, the
    CALLER task is still usable — no leaked CancelledError on the next await."""
    script = tmp_path / "linger.py"
    script.write_text(_LINGER_SERVER)
    sdef = _server_def(str(script))

    async def do_work() -> str:
        mgr = MCPClientManager()
        try:
            tools = await mgr.list_tools(sdef, {})
            assert [t["name"] for t in tools] == ["ping"]
            res = await mgr.call_tool(sdef, "ping", {}, {})
            assert "pong" in res
        finally:
            await mgr.close_all()  # teardown cancellation happens INSIDE the actor task
        await asyncio.sleep(0.02)  # caller must still be usable
        return "ok"

    # Mirror the production request worker: the whole turn runs under wait_for.
    out = await asyncio.wait_for(do_work(), timeout=60)
    assert out == "ok"


async def test_caller_can_run_more_awaits_after_close(tmp_path):
    """Proves the specific failure: a follow-up 'LLM call' (await) after the tool
    call + close_all completes cleanly instead of being cancelled."""
    script = tmp_path / "linger.py"
    script.write_text(_LINGER_SERVER)
    sdef = _server_def(str(script))

    mgr = MCPClientManager()
    await mgr.call_tool(sdef, "ping", {}, {})
    await mgr.close_all()

    # Simulate the next step in the ReAct loop touching anyio again.
    for _ in range(5):
        await asyncio.sleep(0)  # anyio checkpoint equivalent — must not raise
    assert asyncio.current_task().cancelling() == 0


async def test_failed_handshake_surfaces_and_leaves_caller_clean(tmp_path):
    """A connect/handshake failure is surfaced to the caller as a normal exception
    (not a stray CancelledError), and the caller task stays usable afterward."""
    script = tmp_path / "quick.py"
    script.write_text(_QUICK_EXIT_SERVER)
    sdef = _server_def(str(script))

    mgr = MCPClientManager()
    with pytest.raises(Exception):  # noqa: B017 — McpError/RuntimeError, never CancelledError
        await mgr.list_tools(sdef, {})

    await mgr.close_all()
    await asyncio.sleep(0.02)  # caller still usable
    assert asyncio.current_task().cancelling() == 0
