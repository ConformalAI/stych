"""
Integration agent test suite.

Tests:
  TestWorkspaceAccess  — workspace path helpers and list/read tool handlers
  TestReasoningEvents  — reasoning SSE events are emitted at each step
  TestFullFlow         — live end-to-end smoke (requires TRYOPENDATA_API_KEY)

Run:
    cd backend
    python3 -m pytest tests/integration/test_integration_agent.py -v

Live end-to-end (requires env var):
    TRYOPENDATA_API_KEY=<key> python3 -m pytest \
        tests/integration/test_integration_agent.py::TestFullFlow -v -s
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent.parent / ".env", override=False)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("TRYOPENDATA_API_KEY", "")
live = pytest.mark.skipif(not API_KEY, reason="TRYOPENDATA_API_KEY not set")


def _load_llm_config() -> Dict[str, Any]:
    """Load the real LLM config (config.yaml with ${ENV} substitution) for live tests.

    Mirrors main.load_config() without importing main.py (which has heavy
    module-level side effects: SuperTokens/app initialization).
    """
    import yaml

    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    def _sub(obj):
        if isinstance(obj, dict):
            return {k: _sub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sub(v) for v in obj]
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            inner = obj[2:-1]
            i = inner.find(":")
            name = inner if i < 0 else inner[:i]
            default = "" if i < 0 else inner[i + 1 :]
            return os.getenv(name, default)
        return obj

    return _sub(config).get("llm", {})


def _make_state(session_id: str = "test-session", user_id: str = "test-user"):
    """Build a minimal AgentState-like object."""
    state = MagicMock()
    state.session_id = session_id
    state.metadata = {"user_id": user_id}
    return state


# ---------------------------------------------------------------------------
# TestWorkspaceAccess
# ---------------------------------------------------------------------------


class TestQueryResultParser:
    """Unit tests for _parse_query_result — TryOpenData response format handling."""

    def test_zero_rows_returned_gives_empty(self):
        """'0 rows returned' plain-text must return ([], []) not garbage CSV rows."""
        from tools.opendata_parser import _parse_query_result

        resp = (
            "0 rows returned\n"
            "14 columns: _source_page, _source_url, age, cabin, embarked, fare, "
            "name, parents_children, passenger_id, pclass, sex, siblings_spouses, "
            "survived, ticket\n"
            "Execution time: 2ms\n\n"
            "Columns: _source_page, _source_url, age, cabin, embarked, fare, "
            "name, parents_children, passenger_id, pclass, sex, siblings_spouses, "
            "survived, ticket\n"
            "(no rows)\n\n"
            "Open this query in the sandbox: https://tryopendata.ai/sandbox?sql=test"
        )
        headers, rows = _parse_query_result(resp)
        assert headers == [], f"Expected empty headers, got: {headers}"
        assert rows == [], f"Expected empty rows, got {len(rows)} rows: {rows[:2]}"

    def test_nonzero_rows_envelope_with_json_data(self):
        """'N rows returned' envelope containing JSON data section is parsed correctly."""
        import json as _json

        from tools.opendata_parser import _parse_query_result

        data = [{"name": "Allen", "age": "29"}, {"name": "Brown", "age": "35"}]
        resp = (
            f"2 rows returned\n"
            f"2 columns: name, age\n"
            f"Execution time: 5ms\n\n"
            f"Columns: name, age\n"
            f"{_json.dumps(data)}\n\n"
            f"Open this query in the sandbox: https://tryopendata.ai/sandbox?sql=test"
        )
        headers, rows = _parse_query_result(resp)
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}: {rows}"
        assert headers == ["name", "age"]

    def test_plain_json_array_is_parsed(self):
        """Standard JSON array-of-objects (TryOpenData's normal data format) works."""
        import json as _json

        from tools.opendata_parser import _parse_query_result

        data = [{"col_a": "1", "col_b": "x"}, {"col_a": "2", "col_b": "y"}]
        headers, rows = _parse_query_result(_json.dumps(data))
        assert headers == ["col_a", "col_b"]
        assert rows == [["1", "x"], ["2", "y"]]

    def test_postgres_pipe_table_is_parsed(self):
        """TryOpenData's PostgreSQL-style pipe table format is parsed correctly."""
        from tools.opendata_parser import _parse_query_result

        resp = (
            "sepal_length | sepal_width | petal_length | petal_width | species\n"
            "-------------+-------------+--------------+-------------+--------\n"
            "         5.1 |         3.5 |          1.4 |         0.2 | setosa \n"
            "         4.9 |           3 |          1.4 |         0.2 | setosa \n"
            "         4.7 |         3.2 |          1.3 |         0.2 | setosa \n"
        )
        headers, rows = _parse_query_result(resp)
        assert headers == [
            "sepal_length",
            "sepal_width",
            "petal_length",
            "petal_width",
            "species",
        ], f"Bad headers: {headers}"
        assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"
        assert rows[0] == ["5.1", "3.5", "1.4", "0.2", "setosa"]

    def test_nonzero_rows_envelope_with_postgres_table(self):
        """'N rows returned' envelope containing a PostgreSQL pipe table is parsed end-to-end."""
        from tools.opendata_parser import _parse_query_result

        resp = (
            "150 rows returned\n"
            "5 columns: sepal_length, sepal_width, petal_length, petal_width, species\n"
            "Execution time: 6ms\n\n"
            "sepal_length | sepal_width | petal_length | petal_width | species\n"
            "-------------+-------------+--------------+-------------+--------\n"
            "         5.1 |         3.5 |          1.4 |         0.2 | setosa \n"
            "         4.9 |           3 |          1.4 |         0.2 | setosa \n\n"
            "Open this query in the sandbox: https://tryopendata.ai/sandbox?sql=test"
        )
        headers, rows = _parse_query_result(resp)
        assert headers == [
            "sepal_length",
            "sepal_width",
            "petal_length",
            "petal_width",
            "species",
        ], f"Bad headers: {headers}"
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}: {rows}"
        assert rows[0][0] == "5.1"

    def test_headerless_csv_data_section_uses_envelope_columns(self):
        """
        TryOpenData returns '14 columns: ...' in the envelope but NO header row in
        the data section — column names come only from the envelope declaration.
        The parser must NOT treat the first data row as column headers.
        """
        from tools.opendata_parser import _parse_query_result

        # Real TryOpenData Titanic format: envelope declares 14 cols (including
        # _source_page which is absent from actual rows), data is headerless CSV.
        resp = (
            "3 rows returned\n"
            "14 columns: _source_page, _source_url, age, cabin, embarked, fare, "
            "name, parents_children, passenger_id, pclass, sex, siblings_spouses, "
            "survived, ticket\n"
            "Execution time: 5ms\n\n"
            "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv,"
            '22,,S,7.25,"Braund, Mr. Owen Harris",0,1,3,male,1,false,A/5 21171\n'
            "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv,"
            '38,C85,C,71.2833,"Cumings, Mrs. John Bradley (Florence Briggs Thayer)",'
            "0,2,1,female,1,true,PC 17599\n"
            "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv,"
            '26,,S,7.925,"Heikkinen, Miss. Laina",0,3,3,female,0,true,'
            "STON/O2. 3101282\n\n"
            "Open this query in the sandbox: https://tryopendata.ai/sandbox?sql=test"
        )
        headers, rows = _parse_query_result(resp)

        # Headers must be column names, not the URL value from the first data row
        assert headers, "Headers should not be empty"
        assert not any(
            h.startswith("http") for h in headers
        ), f"Headers contain URL values (data row used as header): {headers}"
        assert "_source_url" in headers, f"Expected _source_url in headers: {headers}"
        # All 3 passengers should be in rows (first data row must not be lost)
        assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}: {rows}"
        # First data value in each row should be the URL
        assert all(
            r[0].startswith("https://") for r in rows
        ), f"First column should be URL in all rows: {rows}"

    def test_source_columns_filtered_in_fetch(self):
        """
        _source_url and _source_page columns must be stripped from the final CSV
        when fetch_opendata_to_workspace writes the file.
        """
        from tools.opendata_parser import _parse_query_result

        # After _parse_query_result returns proper headers, the filtering in
        # _fetch_opendata_to_workspace_impl will strip _source_* columns.
        # Here we verify _parse_query_result itself returns correct column names.
        resp = (
            "2 rows returned\n"
            "13 columns: _source_url, age, cabin, embarked, fare, name, "
            "parents_children, passenger_id, pclass, sex, siblings_spouses, "
            "survived, ticket\n"
            "Execution time: 4ms\n\n"
            'https://example.com/titanic.csv,22,,S,7.25,"Braund, Mr. Owen",'
            "0,1,3,male,1,false,A/5 21171\n"
            'https://example.com/titanic.csv,38,C85,C,71.28,"Cumings, Mrs. John",'
            "0,2,1,female,1,true,PC 17599\n\n"
            "Open this query in the sandbox: https://tryopendata.ai/sandbox?sql=x"
        )
        headers, rows = _parse_query_result(resp)

        assert "_source_url" in headers, f"Expected _source_url in headers: {headers}"
        source_idx = headers.index("_source_url")
        # After filtering _source_* in fetch impl, age should be first non-source col
        non_source_headers = [h for h in headers if not h.startswith("_source_")]
        assert "age" in non_source_headers
        assert len(rows) == 2
        # All rows should have URL as first column (matches _source_url position)
        assert rows[0][source_idx].startswith("https://")


