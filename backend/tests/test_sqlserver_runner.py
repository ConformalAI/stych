"""SQL Server scratch-runner SQL generation, verified without a live server.

T-SQL is the first engine here with no CREATE TABLE AS SELECT, no LIMIT, and no
regex operator — three things the shared code previously assumed every engine
had. These assert the generated statements respect all three.
"""

from __future__ import annotations

import pytest

from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError
from agents.sql_agent.sqlserver_runner import DEFAULT_SCRATCH_SCHEMA, SqlServerSQLRunner
from datasources.sql_dialect import UnsupportedDialectFeature, get_dialect

URL = "mssql+pymssql://user:pw@host:1433/analytics"
CONFIG = {"connection_url": URL}


@pytest.fixture()
def runner(monkeypatch):
    executed: list[str] = []
    instance = SqlServerSQLRunner(CONFIG)
    monkeypatch.setattr(
        type(instance), "_execute", lambda self, sql, **kw: executed.append(sql), raising=False
    )
    instance.executed = executed  # type: ignore[attr-defined]
    return instance


# -- identity ---------------------------------------------------------------


def test_relations_use_bracket_quoting():
    """Brackets always work; double quotes need QUOTED_IDENTIFIER ON."""
    assert SqlServerSQLRunner(CONFIG).quote_relation("stych_temp", "t") == "[stych_temp].[t]"


def test_scratch_schema_default():
    assert SqlServerSQLRunner(CONFIG).temp_schema == DEFAULT_SCRATCH_SCHEMA == "stych_temp"


def test_missing_or_redacted_credentials_are_refused():
    for url in ("", "[REDACTED]", "not-a-url"):
        with pytest.raises(SQLRunnerError, match="unavailable"):
            SqlServerSQLRunner({"connection_url": url})


def test_writes_are_atomic_unlike_the_cloud_warehouses():
    """A SQLAlchemy transaction wraps the batch, so a failure rolls back."""
    assert SqlServerSQLRunner.atomic_writes is True


# -- statement shapes -------------------------------------------------------


def test_materialization_uses_select_into_not_ctas(runner):
    """T-SQL has no CREATE TABLE AS SELECT."""
    schema, table = runner.create_temp_table_from_source("sess", "dbo", "orders", None)
    stmt = next(s for s in runner.executed if "INTO" in s)
    assert stmt.startswith("SELECT ")
    assert f"INTO [{schema}].[{table}]" in stmt
    assert "CREATE TABLE" not in stmt
    assert "[dbo].[orders] AS _src" in stmt


def test_the_row_id_is_stamped_with_a_t_sql_window(runner):
    runner.create_temp_table_from_source("sess", "dbo", "orders", None)
    stmt = next(s for s in runner.executed if "INTO" in s)
    assert f"[{ROW_ID_COLUMN}]" in stmt
    # ROW_NUMBER requires an ORDER BY in T-SQL; a constant satisfies it.
    assert "ORDER BY (SELECT NULL)" in stmt


def test_branching_carries_the_parent_row_id_forward(runner):
    runner.duplicate_temp_table("stych_a", "stych_b")
    stmt = next(s for s in runner.executed if "stych_b" in s)
    assert "SELECT * INTO [stych_temp].[stych_b]" in stmt
    assert "ROW_NUMBER" not in stmt


def test_schema_creation_works_on_servers_without_if_not_exists(runner):
    runner.ensure_temp_schema()
    stmt = next(s for s in runner.executed if "SCHEMA" in s)
    assert "sys.schemas" in stmt and "EXEC(" in stmt


def test_drop_does_not_require_sql_server_2016(runner):
    runner.drop_temp_table("stych_a")
    stmt = next(s for s in runner.executed if "DROP" in s)
    assert "OBJECT_ID" in stmt


# -- dialect ----------------------------------------------------------------


def test_row_caps_are_a_prefix_not_a_suffix():
    """LIMIT does not exist in T-SQL; the cap is SELECT TOP n."""
    d = get_dialect("sqlserver")
    assert d.select_prefix(50) == "TOP 50 "
    assert d.limit_suffix(50) == ""
    assert d.select_prefix(None) == ""


def test_regex_is_refused_rather_than_approximated():
    """T-SQL has no regex operator. LIKE would match different rows, so the
    predicate is refused instead of silently changing meaning."""
    with pytest.raises(UnsupportedDialectFeature, match="no regular-expression"):
        get_dialect("sqlserver").regex_match("c", "'p'")


def test_numeric_cast_does_not_truncate_decimals():
    """A bare NUMERIC cast is NUMERIC(18,0) and drops the fraction."""
    expr = get_dialect("sqlserver").cast_numeric("c")
    assert "DECIMAL(38,10)" in expr


def test_text_cast_uses_nvarchar_max():
    assert "NVARCHAR(MAX)" in get_dialect("sqlserver").cast_text("c")


def test_count_if_avoids_the_filter_clause():
    assert "SUM(CASE WHEN" in get_dialect("sqlserver").count_if("x")


def test_case_insensitive_match_folds_both_sides():
    """Case sensitivity is a collation property here, not an operator."""
    expr = get_dialect("sqlserver").ilike("c", ":p")
    assert expr.count("LOWER(") == 2
    assert "ILIKE" not in expr


def test_primary_keys_are_trusted_because_sql_server_enforces_them():
    lookup = get_dialect("sqlserver").primary_key_query("dbo", "orders")
    assert lookup is not None
    sql, params = lookup
    assert "PRIMARY KEY" in sql and params == {"schema": "dbo", "tname": "orders"}


# -- registration -----------------------------------------------------------


def test_the_factory_resolves_sqlserver():
    from agents.sql_agent.protocols import supports_source_execution

    assert supports_source_execution({"source": "sqlserver"}) is True


def test_select_into_is_allowed_only_into_scratch():
    """T-SQL materialization must not become a hole in the write guard."""
    from agents.sql_agent.write_guard import WriteGuardError, validate_write_statements

    validate_write_statements(
        ["SELECT TOP 5 a INTO [stych_temp].[t] FROM [dbo].[orders]"],
        allowed_schema="stych_temp",
    )
    with pytest.raises(WriteGuardError):
        validate_write_statements(
            ["SELECT * INTO [dbo].[stolen] FROM [stych_temp].[t]"], allowed_schema="stych_temp"
        )
