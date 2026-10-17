"""Real-Snowflake checks for the scratch-schema runner.

Skipped unless credentials are provided:

    STYCH_TEST_SNOWFLAKE_ACCOUNT=acme-xy123 \
    STYCH_TEST_SNOWFLAKE_USER=stych \
    STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_FILE=/path/to/rsa_key.p8 \
    STYCH_TEST_SNOWFLAKE_WAREHOUSE=COMPUTE_WH \
    STYCH_TEST_SNOWFLAKE_DATABASE=ANALYTICS \
        uv run pytest tests/integration/test_snowflake_scratch.py

The role needs CREATE SCHEMA on the database plus CREATE TABLE in the schema it
creates. Every object these tests create is dropped in the fixture, including on
failure.

These cover what unit tests structurally cannot: that the generated SQL is
accepted by the engine. The BigQuery port passed its unit tests while emitting
identifiers the engine rejected, so treat a green unit run as necessary, not
sufficient.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("snowflake.connector")

from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError  # noqa: E402
from agents.sql_agent.snowflake_runner import SnowflakeSQLRunner  # noqa: E402
from datasources.snowflake import (  # noqa: E402
    execute_snowflake_statement,
    run_snowflake_query,
)


def _private_key() -> str:
    path = os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_FILE", "")
    if path:
        return Path(path).read_text()
    return os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY", "")


def _config() -> dict:
    required = {
        "account": os.getenv("STYCH_TEST_SNOWFLAKE_ACCOUNT", ""),
        "user": os.getenv("STYCH_TEST_SNOWFLAKE_USER", ""),
        "warehouse": os.getenv("STYCH_TEST_SNOWFLAKE_WAREHOUSE", ""),
        "database": os.getenv("STYCH_TEST_SNOWFLAKE_DATABASE", ""),
    }
    key = _private_key()
    if not key or not all(required.values()):
        pytest.skip(
            "Set STYCH_TEST_SNOWFLAKE_ACCOUNT/USER/WAREHOUSE/DATABASE and "
            "STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_FILE for Snowflake integration tests"
        )
    config = {**required, "private_key": key}
    passphrase = os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    if passphrase:
        config["private_key_passphrase"] = passphrase
    role = os.getenv("STYCH_TEST_SNOWFLAKE_ROLE")
    if role:
        config["role"] = role
    return config


@pytest.fixture()
def sf():
    """A throwaway source schema plus a runner on its own scratch schema."""
    config = _config()
    database = config["database"]
    source_schema = f"STYCH_SRC_{uuid.uuid4().hex[:6].upper()}"
    scratch_schema = f"STYCH_TMP_{uuid.uuid4().hex[:6].upper()}"

    execute_snowflake_statement(config, f'CREATE SCHEMA "{database}"."{source_schema}"')
    execute_snowflake_statement(
        config,
        f'CREATE TABLE "{database}"."{source_schema}"."orders" AS '
        "SELECT 1 AS id, 'ada' AS name UNION ALL SELECT 2, 'grace' UNION ALL SELECT 3, 'alan'",
    )
    runner = SnowflakeSQLRunner({**config, "scratch_schema": scratch_schema})
    try:
        yield runner, config, database, source_schema
    finally:
        for name in (scratch_schema, source_schema):
            try:
                execute_snowflake_statement(
                    config, f'DROP SCHEMA IF EXISTS "{database}"."{name}" CASCADE'
                )
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass


def test_working_copy_is_stamped_with_a_row_id(sf):
    runner, _config, _db, source_schema = sf
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    columns = [c["column_name"] for c in runner.get_table_columns(schema, table)]
    assert ROW_ID_COLUMN in columns
    assert runner.get_row_count(schema, table) == 3


def test_row_ids_survive_cloning_so_copies_stay_aligned(sf):
    """Copy-vs-copy diff is exact only if a branch keeps the parent's row ids."""
    runner, _config, _db, source_schema = sf
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    branch = f"{table}_b"
    runner.duplicate_temp_table(table, branch)

    def ids(name):
        frame = runner.run_sql_read_only(
            f'SELECT "{ROW_ID_COLUMN}", "ID" FROM {runner.quote_relation(schema, name)} '
            'ORDER BY "ID"'
        )
        return list(frame[frame.columns[0]])

    assert ids(table) == ids(branch)


def test_dml_runs_against_a_copy(sf):
    runner, _config, _db, source_schema = sf
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    runner.execute_statements_transaction(
        [f'UPDATE {runner.quote_relation(schema, table)} SET "NAME" = \'ADA\' WHERE "ID" = 1']
    )
    frame = runner.run_sql_read_only(
        f'SELECT "NAME" FROM {runner.quote_relation(schema, table)} WHERE "ID" = 1'
    )
    assert frame.iloc[0][frame.columns[0]] == "ADA"


def test_writes_outside_the_scratch_schema_are_refused(sf):
    runner, _config, database, source_schema = sf
    with pytest.raises(SQLRunnerError, match="must target schema|not schema-qualified"):
        runner.execute_statements_transaction(
            [f'DROP TABLE "{database}"."{source_schema}"."orders"']
        )


def test_source_table_is_untouched_by_a_copy_mutation(sf):
    runner, config, database, source_schema = sf
    schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    runner.execute_statements_transaction(
        [f'UPDATE {runner.quote_relation(schema, table)} SET "NAME" = \'X\' WHERE "ID" = 1']
    )
    _names, rows = run_snowflake_query(
        config,
        f'SELECT "NAME" FROM "{database}"."{source_schema}"."orders" WHERE "ID" = 1',
    )
    assert rows[0][0] == "ada"


def test_prefix_listing_finds_the_working_copy(sf):
    runner, _config, _db, source_schema = sf
    _schema, table = runner.create_temp_table_from_source("sess", source_schema, "orders", None)
    assert table in runner.list_temp_tables_with_prefix("stych_")


def test_export_streams_the_full_relation(sf):
    """The adapter's export path must be accepted by the engine, not just built."""
    from datasources.adapters.base import SourceCacheInfo
    from datasources.adapters.snowflake import SnowflakeTableAdapter

    runner, config, database, source_schema = sf
    adapter = SnowflakeTableAdapter(
        config,
        database=database,
        schema=source_schema,
        table="orders",
        custom_query=None,
        cache_info=SourceCacheInfo(key=("sf-it", source_schema)),
    )
    total = sum(len(batch) for batch in adapter.stream_batches(batch_rows=2))
    assert total == 3
