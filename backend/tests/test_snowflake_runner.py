"""Snowflake scratch-runner SQL generation, verified without a live warehouse.

These assert on the statements the runner emits. That is deliberately the layer
where warehouse ports go wrong: the BigQuery port passed its unit tests while
still emitting Postgres-quoted column identifiers, which only a live run caught.
"""

from __future__ import annotations

import pandas as pd
import pytest

from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError
from agents.sql_agent.snowflake_runner import DEFAULT_SCRATCH_SCHEMA, SnowflakeSQLRunner

CONFIG = {
    "account": "acme-xy123",
    "user": "stych",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    "warehouse": "COMPUTE_WH",
    "database": "ANALYTICS",
}


@pytest.fixture()
def runner(monkeypatch):
    """A runner whose statements are captured instead of executed."""
    executed: list[str] = []

    def _fake_execute(config, statement, parameters=(), **kwargs):
        executed.append(statement)

    def _fake_query(config, statement, parameters=(), **kwargs):
        executed.append(statement)
        if "COUNT(*)" in statement:
            return ["N"], [(7,)]
        if "INFORMATION_SCHEMA.COLUMNS" in statement:
            return ["COLUMN_NAME", "DATA_TYPE"], [(ROW_ID_COLUMN, "NUMBER"), ("id", "NUMBER")]
        if "INFORMATION_SCHEMA.TABLES" in statement:
            return ["TABLE_NAME"], [("stych_abc",), ("other",)]
        return [], []

    monkeypatch.setattr(
        "agents.sql_agent.snowflake_runner.execute_snowflake_statement", _fake_execute
    )
    monkeypatch.setattr("agents.sql_agent.snowflake_runner.run_snowflake_query", _fake_query)
    instance = SnowflakeSQLRunner(CONFIG)
    instance.executed = executed  # type: ignore[attr-defined]
    return instance


# -- identity ---------------------------------------------------------------


def test_relations_are_addressed_in_three_parts():
    """Snowflake resolves DATABASE.SCHEMA.TABLE; a two-part name would land in
    whatever schema the session happens to be using."""
    r = SnowflakeSQLRunner(CONFIG)
    assert r.quote_relation("STYCH_TEMP", "t") == '"ANALYTICS"."STYCH_TEMP"."t"'


def test_scratch_schema_defaults_to_upper_case():
    """Unquoted identifiers fold to upper case, so an upper-case default makes a
    quoted and an unquoted reference resolve to the same schema."""
    assert SnowflakeSQLRunner(CONFIG).temp_schema == DEFAULT_SCRATCH_SCHEMA == "STYCH_TEMP"


def test_missing_database_is_refused():
    with pytest.raises(SQLRunnerError, match="database is required"):
        SnowflakeSQLRunner({**CONFIG, "database": ""})


def test_missing_credentials_are_refused():
    with pytest.raises(SQLRunnerError, match="reconnect"):
        SnowflakeSQLRunner({**CONFIG, "private_key": ""})


def test_invalid_scratch_schema_is_rejected():
    with pytest.raises(SQLRunnerError, match="Invalid Snowflake scratch schema"):
        SnowflakeSQLRunner({**CONFIG, "scratch_schema": "bad name; DROP TABLE x"})


def test_credential_key_does_not_leak_the_private_key():
    key = SnowflakeSQLRunner(CONFIG).credential_key
    assert "PRIVATE KEY" not in key and "fake" not in key


# -- copies -----------------------------------------------------------------


def test_ctas_stamps_a_quoted_row_id_and_qualifies_the_source(runner):
    schema, table = runner.create_temp_table_from_source("sess-1", "PUBLIC", "orders", None)
    create = next(s for s in runner.executed if s.startswith("CREATE TABLE"))
    assert f'"{ROW_ID_COLUMN}"' in create
    assert '"ANALYTICS"."PUBLIC"."orders" AS _src' in create
    assert f'"ANALYTICS"."{schema}"."{table}"' in create
    # Postgres shorthand must not leak into a Snowflake statement.
    assert "::" not in create


def test_ctas_from_a_custom_query_wraps_the_select(runner):
    runner.create_temp_table_from_source("sess-1", "PUBLIC", None, "SELECT 1 AS a")
    create = next(s for s in runner.executed if s.startswith("CREATE TABLE"))
    assert "(SELECT 1 AS a) AS _src" in create


def test_a_non_select_custom_query_is_refused(runner):
    with pytest.raises(SQLRunnerError, match="SELECT"):
        runner.create_temp_table_from_source("s", "PUBLIC", None, "DROP TABLE orders")


def test_branching_uses_clone_so_row_ids_are_preserved(runner):
    """A CTAS branch would re-stamp row ids and break alignment with the parent."""
    runner.duplicate_temp_table("stych_a", "stych_b")
    clone = next(s for s in runner.executed if "CLONE" in s)
    assert clone.startswith("CREATE TABLE") and "CLONE" in clone
    assert "SELECT" not in clone