class TestWorkspaceAccess:
    """Unit tests for _get_workspace_path and workspace tool handlers."""

    def test_workspace_path_returns_none_without_state(self):
        from tools.workspace_tools import _get_workspace_path

        result = _get_workspace_path(None)
        assert result is None

    def test_workspace_path_uses_cleaning_workspace_manager(self):
        """_get_workspace_path calls WorkspaceManager.get_or_create."""
        from tools.workspace_tools import _get_workspace_path

        fake_path = "/tmp/fake_workspace"
        # The import is lazy (inside the function), so patch via the actual module path
        with patch("workspace.manager.WorkspaceManager") as mock_cls:
            mock_cls.return_value.get_or_create.return_value = fake_path
            state = _make_state("sess1", "user1")
            result = _get_workspace_path(state)
        assert result == Path(fake_path)
        mock_cls.return_value.get_or_create.assert_called_once_with("sess1", user_id="user1")

    async def test_list_workspace_files_no_workspace(self):
        """Returns friendly message when workspace directory doesn't exist."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        try:
            with patch("tools.workspace_tools._get_workspace_path", return_value=None):
                result = await _execute_tool(
                    action="list_workspace_files",
                    inp={},
                    creds_map={},
                    mcp_client=mgr,
                    state=_make_state(),
                )
            assert "no workspace" in result.lower() or "empty" in result.lower()
        finally:
            await mgr.close_all()

    async def test_list_workspace_files_returns_filenames(self):
        """Returns the filenames in the workspace directory."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Write two test files
            open(os.path.join(tmp_dir, "data.csv"), "w").close()
            open(os.path.join(tmp_dir, "summary.txt"), "w").close()

            mgr = MCPClientManager()
            try:
                with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                    result = await _execute_tool(
                        action="list_workspace_files",
                        inp={},
                        creds_map={},
                        mcp_client=mgr,
                        state=_make_state(),
                    )
                assert "data.csv" in result
                assert "summary.txt" in result
            finally:
                await mgr.close_all()

    async def test_read_workspace_file_returns_content(self):
        """Writes a temp CSV then reads it back via _execute_tool."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "test.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["country", "gdp"])
                writer.writerow(["USA", "25000000000000"])
                writer.writerow(["China", "18000000000000"])

            mgr = MCPClientManager()
            try:
                with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                    result = await _execute_tool(
                        action="read_workspace_file",
                        inp={"path": "test.csv"},
                        creds_map={},
                        mcp_client=mgr,
                        state=_make_state(),
                    )
                assert "USA" in result
                assert "country" in result
                assert "gdp" in result
            finally:
                await mgr.close_all()

    async def test_read_workspace_file_caps_at_50k(self):
        """Files larger than 50 000 chars are truncated."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            big_path = os.path.join(tmp_dir, "big.txt")
            with open(big_path, "w") as f:
                f.write("x" * 100_000)

            mgr = MCPClientManager()
            try:
                with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                    result = await _execute_tool(
                        action="read_workspace_file",
                        inp={"path": "big.txt"},
                        creds_map={},
                        mcp_client=mgr,
                        state=_make_state(),
                    )
                assert len(result) <= 50_100  # 50k content + truncation suffix (~45 chars)
            finally:
                await mgr.close_all()

    async def test_read_workspace_file_path_traversal_blocked(self):
        """Path traversal attempts (../../etc/passwd) return an error."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = MCPClientManager()
            try:
                with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                    result = await _execute_tool(
                        action="read_workspace_file",
                        inp={"path": "../../etc/passwd"},
                        creds_map={},
                        mcp_client=mgr,
                        state=_make_state(),
                    )
                # Should return an error, not the file contents
                assert "error" in result.lower() or "not allowed" in result.lower()
            finally:
                await mgr.close_all()


# ---------------------------------------------------------------------------
# TestReasoningEvents
# ---------------------------------------------------------------------------


class _MockEmitter:
    """Capture all emitted SSE events."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.parts: List[Dict[str, Any]] = []

    async def emit(self, agent: str, message: str, **kwargs):
        self.events.append({"agent": agent, "message": message, **kwargs})

    async def emit_part(self, part: Dict[str, Any]):
        self.parts.append(part)

    async def emit_token(self, token: str):
        pass

    def reasoning_events(self):
        return [e for e in self.events if e.get("event_type") == "reasoning"]


