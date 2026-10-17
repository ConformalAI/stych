"""
Regression tests for the SQL ReAct tool contract (tools/cleaning_tools.py).

These pin the bug this audit fixed: sql_read / sql_preview must run the model's
*actual* query — accepting either the ``sql`` or ``query`` arg name — instead of
silently defaulting to a full-table ``SELECT * ... LIMIT 20``, and must error loudly
when no SQL is supplied. No live database is required: a fake SQL agent records the
SQL that reaches ``run_sql`` and resolves the {table} placeholder.
"""

import importlib
from datetime import datetime

import pandas as pd
import pytest

from agents.state import AgentState, DataContext
from tools.cleaning_tools import CleaningReactToolRegistry

cleaning_tools = importlib.import_module("tools.cleaning_tools")


class _FakeSQLAgent:
    def __init__(self):
        self.executed = []

    async def ensure_temp_table(self, state):
        return ("stych_temp", "t_abc")

    def _runner_for(self, state):
        # {table} is quoted by the runner, because the correct spelling is
        # dialect-specific (backticks and a catalog on Databricks, brackets on
        # T-SQL). These tests assert the Postgres rendering. _FakeRunner is
        # defined below; the reference resolves at call time.
        return _FakeRunner()

    async def run_sql(self, state, sql):
        self.executed.append(sql)
        return [{"status": "Shipped", "n": 3}]

    async def run_attached_sql_frames(self, state, sql):
        assert "{table}" in sql
        resolved = sql.replace("{table}", '"stych_temp"."t_abc"')
        self.executed.append(resolved)
        return [pd.DataFrame([{"status": "Shipped", "n": 3}])]


def _state():
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


def _registry(fake):
    return CleaningReactToolRegistry(
        sql_agent=fake,
        workspace_manager=None,
        workspace_runner=None,
        is_sql_available=True,
    )


@pytest.mark.asyncio
async def test_sql_working_copy_tools_are_not_rejected_as_mongodb_tools(monkeypatch):
    async def fake_execute(_agent, _state, tool_name, _tool_input):
        return f"executed:{tool_name}"

    monkeypatch.setattr(cleaning_tools, "execute_working_copy_tool", fake_execute)

    out = await _registry(_FakeSQLAgent()).call("list_working_copies", {}, _sql_state())

    assert out == "executed:list_working_copies"


@pytest.mark.asyncio
async def test_sql_read_runs_query_arg_not_default():
    fake = _FakeSQLAgent()
    out = await _registry(fake).call(
        "sql_read",
        {"query": "SELECT status, COUNT(*) FROM {table} GROUP BY status"},
        _state(),
    )
    assert len(fake.executed) == 1
    assert "GROUP BY status" in fake.executed[0]
    # The old silent default ran SELECT * ... LIMIT 20 instead of the model's query.
    assert "LIMIT 20" not in fake.executed[0]
    # {table} placeholder resolved to the temp table, not guessed.
    assert '"stych_temp"."t_abc"' in fake.executed[0]
    assert "ERROR" not in out


@pytest.mark.asyncio
async def test_sql_read_accepts_sql_arg():
    fake = _FakeSQLAgent()
    await _registry(fake).call("sql_read", {"sql": "SELECT DISTINCT status FROM {table}"}, _state())
    assert "DISTINCT status" in fake.executed[0]


@pytest.mark.asyncio
async def test_sql_read_missing_sql_errors_without_executing():
    fake = _FakeSQLAgent()
    out = await _registry(fake).call("sql_read", {"target": "temp"}, _state())
    assert "No SQL provided" in out
    assert fake.executed == []


@pytest.mark.asyncio
async def test_sql_preview_accepts_query_alias():
    fake = _FakeSQLAgent()
    out = await _registry(fake).call(
        "sql_preview", {"query": "SELECT * FROM {table} LIMIT 5"}, _state()
    )
    assert "LIMIT 5" in fake.executed[0]
    assert "ERROR" not in out


@pytest.mark.asyncio
async def test_source_target_resolves_original_without_creating_a_working_copy():
    fake = _FakeSQLAgent()

    async def fail_if_called(_state):
        raise AssertionError("source resolution must not create or select a working copy")

    fake.ensure_temp_table = fail_if_called
    target = await _registry(fake)._resolve_sql_target(_sql_state(), "source")

    assert target == '"public"."orders"'


