"""Real-Redshift checks for the scratch-schema runner.

Skipped unless credentials are provided:

    STYCH_TEST_REDSHIFT_HOST=cluster.abc.us-east-1.redshift.amazonaws.com \
    STYCH_TEST_REDSHIFT_DATABASE=analytics \
    STYCH_TEST_REDSHIFT_USER=stych \
    STYCH_TEST_REDSHIFT_ACCESS_KEY_ID=... \
    STYCH_TEST_REDSHIFT_SECRET_ACCESS_KEY=... \
        uv run pytest tests/integration/test_redshift_scratch.py

The IAM principal needs redshift:GetClusterCredentials, and the database user
needs CREATE on the database plus CREATE TABLE in the schema it creates. Every
object these tests create is dropped in the fixture, including on failure.

These cover what unit tests structurally cannot: that the generated SQL is
accepted by the engine. The BigQuery port passed its unit tests while emitting
identifiers the engine rejected, so a green unit run is necessary, not
sufficient.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("redshift_connector")

from agents.sql_agent.redshift_runner import RedshiftSQLRunner  # noqa: E402
from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError  # noqa: E402
from datasources.redshift import execute_redshift_statement, run_redshift_query  # noqa: E402


def _config() -> dict:
    fields = {
        "host": os.getenv("STYCH_TEST_REDSHIFT_HOST", ""),
        "database": os.getenv("STYCH_TEST_REDSHIFT_DATABASE", ""),
        "user": os.getenv("STYCH_TEST_REDSHIFT_USER", ""),
        "access_key_id": os.getenv("STYCH_TEST_REDSHIFT_ACCESS_KEY_ID", ""),
        "secret_access_key": os.getenv("STYCH_TEST_REDSHIFT_SECRET_ACCESS_KEY", ""),
    }
    if not all(fields.values()):
        pytest.skip(
            "Set STYCH_TEST_REDSHIFT_HOST/DATABASE/USER/ACCESS_KEY_ID/SECRET_ACCESS_KEY "
            "for Redshift integration tests"
        )
    fields["port"] = int(os.getenv("STYCH_TEST_REDSHIFT_PORT", "5439"))
    for optional in ("region", "cluster_identifier", "session_token"):
        value = os.getenv(f"STYCH_TEST_REDSHIFT_{optional.upper()}")
        if value:
            fields[optional] = value
    return fields


@pytest.fixture()
def rs():
    """A throwaway source schema plus a runner on its own scratch schema."""
    config = _config()
    source_schema = f"stych_src_{uuid.uuid4().hex[:6]}"
    scratch_schema = f"stych_tmp_{uuid.uuid4().hex[:6]}"

    execute_redshift_statement(config, f'CREATE SCHEMA "{source_schema}"')
    execute_redshift_statement(
        config,
        f'CREATE TABLE "{source_schema}"."orders" AS '
        "SELECT 1 AS id, 'ada'::varchar AS name UNION ALL "
        "SELECT 2, 'grace' UNION ALL SELECT 3, 'alan'",
    )
    runner = RedshiftSQLRunner({**config, "scratch_schema": scratch_schema})
    try:
        yield runner, config, source_schema
    finally:
        for name in (scratch_schema, source_schema):
            try:
                execute_redshift_statement(config, f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass


def test_working_copy_is_stamped_with_a_row_id(rs):
    runner, _config, source_schema = rs
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    columns = [c["column_name"] for c in runner.get_table_columns(schema, table)]
    assert ROW_ID_COLUMN in columns
    assert runner.get_row_count(schema, table) == 3


def test_row_ids_survive_branching_so_copies_stay_aligned(rs):
    """Redshift has no zero-copy clone, so this proves the real copy carries the
    parent's stamped id rather than re-generating it."""
    runner, _config, source_schema = rs
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    branch = f"{table}_b"
    runner.duplicate_temp_table(table, branch)

    def ids(name):
        frame = runner.run_sql_read_only(
            f'SELECT "{ROW_ID_COLUMN}", id FROM {runner.quote_relation(schema, name)} ORDER BY id'
        )
        return list(frame[frame.columns[0]])

    assert ids(table) == ids(branch)


def test_dml_runs_against_a_copy(rs):
    runner, _config, source_schema = rs
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(schema, table)} SET name = 'ADA' WHERE id = 1"]
    )
    frame = runner.run_sql_read_only(
        f"SELECT name FROM {runner.quote_relation(schema, table)} WHERE id = 1"
    )
    assert frame.iloc[0][frame.columns[0]] == "ADA"


def test_writes_outside_the_scratch_schema_are_refused(rs):
    runner, _config, source_schema = rs
    with pytest.raises(SQLRunnerError, match="must target schema|not schema-qualified"):
        runner.execute_statements_transaction([f'DROP TABLE "{source_schema}"."orders"'])


def test_source_table_is_untouched_by_a_copy_mutation(rs):
    runner, config, source_schema = rs
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(schema, table)} SET name = 'X' WHERE id = 1"]
    )
    _names, rows = run_redshift_query(
        config, f'SELECT name FROM "{source_schema}"."orders" WHERE id = 1'
    )
    assert rows[0][0] == "ada"


def test_long_values_survive_a_text_cast(rs):
    """TEXT is VARCHAR(256) on Redshift; the dialect must cast wide enough that
    a long value is not silently truncated mid-comparison."""
    runner, config, source_schema = rs
    execute_redshift_statement(
        config,
        f'CREATE TABLE "{source_schema}"."wide" AS SELECT 1 AS id, '
        "REPEAT('x', 600)::varchar(65535) AS blob",
    )
    frame = runner.run_sql_read_only(
        f'SELECT LENGTH(CAST(blob AS VARCHAR(65535))) AS n FROM "{source_schema}"."wide"'
    )
    assert int(frame.iloc[0][frame.columns[0]]) == 600


def test_export_streams_the_full_relation(rs):
    from datasources.adapters.base import SourceCacheInfo
    from datasources.adapters.redshift import RedshiftTableAdapter

    _runner, config, source_schema = rs
    adapter = RedshiftTableAdapter(
        config,
        database=None,
        schema=source_schema,
        table="orders",
        custom_query=None,
        cache_info=SourceCacheInfo(key=("rs-it", source_schema)),
    )
    assert sum(len(batch) for batch in adapter.stream_batches(batch_rows=2)) == 3