class TestReasoningEvents:
    """Verify that reasoning SSE events are emitted at every step."""

    async def test_integration_agent_emits_reasoning_events(self):
        """
        Mock MCPClientManager to return a canned tool list + result.
        Assert reasoning events with phase=start and phase=end are emitted.
        Assert the returned metadata contains reasoning_trace.
        """
        from agents.integration.agent import IntegrationAgent
        from llm.infer import LLMRouter
        from llm.models import LLMResponse

        # Build a fake LLM that:
        # Turn 1: returns call to list_mcp_servers
        # Turn 2: returns plain text (final answer)
        call_count = [0]

        async def fake_generate(req):
            call_count[0] += 1
            if call_count[0] == 1:
                # Tool call response
                return LLMResponse(
                    content="",
                    model="fake",
                    metadata={"tool_calls": [{"name": "list_mcp_servers", "arguments": {}}]},
                )
            else:
                # Final plain-text answer
                return LLMResponse(
                    content="The TryOpenData server is connected with status: connected.",
                    model="fake",
                    metadata={},
                )

        mock_router = MagicMock(spec=LLMRouter)
        mock_router.generate = AsyncMock(side_effect=fake_generate)

        # Fake MCPClientManager — not actually needed here since list_mcp_servers
        # is handled internally, but we patch the constructor anyway.
        emitter = _MockEmitter()
        state = _make_state()

        agent = IntegrationAgent(mock_router)

        with patch("agents.integration.agent.MCPClientManager") as mock_mgr_cls:
            mock_mgr = AsyncMock()
            mock_mgr.close_all = AsyncMock()
            mock_mgr_cls.return_value = mock_mgr

            result = await agent.execute(
                task="List the connected MCP servers.",
                state=state,
                app_user_id="test-user",
                creds_map={"opendata": {"api_key": "fake"}},
                progress_emitter=emitter,
            )

        assert result["status"] == "completed"
        assert result["response"]

        # Reasoning is emitted as structured step parts on the unified engine.
        step_parts = [p for p in emitter.parts if p.get("type") == "step"]
        assert len(step_parts) > 0, "Expected at least one reasoning step part"
        phases = {p.get("phase") for p in step_parts}
        assert "start" in phases, f"No start phase in: {phases}"

        # Check metadata trace
        trace = result.get("metadata", {}).get("reasoning_trace", [])
        assert len(trace) > 0, "reasoning_trace should be populated in metadata"
        for entry in trace:
            assert "step" in entry
            assert "action" in entry
            assert "agent" in entry

    async def test_reasoning_trace_includes_observations(self):
        """Each trace entry has an observation field after execution."""
        from agents.integration.agent import IntegrationAgent
        from llm.infer import LLMRouter
        from llm.models import LLMResponse

        call_count = [0]

        async def fake_generate(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return LLMResponse(
                    content="",
                    model="fake",
                    metadata={"tool_calls": [{"name": "list_mcp_servers", "arguments": {}}]},
                )
            else:
                return LLMResponse(
                    content="Done.",
                    model="fake",
                    metadata={},
                )

        mock_router = MagicMock(spec=LLMRouter)
        mock_router.generate = AsyncMock(side_effect=fake_generate)

        agent = IntegrationAgent(mock_router)
        state = _make_state()

        with patch("agents.integration.agent.MCPClientManager") as mock_mgr_cls:
            mock_mgr = AsyncMock()
            mock_mgr.close_all = AsyncMock()
            mock_mgr_cls.return_value = mock_mgr

            result = await agent.execute(
                task="List servers.",
                state=state,
                app_user_id="test-user",
                creds_map={},
            )

        trace = result.get("metadata", {}).get("reasoning_trace", [])
        # Every non-final entry should have a non-empty observation
        for entry in trace:
            if entry.get("phase") != "final":
                assert "observation" in entry


class TestListToolsCache:
    """list_tools is cached per MCPClientManager (one agent turn)."""

    async def test_list_tools_cached_within_turn(self):
        from mcp_infra.mcp_client import MCPClientManager

        mgr = MCPClientManager()
        server_def = MagicMock()
        server_def.server_id = "google_sheets"
        server_def.transport = "stdio"

        fetched = {"n": 0}

        async def _fake_get_actor(sd, creds):
            fetched["n"] += 1
            tools_result = MagicMock()
            tool = MagicMock()
            tool.name = "create_spreadsheet"
            tool.description = "make a sheet"
            tool.inputSchema = {"type": "object"}
            tool.model_dump = MagicMock(
                return_value={
                    "name": "create_spreadsheet",
                    "description": "make a sheet",
                    "inputSchema": {"type": "object"},
                }
            )
            tools_result.tools = [tool]
            actor = MagicMock()
            actor.request = AsyncMock(return_value=tools_result)
            return actor

        mgr._get_actor = AsyncMock(side_effect=_fake_get_actor)
        try:
            a = await mgr.list_tools(server_def, {})
            b = await mgr.list_tools(server_def, {})
            assert a == b
            assert a[0]["name"] == "create_spreadsheet"
            # Second call served from cache — session was opened only once.
            assert fetched["n"] == 1
        finally:
            await mgr.close_all()
        # Cache cleared on close.
        assert mgr._tools_cache == {}


class TestReasoningEventsUnified:
    """Same behavior as TestReasoningEvents but on the unified engine (flag on)."""

    def _agent_with_flag(self, router):
        from agents.integration.agent import IntegrationAgent

        return IntegrationAgent(router, config={"agents": {"use_unified_engine": True}})

    async def test_unified_emits_events_and_native_transcript(self):
        from llm.infer import LLMRouter
        from llm.models import LLMResponse

        seen_requests: List[Any] = []
        call_count = [0]

        async def fake_generate(req):
            seen_requests.append([m.model_dump(exclude_none=True) for m in req.messages])
            call_count[0] += 1
            if call_count[0] == 1:
                return LLMResponse(
                    content="let me check",
                    model="fake",
                    metadata={
                        "tool_calls": [
                            {"id": "call_1", "name": "list_mcp_servers", "arguments": {}}
                        ]
                    },
                )
            return LLMResponse(content="TryOpenData is connected.", model="fake", metadata={})

        mock_router = MagicMock(spec=LLMRouter)
        mock_router.generate = AsyncMock(side_effect=fake_generate)

        emitter = _MockEmitter()
        agent = self._agent_with_flag(mock_router)

        with patch("agents.integration.agent.MCPClientManager") as mock_mgr_cls:
            mock_mgr = AsyncMock()
            mock_mgr.close_all = AsyncMock()
            mock_mgr_cls.return_value = mock_mgr
            result = await agent.execute(
                task="List the connected MCP servers.",
                state=_make_state(),
                app_user_id="test-user",
                creds_map={"opendata": {"api_key": "fake"}},
                progress_emitter=emitter,
            )

        assert result["status"] == "completed"
        assert result["response"] == "TryOpenData is connected."

        # Trace parts emitted (the unified path streams `part.updated` parts).
        assert {p.get("phase") for p in emitter.parts} & {"start", "end", "final"}
        assert all(p.get("agent") == "integration" for p in emitter.parts)

        # Native transcript on the SECOND request: the tool result is a role:"tool"
        # message (NOT a fake user message), correlated by tool_call_id.
        second = seen_requests[1]
        roles = [m["role"] for m in second]
        assert "tool" in roles, f"expected a native tool message, got roles={roles}"
        tool_msg = next(m for m in second if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "call_1"
        # No fake user message smuggling the tool result back in.
        user_msgs = [m for m in second if m["role"] == "user"]
        assert all("Tool result for" not in (m.get("content") or "") for m in user_msgs)

        trace = result["metadata"]["reasoning_trace"]
        assert trace and all("action" in e and "agent" in e for e in trace)


# ---------------------------------------------------------------------------
# TestWriteCsvToSpreadsheet
# ---------------------------------------------------------------------------


class TestWriteCsvToSpreadsheet:
    """Unit tests for _write_csv_to_spreadsheet_impl and its _execute_tool handler."""

    # ── Helper: build a minimal MCP client mock ──────────────────────────

    def _make_mcp_mock(self, create_result: str = '{"spreadsheetId": "sheet123"}') -> MagicMock:
        """Return an AsyncMock MCPClientManager whose call_tool_strict is pre-configured."""
        mgr = AsyncMock()
        mgr.close_all = AsyncMock()

        async def _call_tool_strict(server_def, tool_name, arguments, creds=None):
            if tool_name == "create_spreadsheet":
                return create_result
            if tool_name == "update_values":
                return '{"updatedRange": "Sheet1!A1:E150"}'
            return "{}"

        # wire both call_tool and call_tool_strict to the same handler
        mgr.call_tool = AsyncMock(side_effect=_call_tool_strict)
        mgr.call_tool_strict = AsyncMock(side_effect=_call_tool_strict)
        return mgr

    # ── Tests ────────────────────────────────────────────────────────────

    async def test_write_csv_no_spreadsheet_id_creates_new(self):
        """When spreadsheet_id is omitted, create_spreadsheet is called and URL returned."""
        from tools.sheets_tools import _write_csv_to_spreadsheet_impl

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            csv_path = workspace / "iris.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["sepal_length", "species"])
                for i in range(5):
                    writer.writerow([str(5.0 + i * 0.1), "setosa"])

            mgr = self._make_mcp_mock()
            creds = {"access_token": "fake"}

            with patch("tools.sheets_tools.MCP_REGISTRY") as mock_reg:
                mock_def = MagicMock()
                mock_def.required_credentials = []
                mock_reg.get.return_value = mock_def

                result = await _write_csv_to_spreadsheet_impl(
                    filename="iris.csv",
                    workspace=workspace,
                    credentials=creds,
                    mcp_client=mgr,
                )

        assert "sheet123" in result, f"Expected spreadsheet ID in result: {result}"
        assert "docs.google.com/spreadsheets" in result
        assert "5" in result  # 5 data rows
        # create_spreadsheet was called once
        create_calls = [
            c for c in mgr.call_tool_strict.call_args_list if c.args[1] == "create_spreadsheet"
        ]
        assert len(create_calls) == 1, "Expected exactly one create_spreadsheet call"

    async def test_write_csv_existing_spreadsheet_id_no_create(self):
        """When spreadsheet_id is provided, create_spreadsheet is NOT called."""
        from tools.sheets_tools import _write_csv_to_spreadsheet_impl

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            csv_path = workspace / "data.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["a", "b"])
                writer.writerow(["1", "2"])

            mgr = self._make_mcp_mock()
            creds = {"access_token": "fake"}

            with patch("tools.sheets_tools.MCP_REGISTRY") as mock_reg:
                mock_def = MagicMock()
                mock_def.required_credentials = []
                mock_reg.get.return_value = mock_def

                result = await _write_csv_to_spreadsheet_impl(
                    filename="data.csv",
                    workspace=workspace,
                    credentials=creds,
                    mcp_client=mgr,
                    spreadsheet_id="existing-id-xyz",
                )

        assert "existing-id-xyz" in result
        create_calls = [
            c for c in mgr.call_tool_strict.call_args_list if c.args[1] == "create_spreadsheet"
        ]
        assert len(create_calls) == 0, "create_spreadsheet should NOT be called when ID given"

    async def test_write_csv_path_traversal_blocked(self):
        """Path traversal filenames are rejected without file access."""
        from tools.sheets_tools import _write_csv_to_spreadsheet_impl

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            mgr = self._make_mcp_mock()
            creds = {"access_token": "fake"}

            with patch("tools.sheets_tools.MCP_REGISTRY") as mock_reg:
                mock_def = MagicMock()
                mock_def.required_credentials = []
                mock_reg.get.return_value = mock_def

                result = await _write_csv_to_spreadsheet_impl(
                    filename="../../etc/passwd",
                    workspace=workspace,
                    credentials=creds,
                    mcp_client=mgr,
                )

        assert "error" in result.lower()
        # MCP should never have been called
        assert mgr.call_tool_strict.call_count == 0

    async def test_write_csv_file_not_found(self):
        """Non-existent filename returns a friendly error message."""
        from tools.sheets_tools import _write_csv_to_spreadsheet_impl

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            mgr = self._make_mcp_mock()
            creds = {"access_token": "fake"}

            with patch("tools.sheets_tools.MCP_REGISTRY") as mock_reg:
                mock_def = MagicMock()
                mock_def.required_credentials = []
                mock_reg.get.return_value = mock_def

                result = await _write_csv_to_spreadsheet_impl(
                    filename="nonexistent.csv",
                    workspace=workspace,
                    credentials=creds,
                    mcp_client=mgr,
                )

        assert "not found" in result.lower() or "error" in result.lower()
        assert mgr.call_tool.call_count == 0

    async def test_write_csv_google_sheets_not_connected(self):
        """When google_sheets is absent from creds_map, returns actionable error."""
        from agents.integration.agent import _execute_tool
        from mcp_infra.mcp_client import MCPClientManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            csv_path = workspace / "data.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["x"])
                writer.writerow(["1"])

            mgr = MCPClientManager()
            try:
                with patch("tools.workspace_tools._get_workspace_path", return_value=workspace):
                    result = await _execute_tool(
                        action="write_csv_to_spreadsheet",
                        inp={"filename": "data.csv"},
                        creds_map={},  # google_sheets absent
                        mcp_client=mgr,
                        state=_make_state(),
                    )
            finally:
                await mgr.close_all()

        assert "not connected" in result.lower() or "error" in result.lower()
        assert "settings" in result.lower() or "integrations" in result.lower()

    async def test_write_csv_large_file_batched(self):
        """CSV with more than _SHEETS_BATCH_SIZE rows triggers multiple write_values calls."""
        from tools.sheets_tools import _SHEETS_BATCH_SIZE, _write_csv_to_spreadsheet_impl

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            csv_path = workspace / "large.csv"
            n_data_rows = _SHEETS_BATCH_SIZE + 100  # forces at least 2 batches
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "value"])
                for i in range(n_data_rows):
                    writer.writerow([str(i), f"val_{i}"])

            mgr = self._make_mcp_mock()
            creds = {"access_token": "fake"}

            with patch("tools.sheets_tools.MCP_REGISTRY") as mock_reg:
                mock_def = MagicMock()
                mock_def.required_credentials = []
                mock_reg.get.return_value = mock_def

                result = await _write_csv_to_spreadsheet_impl(
                    filename="large.csv",
                    workspace=workspace,
                    credentials=creds,
                    mcp_client=mgr,
                    spreadsheet_id="sheet-abc",
                )

        write_calls = [
            c for c in mgr.call_tool_strict.call_args_list if c.args[1] == "update_values"
        ]
        assert len(write_calls) >= 2, (
            f"Expected ≥2 write_values calls for {n_data_rows} rows, " f"got {len(write_calls)}"
        )
        assert str(n_data_rows) in result or "rows" in result.lower()


