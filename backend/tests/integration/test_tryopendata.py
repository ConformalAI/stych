"""
TryOpenData MCP test suite.

Live tests use exactly 3 network sessions:
  Session A — connectivity, tool discovery, search, categories
  Session B — dataset info, SQL queries
  Session C — MCPClientManager wrapper (our code)

Benchmark tests (marked with @benchmark) measure download throughput.
Run with: pytest tests/mcp_servers/test_tryopendata.py -v -m benchmark

Unit tests (credential store, registry, agent dispatch) make zero network calls.

Run:
    cd backend
    /path/to/venv/bin/python3 -m pytest tests/mcp_servers/test_tryopendata.py -v

TRYOPENDATA_API_KEY must be set (in .env or environment).
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("TRYOPENDATA_API_KEY", "")
MCP_URL = "https://mcp.tryopendata.ai/mcp"

live = pytest.mark.skipif(not API_KEY, reason="TRYOPENDATA_API_KEY not set")
benchmark = pytest.mark.benchmark

EXPECTED_TOOLS = {
    "search_datasets",
    "list_categories",
    "get_dataset_info",
    "find_related_datasets",
    "query_dataset",
    "query_sql",
    "openchart",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _session(api_key: str = API_KEY):
    """Open a single Streamable HTTP MCP session."""
    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={"X-API-Key": api_key}, timeout=30) as http:
        async with streamable_http_client(MCP_URL, http_client=http) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s


def _text(result: Any) -> str:
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


# ===========================================================================
# Session A — connectivity + tool discovery + search + categories
# ===========================================================================


@live
async def test_discovery_search_categories():
    """Single session: handshake → tool list → search → categories."""
    async with _session() as s:
        # --- tool discovery ---
        tools = await s.list_tools()
        names = {t.name for t in tools.tools}
        missing = EXPECTED_TOOLS - names
        assert not missing, f"Missing tools: {missing}"

        # schemas look correct
        by_name = {t.name: t for t in tools.tools}
        assert "sql" in by_name["query_sql"].inputSchema.get("required", [])
        assert "queries" in by_name["search_datasets"].inputSchema.get("required", [])
        assert "provider" in by_name["get_dataset_info"].inputSchema.get("required", [])
        assert "dataset" in by_name["get_dataset_info"].inputSchema.get("required", [])

        # --- search ---
        r = await s.call_tool("search_datasets", {"queries": "US population by state"})
        assert len(_text(r)) > 50

        # batch query
        r = await s.call_tool(
            "search_datasets",
            {"queries": ["GDP per capita", "inflation rate"], "limit_per_query": 3},
        )
        assert _text(r)

        # --- categories ---
        r = await s.call_tool("list_categories", {})
        cat_text = _text(r).lower()
        found = [c for c in ["economics", "health", "education", "climate"] if c in cat_text]
        assert len(found) >= 2, f"Too few known categories: {cat_text[:200]}"


# ===========================================================================
# Session B — dataset info + SQL
# ===========================================================================


@live
async def test_dataset_info_and_sql():
    """Single session: metadata lookup → SQL queries."""
    async with _session() as s:
        # --- dataset info ---
        r = await s.call_tool(
            "get_dataset_info", {"provider": "worldbank", "dataset": "population"}
        )
        text = _text(r)
        assert len(text) > 50
        assert any(kw in text.lower() for kw in ["column", "country", "population", "year", "rows"])

        # --- basic SQL ---
        r = await s.call_tool(
            "query_sql",
            {
                "sql": (
                    "SELECT country_name, population "
                    'FROM "worldbank/population" '
                    "WHERE year = 2022 ORDER BY population DESC LIMIT 5"
                )
            },
        )
        text = _text(r)
        assert len(text) > 20  # got actual row data back

        # --- aggregation ---
        r = await s.call_tool(
            "query_sql",
            {
                "sql": (
                    "SELECT year, AVG(population) AS avg_pop "
                    'FROM "worldbank/population" '
                    "WHERE year >= 2015 GROUP BY year ORDER BY year"
                )
            },
        )
        assert _text(r)

        # --- invalid table doesn't crash ---
        r = await s.call_tool("query_sql", {"sql": "SELECT * FROM nonexistent_table_xyz LIMIT 1"})
        assert r.content is not None


# ===========================================================================
# Session C — MCPClientManager (our wrapper code)
# ===========================================================================


@live
async def test_mcp_client_manager():
    """Single manager instance: list_tools, call_tool, truncation guard."""
    from mcp_infra.mcp_client import MAX_RESULT_CHARS, MCPClientManager
    from mcp_infra.registry import MCP_REGISTRY

    creds = {"api_key": API_KEY}
    mgr = MCPClientManager()
    try:
        # list_tools returns well-formed list
        tools = await mgr.list_tools(MCP_REGISTRY["opendata"], creds)
        assert isinstance(tools, list)
        names = {t["name"] for t in tools}
        assert "search_datasets" in names
        assert "query_sql" in names

        # call_tool returns a string
        result = await mgr.call_tool(
            MCP_REGISTRY["opendata"],
            "search_datasets",
            {"queries": "unemployment rate"},
            creds,
        )
        assert isinstance(result, str) and len(result) > 20

        # truncation: large result gets the "truncated" marker if over limit
        result2 = await mgr.call_tool(
            MCP_REGISTRY["opendata"],
            "query_sql",
            {"sql": 'SELECT * FROM "worldbank/population" LIMIT 200'},
            creds,
        )
        if len(result2) >= MAX_RESULT_CHARS:
            assert "truncated" in result2.lower()

    finally:
        await mgr.close_all()


# ===========================================================================
# Unit tests — zero network calls (always run)
# ===========================================================================


class TestCredentialStore:
    def test_fernet_key_is_deterministic(self):
        from helpers.secrets_crypto import fernet_key

        assert fernet_key() == fernet_key()

    def test_encrypt_decrypt_roundtrip(self):
        import json

        from agents.integration.credential_store import _fernet

        f = _fernet()
        original = {"api_key": "od_live_test123"}
        encrypted = f.encrypt(json.dumps(original).encode()).decode()
        decrypted = json.loads(f.decrypt(encrypted.encode()).decode())
        assert decrypted == original

    def test_empty_creds_stored_as_null(self):
        import json

        from agents.integration.credential_store import _fernet

        creds: dict = {}
        result = _fernet().encrypt(json.dumps(creds).encode()).decode() if creds else None
        assert result is None


class TestRegistry:
    def test_opendata_transport_is_http(self):
        from mcp_infra.registry import MCP_REGISTRY

        assert MCP_REGISTRY["opendata"].transport == "http"

    def test_opendata_url_uses_mcp_endpoint(self):
        from mcp_infra.registry import MCP_REGISTRY

        url = MCP_REGISTRY["opendata"].http_url or ""
        assert url.endswith("/mcp"), f"Expected /mcp endpoint, got: {url}"
        assert "/sse" not in url

    def test_opendata_api_key_is_required(self):
        from mcp_infra.registry import MCP_REGISTRY

        assert "api_key" in MCP_REGISTRY["opendata"].required_credentials

    def test_google_sheets_uses_stdio(self):
        from mcp_infra.registry import MCP_REGISTRY

        defn = MCP_REGISTRY["google_sheets"]
        assert defn.transport == "stdio"
        assert defn.stdio_cmd

    def test_all_entries_have_required_fields(self):
        from mcp_infra.registry import MCP_REGISTRY

        for server_id, defn in MCP_REGISTRY.items():
            assert defn.server_id == server_id
            assert defn.name
            assert defn.description
            assert defn.transport in ("http", "http_sse", "stdio")


class TestAgentDispatch:
    async def test_unknown_server_returns_error(self):
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="list_mcp_tools",
                inp={"server_id": "nonexistent_xyz"},
                creds_map={},
                mcp_client=mgr,
            )
            assert "error" in result.lower() or "unknown" in result.lower()
        finally:
            await mgr.close_all()

    async def test_disconnected_server_returns_friendly_error(self):
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="call_mcp_tool",
                inp={"server_id": "opendata", "tool_name": "search_datasets", "arguments": {}},
                creds_map={},  # empty → not connected
                mcp_client=mgr,
            )
            assert "not connected" in result.lower() or "settings" in result.lower()
        finally:
            await mgr.close_all()

    async def test_list_servers_no_creds_all_disconnected(self):
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="list_mcp_servers", inp={}, creds_map={}, mcp_client=mgr
            )
            assert "not_connected" in result
        finally:
            await mgr.close_all()

    async def test_list_servers_with_creds_shows_connected(self):
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="list_mcp_servers",
                inp={},
                creds_map={"opendata": {"api_key": "any_value"}},
                mcp_client=mgr,
            )
            opendata_line = next((line for line in result.splitlines() if "opendata" in line), "")
            assert opendata_line
            assert "not_connected" not in opendata_line
        finally:
            await mgr.close_all()

    async def test_list_servers_empty_creds_shows_missing_credentials(self):
        """Row exists (so creds_map has the key) but api_key is absent — should show
        connected_missing_credentials, not connected."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="list_mcp_servers",
                inp={},
                creds_map={"opendata": {}},  # row exists but no api_key
                mcp_client=mgr,
            )
            opendata_line = next((line for line in result.splitlines() if "opendata" in line), "")
            assert "connected_missing_credentials" in opendata_line
        finally:
            await mgr.close_all()

    async def test_list_tools_empty_creds_returns_friendly_error(self):
        """list_mcp_tools with connected-but-no-api_key returns an actionable error,
        not a raw TaskGroup/ExceptionGroup traceback."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            result = await _execute_tool(
                action="list_mcp_tools",
                inp={"server_id": "opendata"},
                creds_map={"opendata": {}},  # connected but api_key missing
                mcp_client=mgr,
            )
            assert "missing required credentials" in result.lower()
            assert "taskgroup" not in result.lower()
            assert "exceptiongroup" not in result.lower()
        finally:
            await mgr.close_all()


# ===========================================================================
# Benchmark — download throughput (requires TRYOPENDATA_API_KEY)
#
# Run: pytest tests/mcp_servers/test_tryopendata.py -v -m benchmark -s
# ===========================================================================


@live
@benchmark
async def test_rest_api_download_mid_size(tmp_path: Path):
    """
    Benchmark REST API path: worldbank/population (~17k rows).
    Primary path — no MCP pagination involved.
    """
    from tools.opendata_tools import _fetch_opendata_to_workspace_impl

    credentials = {"api_key": API_KEY}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    t0 = time.perf_counter()
    result = await _fetch_opendata_to_workspace_impl(
        sql="SELECT * FROM worldbank/population",
        filename="bench_population.csv",
        workspace=workspace,
        credentials=credentials,
        state=None,
    )
    elapsed = time.perf_counter() - t0

    assert not result.startswith("Error"), f"Fetch failed: {result}"
    csv_file = workspace / "bench_population.csv"
    assert csv_file.exists(), "CSV file was not created"

    file_size_mb = csv_file.stat().st_size / (1024 * 1024)
    rows_match = __import__("re").search(r"([\d,]+) rows", result)
    row_count = int(rows_match.group(1).replace(",", "")) if rows_match else 0
    rows_per_sec = row_count / elapsed if elapsed > 0 else 0
    mbps = file_size_mb / elapsed if elapsed > 0 else 0

    print(
        f"\n[benchmark] worldbank/population (REST): {row_count:,} rows, "
        f"{file_size_mb:.2f} MB in {elapsed:.2f}s = {rows_per_sec:,.0f} rows/s, {mbps:.3f} MB/s"
    )
    assert row_count >= 10_000, f"Expected ≥10k rows, got {row_count}"


@live
@benchmark
async def test_rest_api_download_with_filter(tmp_path: Path):
    """
    Benchmark REST API + SQLite filter: full dataset downloaded then filtered locally.
    """
    from tools.opendata_tools import _fetch_opendata_to_workspace_impl

    credentials = {"api_key": API_KEY}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    t0 = time.perf_counter()
    result = await _fetch_opendata_to_workspace_impl(
        sql="SELECT * FROM worldbank/population WHERE year >= 2000 ORDER BY population DESC",
        filename="bench_filtered.csv",
        workspace=workspace,
        credentials=credentials,
        state=None,
    )
    elapsed = time.perf_counter() - t0

    assert not result.startswith("Error"), f"Fetch failed: {result}"
    csv_file = workspace / "bench_filtered.csv"
    assert csv_file.exists(), "CSV file was not created"

    rows_match = __import__("re").search(r"([\d,]+) rows", result)
    row_count = int(rows_match.group(1).replace(",", "")) if rows_match else 0
    print(
        f"\n[benchmark] worldbank/population WHERE year>=2000 (REST+filter): "
        f"{row_count:,} rows in {elapsed:.2f}s"
    )
    assert row_count > 0, "No rows returned after filter"


@live
@benchmark
async def test_rest_api_download_large(tmp_path: Path):
    """
    Benchmark REST API for a 100k+ row dataset: noaa/hurricanes (~127k rows, ~37MB).
    This is the key test for the 100k+ row goal.
    """
    from tools.opendata_tools import _fetch_opendata_to_workspace_impl

    credentials = {"api_key": API_KEY}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    t0 = time.perf_counter()
    result = await _fetch_opendata_to_workspace_impl(
        sql="SELECT * FROM noaa/hurricanes",
        filename="bench_hurricanes.csv",
        workspace=workspace,
        credentials=credentials,
        state=None,
    )
    elapsed = time.perf_counter() - t0

    assert not result.startswith("Error"), f"Fetch failed: {result}"
    csv_file = workspace / "bench_hurricanes.csv"
    assert csv_file.exists(), "CSV file was not created"

    file_size_mb = csv_file.stat().st_size / (1024 * 1024)
    rows_match = __import__("re").search(r"([\d,]+) rows", result)
    row_count = int(rows_match.group(1).replace(",", "")) if rows_match else 0
    rows_per_sec = row_count / elapsed if elapsed > 0 else 0
    mbps = file_size_mb / elapsed if elapsed > 0 else 0

    print(
        f"\n[benchmark] noaa/hurricanes (REST, 100k+ rows): {row_count:,} rows, "
        f"{file_size_mb:.2f} MB in {elapsed:.2f}s = {rows_per_sec:,.0f} rows/s, {mbps:.3f} MB/s"
    )
    # Baseline: a ~37MB CSV download at even 1 MB/s (slow internet) takes 37s.
    # The REST API should comfortably beat that.
    assert row_count >= 100_000, f"Expected ≥100k rows, got {row_count}"
    assert elapsed < 120, f"Download took {elapsed:.0f}s — exceeded 2-minute limit"
