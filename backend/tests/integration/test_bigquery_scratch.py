"""Real-BigQuery checks for the scratch-dataset runner.

Skipped unless credentials are provided:

    STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON_FILE=/path/to/sa.json \
    STYCH_TEST_BIGQUERY_PROJECT_ID=my-project \
        uv run pytest tests/integration/test_bigquery_scratch.py

The service account needs BigQuery Job User + Data Editor. Every dataset this
module creates is dropped in a fixture, including on failure.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from agents.sql_agent.bigquery_runner import BigQuerySQLRunner  # noqa: E402
from agents.sql_agent.runner import ROW_ID_COLUMN, SQLRunnerError  # noqa: E402
from datasources.bigquery import _client  # noqa: E402
from datasources.warehouse import encode_connection_config  # noqa: E402

# A location deliberately different from BigQuery's US default, so a runner that
# ignored the source's location would fail loudly here instead of silently
# working for US-only users.
SOURCE_LOCATION = "europe-west1"


def _service_account_json() -> str:
    path = os.getenv("STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON_FILE", "")
    if path:
        return Path(path).read_text()
    return os.getenv("STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON", "")


def _config() -> dict:
    raw = _service_account_json()
    project = os.getenv("STYCH_TEST_BIGQUERY_PROJECT_ID", "")
    if not raw or not project:
        pytest.skip(
            "Set STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON_FILE and "
            "STYCH_TEST_BIGQUERY_PROJECT_ID for BigQuery integration tests"
        )
    return {"service_account_json": raw, "project_id": project}


@pytest.fixture()
def bq():
    """A throwaway source dataset plus a runner on its own scratch dataset."""
    config = _config()
    from google.cloud import bigquery

    client = _client(config)
    project = config["project_id"]
    source_ds = f"stych_src_{uuid.uuid4().hex[:6]}"
    scratch_ds = f"stych_tmp_{uuid.uuid4().hex[:6]}"

    dataset = bigquery.Dataset(f"{project}.{source_ds}")
    dataset.location = SOURCE_LOCATION
    client.create_dataset(dataset)
    client.query(
        f"CREATE TABLE `{project}.{source_ds}.orders` AS SELECT * FROM UNNEST("
        "[STRUCT(1 AS id, 'ada' AS name), (2, 'grace'), (3, 'alan')])"
    ).result()

    meta = {
        "source": "bigquery",
        "schema": source_ds,
        "connection_config": encode_connection_config({**config, "scratch_schema": scratch_ds}),
    }
    runner = BigQuerySQLRunner.from_meta(meta)
    try:
        yield runner, client, project, source_ds
    finally:
        for name in (scratch_ds, source_ds):
            client.delete_dataset(f"{project}.{name}", delete_contents=True, not_found_ok=True)


def test_scratch_dataset_inherits_the_source_location(bq):
    """A query cannot read one location and write another, so a scratch dataset
    in the wrong region breaks every working copy."""
    runner, client, project, source_ds = bq
    schema, _table = runner.create_temp_table_from_source("sess", source_ds, "orders", None)
    assert client.get_dataset(f"{project}.{schema}").location == SOURCE_LOCATION


def test_working_copy_is_stamped_with_a_row_id(bq):
    runner, _client_, _project, source_ds = bq
    schema, table = runner.create_temp_table_from_source("sess", source_ds, "orders", None)
    columns = [c["column_name"] for c in runner.get_table_columns(schema, table)]
    assert ROW_ID_COLUMN in columns
    assert runner.get_row_count(schema, table) == 3


def test_row_ids_survive_branching_so_copies_stay_aligned(bq):
    """Copy-vs-copy diff is exact only if a branch keeps the parent's row ids."""
    runner, _client_, _project, source_ds = bq
    schema, table = runner.create_temp_table_from_source("sess", source_ds, "orders", None)
    branch = f"{table}_b"
    runner.duplicate_temp_table(table, branch)

    def ids(name):
        frame = runner.run_sql_read_only(
            f"SELECT {ROW_ID_COLUMN}, id FROM {runner.quote_relation(schema, name)} ORDER BY id"
        )
        return list(frame[ROW_ID_COLUMN])

    assert ids(table) == ids(branch)


def test_dml_runs_against_a_copy(bq):
    runner, _client_, _project, source_ds = bq
    schema, table = runner.create_temp_table_from_source("sess", source_ds, "orders", None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(schema, table)} SET name = 'ADA' WHERE id = 1"]
    )
    frame = runner.run_sql_read_only(
        f"SELECT name FROM {runner.quote_relation(schema, table)} WHERE id = 1"
    )
    assert frame.iloc[0]["name"] == "ADA"


def test_writes_outside_the_scratch_dataset_are_refused(bq):
    """The source table must be unreachable from the write path."""
    runner, _client_, project, source_ds = bq
    with pytest.raises(SQLRunnerError, match="must target schema|not schema-qualified"):
        runner.execute_statements_transaction([f"DROP TABLE `{project}.{source_ds}.orders`"])


def test_source_table_is_untouched_by_a_copy_mutation(bq):
    runner, _client_, _project, source_ds = bq
    schema, table = runner.create_temp_table_from_source("sess", source_ds, "orders", None)
    runner.execute_statements_transaction(
        [f"UPDATE {runner.quote_relation(schema, table)} SET name = 'CHANGED' WHERE id = 1"]
    )
    original = runner.run_sql_read_only(
        f"SELECT name FROM {runner.quote_relation(source_ds, 'orders')} WHERE id = 1"
    )
    assert original.iloc[0]["name"] == "ada"
