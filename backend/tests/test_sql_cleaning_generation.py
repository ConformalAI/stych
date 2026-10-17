"""The SQL cleaning paths must bump ``temp_table_generation`` after mutating
the stych_temp working table — preview/diff caches key on it."""

from types import SimpleNamespace

from agents.sql_agent.cleaning import SQLCleaningService


class _FakeRunner:
    atomic_writes = True

    def quote_relation(self, schema, table):
        return f'"{schema}"."{table}"'

    def get_row_count(self, schema, table):
        return 10

    def get_table_columns(self, schema, table):
        return [{"column_name": "a", "data_type": "integer", "is_nullable": "YES"}]

    def execute_statements_transaction(self, statements, allowed_schema=None):
        if any("BOOM" in s for s in statements):
            raise RuntimeError("boom")

    def create_or_replace_view(self, *args):
        pass


class _FakeAgent:
    async def ensure_temp_table(self, state):
        return ("stych_temp", "w1")

    def _runner(self, url):
        return _FakeRunner()

    def _runner_for(self, state):
        return _FakeRunner()


def _state():
    meta = {
        "connection_url": "postgresql://u:p@h/db",
        "temp_schema": "stych_temp",
        "temp_table": "w1",
        "temp_table_generation": 0,
    }
    return SimpleNamespace(session_id="s1", data_context=SimpleNamespace(metadata=meta))


async def test_script_success_bumps_generation():
    svc = SQLCleaningService(_FakeAgent())
    state = _state()
    result = await svc.run_cleaning_sql_script(state, 'UPDATE {table} SET "a" = 1')
    assert not result.get("error")
    assert state.data_context.metadata["temp_table_generation"] == 1


async def test_script_failure_does_not_bump():
    svc = SQLCleaningService(_FakeAgent())
    state = _state()
    result = await svc.run_cleaning_sql_script(state, "UPDATE {table} SET BOOM = 1")
    assert result.get("error")
    assert state.data_context.metadata["temp_table_generation"] == 0


async def test_structured_ops_bump_generation():
    svc = SQLCleaningService(_FakeAgent())
    state = _state()
    await svc.run_cleaning_sql(state, [])
    assert state.data_context.metadata["temp_table_generation"] == 1
