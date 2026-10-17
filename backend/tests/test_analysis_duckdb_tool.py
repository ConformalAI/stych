"""General-analysis SQL tool routing for non-Postgres sessions."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from agents.state import AgentState, DataContext
from execution.duckdb import duckdb_kernel_manager
from tools.analysis_tools import ReactToolRegistry
from tools.workspace_toolset import WorkspaceToolset


class _WorkspaceManager:
    root = None

    def get_or_create(self, session_id, user_id=None):
        path = type(self).root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


@pytest.fixture()
def analysis_state(tmp_path, monkeypatch):
    import execution.session_plane as session_plane

    _WorkspaceManager.root = tmp_path
    monkeypatch.setattr(session_plane, "WorkspaceManager", _WorkspaceManager)
    frame = pd.DataFrame({"region": ["EU", "EU", "NA"], "amount": [5, 7, 3]})
    state = AgentState(session_id="analysis-duckdb", metadata={"user_id": "test-user"})
    state.set_data_context(
        DataContext(
            file_id="orders-1",
            filename="orders.csv",
            raw_dataframe=frame.copy(deep=True),
            working_dataframe=frame.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    yield state, frame
    duckdb_kernel_manager.close_session(state.session_id)


async def test_run_sql_queries_the_canonical_duckdb_relation(analysis_state):
    state, frame = analysis_state
    registry = ReactToolRegistry(WorkspaceToolset(), None)
    output = await registry.call(
        "run_sql",
        {
            "query": (
                "SELECT region, SUM(amount) AS total FROM {table} "
                "GROUP BY region ORDER BY total DESC"
            )
        },
        frame,
        state,
    )
    assert "Query returned 2 rows" in output
    assert "EU" in output and "12" in output
    assert "Source snapshot: full" in output


async def test_run_sql_keeps_duckdb_read_only(analysis_state):
    state, frame = analysis_state
    registry = ReactToolRegistry(WorkspaceToolset(), None)
    output = await registry.call("run_sql", {"query": "DELETE FROM {table}"}, frame, state)
    assert "Only read-only" in output


async def test_run_sql_accepts_leading_comments_and_batches(analysis_state):
    state, frame = analysis_state
    registry = ReactToolRegistry(WorkspaceToolset(), None)
    output = await registry.call(
        "run_sql",
        {
            "query": (
                "-- 1. OVERVIEW\n"
                "SELECT COUNT(*) AS n FROM {table};\n"
                "-- 2. REGIONS\n"
                "SELECT region, SUM(amount) AS total FROM {table} GROUP BY region"
            )
        },
        frame,
        state,
    )
    assert "Query 1/2" in output
    assert "Query 2/2" in output
    assert "EU" in output


async def test_run_sql_rejects_write_hiding_behind_a_select(analysis_state):
    state, frame = analysis_state
    registry = ReactToolRegistry(WorkspaceToolset(), None)
    output = await registry.call(
        "run_sql",
        {"query": "SELECT 1; DELETE FROM {table}"},
        frame,
        state,
    )
    assert "Only read-only" in output
    assert "DELETE" in output


async def test_get_schema_reports_the_actual_duckdb_relation(analysis_state):
    state, frame = analysis_state
    registry = ReactToolRegistry(WorkspaceToolset(), None)
    output = await registry.call("get_schema", {}, frame, state)
    assert "DuckDB schema" in output
    assert "amount (int64)" in output