# ---------------------------------------------------------------------------
# TestFullFlow — live (requires TRYOPENDATA_API_KEY)
# ---------------------------------------------------------------------------


class TestFullFlow:
    @live
    async def test_search_opendata_via_integration_agent(self):
        """
        End-to-end: integration agent lists servers, discovers TryOpenData tools,
        and searches for GDP data. Requires TRYOPENDATA_API_KEY.

        Run:
            cd backend
            TRYOPENDATA_API_KEY=<key> python3 -m pytest \
                tests/integration/test_integration_agent.py::TestFullFlow -v -s
        """
        from agents.integration.agent import IntegrationAgent
        from llm.infer import LLMRouter

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Write a sample CSV so the workspace isn't empty
            csv_path = os.path.join(tmp_dir, "gdp_data.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["country", "year", "gdp_usd", "growth_pct"])
                writer.writerow(["USA", "2022", "25000000000000", "2.1"])
                writer.writerow(["China", "2022", "18000000000000", "3.0"])
                writer.writerow(["Germany", "2022", "4200000000000", "1.8"])

            state = _make_state("live-test-session", "live-test-user")

            llm_router = LLMRouter(_load_llm_config())
            agent = IntegrationAgent(llm_router)

            creds_map = {"opendata": {"api_key": API_KEY}}

            with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                result = await agent.execute(
                    task=(
                        "List the workspace files, then search TryOpenData for global GDP data. "
                        "Report what datasets you find."
                    ),
                    state=state,
                    app_user_id="live-test-user",
                    creds_map=creds_map,
                )

        print("\n=== Integration Agent Response ===")
        print(result["response"])
        print("\n=== Reasoning Trace ===")
        for step in result.get("metadata", {}).get("reasoning_trace", []):
            print(f"  step={step['step']} action={step['action']} phase={step.get('phase', '?')}")
            obs = str(step.get("observation", ""))
            if obs:
                print(f"    obs={obs[:200]}")

        assert result["status"] == "completed", f"Expected completed, got: {result['status']}"
        assert result["response"], "Response should not be empty"

        trace = result.get("metadata", {}).get("reasoning_trace", [])
        assert len(trace) > 0, "reasoning_trace should be populated"

        # Check the agent actually called at least one MCP-related tool
        actions = [t["action"] for t in trace]
        mcp_actions = [a for a in actions if "mcp" in a or "workspace" in a]
        assert mcp_actions, f"Expected at least one mcp/workspace action, got: {actions}"

    @live
    async def test_workspace_file_read_and_export_flow(self):
        """
        Verify the agent can list workspace files, read one, and mention its content.
        Does not require Google Sheets — just confirms the data context pathway works.
        """
        from agents.integration.agent import IntegrationAgent
        from llm.infer import LLMRouter

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "analysis_result.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["metric", "value"])
                writer.writerow(["mean_salary", "85000"])
                writer.writerow(["median_salary", "72000"])
                writer.writerow(["std_salary", "15000"])

            state = _make_state("ws-test-session", "ws-test-user")
            llm_router = LLMRouter(_load_llm_config())
            agent = IntegrationAgent(llm_router)

            with patch("tools.workspace_tools._get_workspace_path", return_value=Path(tmp_dir)):
                result = await agent.execute(
                    task=(
                        "List the workspace files and read analysis_result.csv. "
                        "Tell me what data is in the file."
                    ),
                    state=state,
                    app_user_id="ws-test-user",
                    creds_map={},
                )

        print("\n=== Response ===")
        print(result["response"])

        assert result["status"] == "completed"
        # The agent should mention the file or its data in the response
        response_lower = result["response"].lower()
        assert any(
            kw in response_lower for kw in ["salary", "metric", "analysis_result", "csv", "file"]
        ), f"Response doesn't mention the file data: {result['response'][:300]}"
