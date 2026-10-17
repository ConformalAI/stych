"""Redshift scratch-runner SQL generation, verified without a live warehouse.

Redshift is Postgres-shaped but differs where it matters: IAM auth through
Amazon's own driver, no zero-copy clone, no physical row address, and a TEXT
alias that truncates. These assert the generated SQL respects all four.
"""

from __future__ import annotations

import pandas as pd
import pytest

from agents.sql_agent.redshift_runner import DEFAULT_SCRATCH_SCHEMA, RedshiftSQLRunner
from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError

CONFIG = {
    "host": "cluster.abc123.us-east-1.redshift.amazonaws.com",
    "database": "analytics",
    "user": "stych",
    "access_key_id": "AKIAFAKE",
    "secret_access_key": "secret-fake",
}


@pytest.fixture()
def runner(monkeypatch):
    """A runner whose statements are captured instead of executed."""
    executed: list[str] = []

    def _fake_execute(config, statement, parameters=(), **kwargs):
        executed.append(statement)

    def _fake_query(config, statement, parameters=(), **kwargs):
        executed.append(statement)
        if "count(*)" in statement.lower():
            return ["n"], [(7,)]
        if "information_schema.columns" in statement.lower():
            return ["column_name", "data_type"], [(ROW_ID_COLUMN, "bigint"), ("id", "integer")]
        if "information_schema.tables" in statement.lower():
            return ["table_name"], [("stych_abc",), ("other",)]
        return [], []

    monkeypatch.setattr(
        "agents.sql_agent.redshift_runner.execute_redshift_statement", _fake_execute
    )
    monkeypatch.setattr("agents.sql_agent.redshift_runner.run_redshift_query", _fake_query)
    instance = RedshiftSQLRunner(CONFIG)
    instance.executed = executed  # type: ignore[attr-defined]
    return instance


# -- identity ---------------------------------------------------------------


def test_relations_are_two_part_because_the_database_is_per_connection():
    """Unlike Snowflake, Redshift cannot address another database in a query."""
    r = RedshiftSQLRunner(CONFIG)
    assert r.quote_relation("stych_temp", "t") == '"stych_temp"."t"'


def test_scratch_schema_defaults_to_lower_case():
    """Redshift folds unquoted identifiers down, so a lower-case default keeps
    quoted and unquoted references pointing at the same schema."""
    assert RedshiftSQLRunner(CONFIG).temp_schema == DEFAULT_SCRATCH_SCHEMA == "stych_temp"


def test_missing_database_is_refused():
    with pytest.raises(SQLRunnerError, match="database is required"):
        RedshiftSQLRunner({**CONFIG, "database": ""})


def test_missing_credentials_are_refused():
    with pytest.raises(SQLRunnerError, match="reconnect"):
        RedshiftSQLRunner({**CONFIG, "access_key_id": ""})


def test_invalid_scratch_schema_is_rejected():
    with pytest.raises(SQLRunnerError, match="Invalid Redshift scratch schema"):
        RedshiftSQLRunner({**CONFIG, "scratch_schema": "bad; DROP TABLE x"})


def test_credential_key_does_not_leak_the_secret():
    key = RedshiftSQLRunner(CONFIG).credential_key
    assert "secret-fake" not in key and "AKIAFAKE" not in key


# -- copies -----------------------------------------------------------------


def test_ctas_stamps_a_quoted_row_id_and_qualifies_the_source(runner):
    schema, table = runner.create_temp_table_from_source("sess-1", "public", "orders", None)
    create = next(s for s in runner.executed if s.startswith("CREATE TABLE"))
    assert f'"{ROW_ID_COLUMN}"' in create
    assert '"public"."orders" AS _src' in create
    assert f'"{schema}"."{table}"' in create
    assert "ROW_NUMBER() OVER ()" in create


def test_branching_carries_the_parent_row_id_forward(runner):
    """Redshift has no CLONE, so the branch is a real copy — but SELECT * must
    carry the stamped id rather than re-generating it."""
    runner.duplicate_temp_table("stych_a", "stych_b")
    stmt = next(s for s in runner.executed if "stych_b" in s)
    assert "SELECT * FROM" in stmt
    assert "ROW_NUMBER" not in stmt


def test_ensure_temp_schema_is_idempotent(runner):
    runner.ensure_temp_schema()
    assert 'CREATE SCHEMA IF NOT EXISTS "stych_temp"' in runner.executed


def test_a_non_select_custom_query_is_refused(runner):
    with pytest.raises(SQLRunnerError, match="SELECT"):
        runner.create_temp_table_from_source("s", "public", None, "DROP TABLE orders")


# -- write guard ------------------------------------------------------------


def test_writes_outside_the_scratch_schema_are_refused(runner):
    with pytest.raises(SQLRunnerError, match="must target schema|not schema-qualified"):
        runner.execute_statements_transaction(['DROP TABLE "public"."orders"'])


def test_stacked_statements_are_refused(runner):
    with pytest.raises(SQLRunnerError, match="Multiple statements"):
        runner.execute_statements_transaction(['UPDATE "stych_temp"."t" SET a = 1; DROP TABLE x'])


def test_read_path_rejects_a_mutating_statement(runner):
    with pytest.raises(SQLRunnerError):
        runner.run_sql_read_only('DELETE FROM "stych_temp"."t"')


# -- introspection ----------------------------------------------------------


def test_row_count_reads_through_the_qualified_relation(runner):
    assert runner.get_row_count("stych_temp", "t") == 7


def test_table_columns_are_returned_in_the_shared_shape(runner):
    columns = runner.get_table_columns("stych_temp", "t")
    assert [c["column_name"] for c in columns] == [ROW_ID_COLUMN, "id"]


def test_prefix_listing_filters_to_stych_tables(runner):
    assert runner.list_temp_tables_with_prefix("stych_") == ["stych_abc"]


def test_read_returns_a_dataframe(runner):
    assert isinstance(runner.run_sql_read_only("SELECT 1"), pd.DataFrame)


# -- dialect + registration -------------------------------------------------


def test_text_casts_avoid_the_varchar_256_truncation(runner):
    """CAST(x AS TEXT) is VARCHAR(256) on Redshift and would cut long values."""
    from datasources.sql_dialect import get_dialect

    expr = get_dialect("redshift").cast_text('"c"')
    assert "VARCHAR(65535)" in expr


def test_the_factory_resolves_redshift():
    from agents.sql_agent.protocols import supports_source_execution

    assert supports_source_execution({"source": "redshift"}) is True


def test_annotation_materialization_is_available_on_redshift():
    from types import SimpleNamespace

    from filters.annotate_sql import _can_compute_fuzzy, _can_materialize

    adapter = SimpleNamespace(
        dialect="redshift", ident_quote='"', connection_config=CONFIG, db_schema="public"
    )
    assert _can_materialize(adapter) is True
    assert _can_compute_fuzzy(adapter) is False
