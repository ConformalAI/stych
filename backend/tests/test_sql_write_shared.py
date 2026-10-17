"""
Tests for the shared temp-table write path (tools/sql_write.py).

Pins the fix for the silent-no-op mutation bug: SQL sessions routed to the
analysis agent had only read-only tools, so requested data changes (imputation,
derived columns) were computed in CTEs and never persisted. Both the analysis
and cleaning registries now expose ``sql_write_temp`` through one shared
implementation that also bumps ``temp_table_generation`` so preview/diff
caches refresh. No live database: fakes record what reaches the runner.
"""

from datetime import datetime

import pandas as pd

from agents.analysis.agent import _SQL_ANALYSIS_TOOLS
from agents.state import AgentState, DataContext
from tools.analysis_tools import ReactToolRegistry
from tools.cleaning_tools import CleaningReactToolRegistry
from tools.sql_write import execute_sql_write_temp


class _FakeRunner:
    def __init__(self):
        self.transactions = []  # list of (statements, allowed_schema)

    def quote_relation(self, schema, table):
        return f'"{schema}"."{table}"'

    def execute_statements_transaction(self, statements, allowed_schema=None):
        self.transactions.append((statements, allowed_schema))


class _FakeSQLAgent:
    def __init__(self):
        self.runner = _FakeRunner()

    async def ensure_temp_table(self, state):
        return ("stych_temp", "t_abc")

    def _runner_for(self, state):
        return self.runner

    def _runner(self, connection_url):
        return self.runner


def _sql_state():
    raw = pd.DataFrame({"a": [1]})
    st = AgentState(session_id="s")
    st.set_data_context(
        DataContext(
            file_id="f",
            filename="d.csv",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "postgres",
                "connection_url": "postgresql://localhost/db",
                "schema": "public",
                "table": "orders",
                "temp_table_generation": 0,
            },
        )
    )
    return st


def _file_state():
    raw = pd.DataFrame({"a": [1]})
    st = AgentState(session_id="s")
    st.set_data_context(
        DataContext(
            file_id="f",
            filename="d.csv",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    return st


async def test_write_resolves_table_and_bumps_generation():
    fake = _FakeSQLAgent()
    state = _sql_state()
    out = await execute_sql_write_temp(
        fake, state, 'UPDATE {table} SET "total" = 1 WHERE "total" IS NULL'
    )
    assert "ERROR" not in out
    statements, allowed_schema = fake.runner.transactions[0]
    assert statements == ['UPDATE "stych_temp"."t_abc" SET "total" = 1 WHERE "total" IS NULL']
    assert allowed_schema == "stych_temp"
    # Preview/diff caches key on the generation — a write must invalidate them.
    assert state.data_context.metadata["temp_table_generation"] == 1


async def test_write_rejects_select_only_without_executing():
    fake = _FakeSQLAgent()
    state = _sql_state()
    out = await execute_sql_write_temp(fake, state, "SELECT * FROM {table}")
    assert out.startswith("ERROR")
    assert fake.runner.transactions == []
    assert state.data_context.metadata["temp_table_generation"] == 0


async def test_write_rejects_commented_select_only():
    fake = _FakeSQLAgent()
    state = _sql_state()
    out = await execute_sql_write_temp(fake, state, "-- peek\nSELECT * FROM {table}")
    assert out.startswith("ERROR")
    assert fake.runner.transactions == []


async def test_write_empty_sql_errors():
    out = await execute_sql_write_temp(_FakeSQLAgent(), _sql_state(), "")
    assert out.startswith("ERROR")


async def test_write_runner_failure_reports_and_skips_bump():
    class _Boom(_FakeRunner):
        def execute_statements_transaction(self, statements, allowed_schema=None):
            raise RuntimeError("permission denied")

    fake = _FakeSQLAgent()
    fake.runner = _Boom()
    state = _sql_state()
    out = await execute_sql_write_temp(fake, state, "DELETE FROM {table} WHERE a IS NULL")
    assert out.startswith("SQL ERROR")
    assert state.data_context.metadata["temp_table_generation"] == 0


async def test_analysis_registry_dispatches_sql_write_temp():
    fake = _FakeSQLAgent()
    state = _sql_state()
    registry = ReactToolRegistry(toolset=None, sql_agent=fake)
    out = await registry.call(
        "sql_write_temp",
        {"sql": "ALTER TABLE {table} ADD COLUMN total numeric"},
        df=state.data_context.working_dataframe,
        state=state,
    )
    assert "ERROR" not in out
    statements, _ = fake.runner.transactions[0]
    assert '"stych_temp"."t_abc"' in statements[0]
    assert state.data_context.metadata["temp_table_generation"] == 1


async def test_analysis_registry_write_unavailable_for_file_sessions():
    fake = _FakeSQLAgent()
    state = _file_state()
    registry = ReactToolRegistry(toolset=None, sql_agent=fake)
    out = await registry.call(
        "sql_write_temp",
        {"sql": "UPDATE {table} SET a = 1"},
        df=state.data_context.working_dataframe,
        state=state,
    )
    assert "only available when a PostgreSQL datasource" in out
    assert fake.runner.transactions == []


async def test_cleaning_registry_write_bumps_generation():
    # The cleaning ReAct path used to write without bumping the generation,
    # leaving the preview/diff caches stale until an unrelated cleaning run.
    fake = _FakeSQLAgent()
    state = _sql_state()
    registry = CleaningReactToolRegistry(
        sql_agent=fake,
        workspace_manager=None,
        workspace_runner=None,
        is_sql_available=True,
    )
    out = await registry.call(
        "sql_write_temp", {"sql": "UPDATE {table} SET a = 1 WHERE a IS NULL"}, state
    )
    assert "ERROR" not in out
    assert fake.runner.transactions[0][1] == "stych_temp"
    assert state.data_context.metadata["temp_table_generation"] == 1


def test_sql_analysis_toolset_exposes_write_and_lifecycle():
    names = {t["function"]["name"] for t in _SQL_ANALYSIS_TOOLS}
    assert names == {
        "run_sql",
        "get_schema",
        "sql_write_temp",
        "create_working_copy",
        "switch_working_copy",
        "drop_working_copy",
        "list_working_copies",
        "diff_working_copies",
    }
