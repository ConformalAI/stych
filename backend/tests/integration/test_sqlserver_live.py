"""
Live-SQL Server checks for the scratch runner, the write guard, and the T-SQL
dialect. Skipped unless a server is reachable:

    STYCH_TEST_SQLSERVER_URL=mssql+pymssql://sa:PASSWORD@127.0.0.1:14330/master \
        pytest tests/integration/test_sqlserver_live.py

On Apple Silicon the usable image is ``mcr.microsoft.com/azure-sql-edge``
(arm64 native); ``mcr.microsoft.com/mssql/server`` is amd64 only and its
binaries fail under Rosetta with "unable to mmap ELF".

These cover what a dialect unit test structurally cannot: that a real T-SQL
engine accepts the SQL we emit. SQL Server is the dialect with the most
divergence — ``TOP`` instead of ``LIMIT``, ``SELECT … INTO`` instead of
``CREATE TABLE AS``, bracket quoting, and no regex operator at all — so a green
unit run is necessary, not sufficient.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("pymssql")

from agents.sql_agent.protocols import SQLRunnerError  # noqa: E402
from agents.sql_agent.sqlserver_runner import SqlServerSQLRunner  # noqa: E402
from agents.sql_agent.write_guard import (  # noqa: E402
    WriteGuardError,
    validate_write_statements,
)
from datasources.sql_dialect import UnsupportedDialectFeature, get_dialect  # noqa: E402


def _url() -> str:
    url = os.getenv("STYCH_TEST_SQLSERVER_URL", "")
    if not url:
        pytest.skip("Set STYCH_TEST_SQLSERVER_URL for SQL Server integration tests")
    return url


@pytest.fixture(scope="module")
def sqlserver():
    """A throwaway source table plus a runner on its own scratch schema."""
    from sqlalchemy import create_engine, text

    url = _url()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"SQL Server not reachable: {exc}")

    table = f"orders_{uuid.uuid4().hex[:8]}"
    scratch = f"stych_tmp_{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE dbo.[{table}] (id INT PRIMARY KEY, sku NVARCHAR(50), "
                f"qty INT, price DECIMAL(10,2))"
            )
        )
        for row in (
            (1, "A", 2, 10.50),
            (2, "B", 0, 3.25),
            (3, "A", 5, 7.00),
        ):
            conn.execute(
                text(f"INSERT INTO dbo.[{table}] VALUES (:i, :s, :q, :p)"),
                {"i": row[0], "s": row[1], "q": row[2], "p": row[3]},
            )
    runner = SqlServerSQLRunner({"connection_url": url, "scratch_schema": scratch})
    try:
        yield runner, engine, table, scratch
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS dbo.[{table}]"))
            for name in _scratch_tables(conn, scratch):
                conn.execute(text(f"DROP TABLE IF EXISTS [{scratch}].[{name}]"))
            conn.execute(text(f"DROP SCHEMA IF EXISTS [{scratch}]"))
        engine.dispose()


def _scratch_tables(conn, scratch):
    from sqlalchemy import text

    rows = conn.execute(
        text("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :s"),
        {"s": scratch},
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Scratch runner
# ---------------------------------------------------------------------------


def test_working_copy_is_created_in_the_scratch_schema(sqlserver):
    """T-SQL has no CREATE TABLE AS, so the copy goes through SELECT … INTO."""
    runner, _engine, table, scratch = sqlserver
    schema, name = runner.create_temp_table_from_source("sess1", "dbo", table, None)
    assert schema == scratch
    assert runner.table_exists(schema, name)
    assert runner.get_row_count(schema, name) == 3


def test_a_working_copy_can_be_duplicated_and_dropped(sqlserver):
    runner, _engine, table, scratch = sqlserver
    _schema, name = runner.create_temp_table_from_source("sess2", "dbo", table, None)
    copy = f"{name}_copy"
    runner.duplicate_temp_table(name, copy)
    assert runner.get_row_count(scratch, copy) == 3
    runner.drop_temp_table(copy)
    assert not runner.table_exists(scratch, copy)


def test_the_source_table_is_never_modified(sqlserver):
    runner, _engine, table, _scratch = sqlserver
    _schema, name = runner.create_temp_table_from_source("sess3", "dbo", table, None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(runner.temp_schema, name)} SET qty = 999"],
        allowed_schema=runner.temp_schema,
    )
    assert runner.get_row_count("dbo", table) == 3
    frame = runner.run_sql_read_only(f"SELECT qty FROM dbo.[{table}] ORDER BY id")
    assert frame["qty"].tolist() != [999, 999, 999]


def test_decimal_columns_keep_their_scale_through_a_copy(sqlserver):
    """A bare NUMERIC cast in T-SQL is NUMERIC(18,0) and silently drops the
    decimals, which is why the dialect pins DECIMAL(38,10)."""
    runner, _engine, table, scratch = sqlserver
    _schema, name = runner.create_temp_table_from_source("sess4", "dbo", table, None)
    frame = runner.run_sql_read_only(
        f"SELECT price FROM {runner.quote_relation(scratch, name)} ORDER BY price"
    )
    assert float(frame["price"].iloc[-1]) == pytest.approx(10.50)


def test_source_to_working_copy_diff_uses_tsql_end_to_end(sqlserver):
    from api import sql_diff_service
    from datasources.adapters.sqlserver import SqlServerTableAdapter

    runner, _engine, table, scratch = sqlserver
    _schema, working = runner.create_temp_table_from_source("diff1", "dbo", table, None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(scratch, working)} " "SET qty = 9 WHERE id = 2"],
        allowed_schema=scratch,
    )
    base = SqlServerTableAdapter(_url(), schema="dbo", table=table)
    target = SqlServerTableAdapter(_url(), schema=scratch, table=working)

    sql_diff_service.clear_spec_cache()
    page = sql_diff_service.build_sql_diff_page(
        base,
        target,
        session_id=f"sqlserver-diff-{uuid.uuid4().hex}",
        page=1,
        page_size=10,
    )

    assert page["summary"]["rows_changed"] == 1
    assert page["summary"]["cells_changed"] == 1
    assert next(c for c in page["columns"] if c["name"] == "qty")["changed_cells"] == 1


async def test_named_working_copy_tools_and_diff_route_for_sqlserver(sqlserver):
    from datetime import datetime

    import pandas as pd

    from agents.sql_agent.agent import SQLAgent
    from agents.state import AgentState, DataContext
    from tools.sql_copies import execute_working_copy_tool

    _runner, _engine, table, scratch = sqlserver
    state = AgentState(session_id=f"sqlserver-tools-{uuid.uuid4().hex}")
    sample = pd.DataFrame({"id": [1], "sku": ["A"], "qty": [2], "price": [10.5]})
    state.set_data_context(
        DataContext(
            file_id="sqlserver-live",
            filename=f"SQL Server: dbo.{table}",
            raw_dataframe=sample.copy(deep=True),
            working_dataframe=sample.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "sqlserver",
                "connection_url": _url(),
                "schema": "dbo",
                "table": table,
                "scratch_schema": scratch,
            },
        )
    )
    agent = SQLAgent(llm_router=None)
    schema, working = await agent.ensure_temp_table(state)
    runner = agent._runner_for(state)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(schema, working)} SET qty = 9 WHERE id = 2"],
        allowed_schema=schema,
    )

    listed = await execute_working_copy_tool(agent, state, "list_working_copies", {})
    diffed = await execute_working_copy_tool(agent, state, "diff_working_copies", {})
    created = await execute_working_copy_tool(
        agent,
        state,
        "create_working_copy",
        {"name": "alternate", "from": "working"},
    )
    switched = await execute_working_copy_tool(
        agent, state, "switch_working_copy", {"name": "working"}
    )
    dropped = await execute_working_copy_tool(
        agent, state, "drop_working_copy", {"name": "alternate"}
    )

    assert "MongoDB cleaning tools are not available" not in listed
    assert '"rows_changed": 1' in diffed
    assert "alternate" in created
    assert "working" in switched
    assert "Dropped working copy 'alternate'" in dropped


def test_filter_predicates_and_duplicate_annotations_use_tsql(sqlserver):
    from datasources.adapters.base import clear_caches
    from datasources.adapters.sqlserver import SqlServerTableAdapter
    from filters import annotate_sql, sql_service
    from filters.spec import parse_spec

    _runner, _engine, table, _scratch = sqlserver
    clear_caches()
    sql_service.clear_caches()
    annotate_sql.clear_registry()
    adapter = SqlServerTableAdapter(_url(), schema="dbo", table=table)

    predicate = parse_spec(
        {
            "name": "positive-qty",
            "select": {"op": "ge", "column": "qty", "value": 2},
            "present": {"matches_only": True},
        }
    )
    selected = sql_service.build_sql_filter_page(
        adapter, predicate, session_id=f"sqlserver-filter-{uuid.uuid4().hex}"
    )
    assert selected["summary"]["matched_rows"] == 2

    duplicates = parse_spec(
        {
            "name": "duplicate-sku",
            "annotate": [{"kind": "duplicate_values", "id": "dup", "columns": ["sku"]}],
            "select": {"op": "not_null", "column": "dup_group"},
            "present": {"group_by": "dup_group", "matches_only": True},
        }
    )
    grouped = sql_service.build_sql_filter_page(
        adapter, duplicates, session_id=f"sqlserver-filter-{uuid.uuid4().hex}"
    )
    assert grouped["summary"]["group_count"] == 1
    assert grouped["summary"]["matched_rows"] == 2


# ---------------------------------------------------------------------------
# Write guard — against a real parser, on a real engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dbo.orders SELECT * FROM [stych_tmp].[t1]",
        "UPDATE dbo.orders SET qty = 1",
        "DELETE FROM dbo.orders",
        "DROP TABLE dbo.orders",
        "TRUNCATE TABLE dbo.orders",
        "SELECT * INTO dbo.stolen FROM dbo.orders",
        "EXEC sp_executesql N'DROP TABLE dbo.orders'",
        "GRANT SELECT ON dbo.orders TO public",
    ],
)
def test_the_guard_refuses_writes_outside_scratch(sql):
    with pytest.raises(WriteGuardError):
        validate_write_statements([sql], allowed_schema="stych_tmp")


def test_the_guard_permits_select_into_the_scratch_schema():
    """SELECT … INTO is T-SQL's only materialization form, so it has to be
    allowed — but only into scratch."""
    validate_write_statements(
        ["SELECT * INTO [stych_tmp].[t2] FROM dbo.orders"], allowed_schema="stych_tmp"
    )


def test_a_refused_statement_never_reaches_the_server(sqlserver):
    """The guard is the boundary; the engine must not be asked at all."""
    runner, _engine, table, _scratch = sqlserver
    with pytest.raises((WriteGuardError, SQLRunnerError)):
        runner.run_sql_read_only(f"DROP TABLE dbo.[{table}]")
    assert runner.table_exists("dbo", table)


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------


def test_top_is_emitted_instead_of_limit(sqlserver):
    runner, _engine, table, _scratch = sqlserver
    d = get_dialect("sqlserver")
    sql = f"SELECT {d.select_prefix(2)}id FROM dbo.[{table}] ORDER BY id{d.limit_suffix(2)}"
    assert len(runner.run_sql_read_only(sql)) == 2


def test_bracket_quoting_survives_a_reserved_word_column(sqlserver):
    runner, engine, _table, _scratch = sqlserver
    from sqlalchemy import text

    d = get_dialect("sqlserver")
    odd = f"odd_{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE dbo.[{odd}] ([select] INT, [group] NVARCHAR(10))"))
        conn.execute(text(f"INSERT INTO dbo.[{odd}] VALUES (1, 'x')"))
    try:
        frame = runner.run_sql_read_only(
            f"SELECT {d.quote('select')}, {d.quote('group')} FROM dbo.{d.quote(odd)}"
        )
        assert frame.iloc[0, 0] == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS dbo.[{odd}]"))


def test_a_regex_predicate_is_refused_rather_than_mistranslated():
    """SQL Server has no regular-expression operator. Emitting LIKE instead
    would silently change what the filter means."""
    with pytest.raises(UnsupportedDialectFeature):
        get_dialect("sqlserver").regex_match("[col]", "'^a'")


def test_the_iso_timestamp_format_matches_the_other_engines(sqlserver):
    runner, _engine, _table, _scratch = sqlserver
    d = get_dialect("sqlserver")
    expr = d.format_iso_timestamp(d.cast_timestamp("'2024-03-05 14:30:00'"))
    frame = runner.run_sql_read_only(f"SELECT {expr} AS iso")
    assert frame["iso"].iloc[0] == "2024-03-05T14:30:00"
