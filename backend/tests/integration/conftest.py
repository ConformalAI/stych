"""
Shared fixtures for MCP integration test suites.

These tests make REAL network calls to external MCP servers.
They require valid API keys set in the environment.

Run only the MCP tests:
    pytest tests/mcp/ -v

Skip if no key is present (CI-safe):
    pytest tests/mcp/ -v -m "not live"
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env so tests pick up keys when run locally without the server running.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env"))


# ---------------------------------------------------------------------------
# Shared async MCP client helper
# ---------------------------------------------------------------------------


async def mcp_session(url: str, headers: dict):
    """
    Async context manager that yields an initialized MCP ClientSession.

    Usage::
        async with mcp_session(url, headers) as session:
            result = await session.list_tools()
    """
    from contextlib import asynccontextmanager

    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    @asynccontextmanager
    async def _inner():
        async with httpx.AsyncClient(headers=headers, timeout=30) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session

    return _inner()