def test_ensure_temp_schema_is_idempotent_and_qualified(runner):
    runner.ensure_temp_schema()
    stmt = next(s for s in runner.executed if s.startswith("CREATE SCHEMA"))
    assert stmt == 'CREATE SCHEMA IF NOT EXISTS "ANALYTICS"."STYCH_TEMP"'


# -- write guard ------------------------------------------------------------


def test_writes_outside_the_scratch_schema_are_refused(runner):
    with pytest.raises(SQLRunnerError, match="must target schema|not schema-qualified"):
        runner.execute_statements_transaction(['DROP TABLE "ANALYTICS"."PUBLIC"."orders"'])


def test_writes_inside_the_scratch_schema_are_allowed(runner):
    runner.execute_statements_transaction(
        ['UPDATE "ANALYTICS"."STYCH_TEMP"."t" SET a = 1 WHERE id = 2']
    )
    assert any("STYCH_TEMP" in s and s.startswith("UPDATE") for s in runner.executed)


def test_stacked_statements_are_refused(runner):
    with pytest.raises(SQLRunnerError, match="Multiple statements"):
        runner.execute_statements_transaction(
            ['UPDATE "ANALYTICS"."STYCH_TEMP"."t" SET a = 1; DROP TABLE x']
        )


def test_read_path_rejects_a_mutating_statement(runner):
    with pytest.raises(SQLRunnerError):
        runner.run_sql_read_only('DELETE FROM "ANALYTICS"."STYCH_TEMP"."t"')


# -- introspection ----------------------------------------------------------


def test_row_count_reads_through_the_qualified_relation(runner):
    assert runner.get_row_count("STYCH_TEMP", "t") == 7
    assert any('"ANALYTICS"."STYCH_TEMP"."t"' in s for s in runner.executed)


def test_table_columns_are_returned_in_the_shared_shape(runner):
    columns = runner.get_table_columns("STYCH_TEMP", "t")
    assert [c["column_name"] for c in columns] == [ROW_ID_COLUMN, "id"]
    assert all("data_type" in c for c in columns)


def test_prefix_listing_filters_to_stych_tables(runner):
    assert runner.list_temp_tables_with_prefix("stych_") == ["stych_abc"]


def test_read_returns_a_dataframe(runner):
    assert isinstance(runner.run_sql_read_only("SELECT 1"), pd.DataFrame)


# -- protocol registration --------------------------------------------------


def test_the_factory_resolves_snowflake():
    from agents.sql_agent.protocols import supports_source_execution

    assert supports_source_execution({"source": "snowflake"}) is True


# -- adapter export + annotation wiring -------------------------------------


def _adapter(monkeypatch, captured):
    from datasources.adapters.base import SourceCacheInfo
    from datasources.adapters.snowflake import SnowflakeTableAdapter

    def _fake_stream(config, statement, parameters=(), **kwargs):
        captured.append(statement)
        yield ["id", "name"], [(1, "ada"), (2, "grace")]

    def _fake_run(config, statement, parameters=(), **kwargs):
        captured.append(statement)
        return ["COLUMN_NAME", "DATA_TYPE"], [("id", "NUMBER"), ("name", "TEXT")]

    monkeypatch.setattr("datasources.adapters.snowflake.stream_snowflake_query", _fake_stream)
    monkeypatch.setattr("datasources.adapters.snowflake.run_snowflake_query", _fake_run)
    return SnowflakeTableAdapter(
        CONFIG,
        database="ANALYTICS",
        schema="PUBLIC",
        table="orders",
        custom_query=None,
        cache_info=SourceCacheInfo(key=("sf", "orders")),
    )


def test_export_streams_from_a_qualified_relation(monkeypatch):
    from datasources.adapters.base import clear_caches

    clear_caches()
    captured: list[str] = []
    adapter = _adapter(monkeypatch, captured)
    batches = list(adapter.stream_batches(batch_rows=10, max_rows=2))
    assert len(batches) == 1 and list(batches[0].columns) == ["id", "name"]
    # The schema lookup also runs; pick the export statement specifically.
    select = next(s for s in captured if "INFORMATION_SCHEMA" not in s and "LIMIT" in s)
    assert '"ANALYTICS"."PUBLIC"."orders"' in select
    assert "LIMIT 2" in select
    assert "::" not in select


def test_annotation_materialization_is_available_on_snowflake(monkeypatch):
    """Snowflake has a scratch runner, so agent SQL annotation steps can be
    written into it — unlike fuzzy grouping, which would need a sample."""
    from types import SimpleNamespace

    from filters.annotate_sql import _can_compute_fuzzy, _can_materialize

    adapter = SimpleNamespace(
        dialect="snowflake", ident_quote='"', connection_config=CONFIG, db_schema="PUBLIC"
    )
    assert _can_materialize(adapter) is True
    assert _can_compute_fuzzy(adapter) is False