# ---------------------------------------------------------------------------
# Copy-table write-back contract (sql_materialize_copy / sql_write_copy).
# Pins the bugs behind the CleaningAgent permission loop: the materialize happy
# path crashing on an unbound `schema`, CTE rejection, the redundant SELECT path
# in sql_write_copy, and sql_read mislabeling raw queries as the temp table.
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, table_exists=True):
        self.transactions = []  # list of (statements, allowed_schema)
        self.executed = []  # raw DDL via _execute (e.g. CREATE TABLE on first write-back)
        self._table_exists = table_exists

    def table_exists(self, schema, table):
        return self._table_exists

    def _execute(self, sql):
        self.executed.append(sql)
        # A create makes the copy table exist for subsequent checks.
        self._table_exists = True

    def execute_statements_transaction(self, statements, allowed_schema=None):
        self.transactions.append((statements, allowed_schema))

    def get_row_count(self, schema, table):
        return 25

    def quote_relation(self, schema, table):
        # Postgres-shaped spelling, matching what these contract tests assert.
        return f'"{schema}"."{table}"'


class _FakeSQLAgentWithRunner(_FakeSQLAgent):
    def __init__(self):
        super().__init__()
        self.runner = _FakeRunner()

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
            },
        )
    )
    return st


@pytest.mark.asyncio
async def test_materialize_copy_temp_source_does_not_crash_on_schema():
    # Regression: `schema` was only bound on the source_target=="source" branch but
    # used unconditionally, so the default temp path raised UnboundLocalError.
    fake = _FakeSQLAgentWithRunner()
    out = await _registry(fake).call(
        "sql_materialize_copy",
        {"select_sql": "SELECT * FROM {source_table}", "source_target": "temp", "approved": True},
        _sql_state(),
    )
    assert "SQL ERROR" not in out
    assert '"status": "ok"' in out
    assert len(fake.runner.transactions) == 1
    stmts, allowed = fake.runner.transactions[0]
    assert allowed == "public"
    assert any("TRUNCATE" in s for s in stmts)
    assert any("INSERT INTO" in s for s in stmts)


@pytest.mark.asyncio
async def test_materialize_copy_accepts_cte():
    fake = _FakeSQLAgentWithRunner()
    out = await _registry(fake).call(
        "sql_materialize_copy",
        {
            "select_sql": "WITH c AS (SELECT * FROM {source_table}) SELECT * FROM c",
            "approved": True,
        },
        _sql_state(),
    )
    assert "must start with SELECT" not in out
    assert "SQL ERROR" not in out
    assert len(fake.runner.transactions) == 1


@pytest.mark.asyncio
async def test_materialize_copy_requires_approval():
    fake = _FakeSQLAgentWithRunner()
    out = await _registry(fake).call(
        "sql_materialize_copy",
        {"select_sql": "SELECT * FROM {source_table}"},
        _sql_state(),
    )
    assert "PERMISSION_REQUIRED" in out
    assert fake.runner.transactions == []


@pytest.mark.asyncio
async def test_materialize_copy_auto_creates_and_defaults_select():
    # One approved call must create the copy table when missing AND persist the whole
    # cleaned working table, even with no select_sql — the reliable single-step write-back.
    fake = _FakeSQLAgentWithRunner()
    fake.runner = _FakeRunner(table_exists=False)
    out = await _registry(fake).call(
        "sql_materialize_copy",
        {"approved": True},  # no select_sql, no source_target
        _sql_state(),
    )
    assert "SQL ERROR" not in out
    assert '"status": "ok"' in out
    assert '"rows_written": 25' in out
    # Created the copy table (LIKE source) before populating it.
    assert any("CREATE TABLE" in s and "copy_orders" in s for s in fake.runner.executed)
    assert len(fake.runner.transactions) == 1
    stmts, _ = fake.runner.transactions[0]
    # Defaulted to persisting the full cleaned (temp) table.
    assert any("TRUNCATE" in s for s in stmts)
    assert any('"stych_temp"."t_abc"' in s for s in stmts)


@pytest.mark.asyncio
async def test_sql_write_copy_rejects_select_without_writing():
    fake = _FakeSQLAgentWithRunner()
    out = await _registry(fake).call(
        "sql_write_copy",
        {"sql": "SELECT * FROM {table}", "approved": True},
        _sql_state(),
    )
    assert "sql_materialize_copy" in out
    assert fake.runner.transactions == []


@pytest.mark.asyncio
async def test_sql_write_copy_rejects_writes_to_the_source_table():
    fake = _FakeSQLAgentWithRunner()
    out = await _registry(fake).call(
        "sql_write_copy",
        {"sql": 'UPDATE "public"."customers" SET qty = 0', "approved": True},
        _sql_state(),
    )
    assert out.startswith("ERROR:")
    assert "copy_orders" in out
    assert fake.runner.transactions == []


@pytest.mark.asyncio
async def test_sql_read_literal_table_is_refused_by_asset_scope():
    fake = _FakeSQLAgent()
    out = await _registry(fake).call(
        "sql_read",
        {"sql": "SELECT DISTINCT country FROM public.copy_orders"},
        _state(),
    )
    assert "attached dataset" in out
    assert fake.executed == []
