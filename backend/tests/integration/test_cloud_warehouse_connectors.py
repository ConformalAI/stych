"""Credential-gated smoke tests for the real cloud warehouse drivers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from datasources.bigquery import test_bigquery_connection as check_bigquery_connection
from datasources.databricks import test_databricks_connection as check_databricks_connection
from datasources.redshift import test_redshift_connection as check_redshift_connection
from datasources.snowflake import test_snowflake_connection as check_snowflake_connection


def _required(prefix: str, names: tuple[str, ...]) -> dict[str, Any]:
    values = {name.lower(): os.getenv(f"{prefix}_{name}", "") for name in names}
    if not all(values.values()):
        pytest.skip(f"Set all {prefix}_* credentials to run this integration test")
    return values


def _snowflake_private_key() -> str:
    path = os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_FILE", "")
    if path:
        return Path(path).read_text()
    raw = os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY", "")
    return raw.replace("\\n", "\n")


def test_real_snowflake_connection():
    config = _required(
        "STYCH_TEST_SNOWFLAKE",
        ("ACCOUNT", "USER", "WAREHOUSE", "DATABASE"),
    )
    private_key = _snowflake_private_key()
    if not private_key:
        pytest.skip("Set STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_FILE or STYCH_TEST_SNOWFLAKE_PRIVATE_KEY")
    config["private_key"] = private_key
    passphrase = os.getenv("STYCH_TEST_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    if passphrase:
        config["private_key_passphrase"] = passphrase
    role = os.getenv("STYCH_TEST_SNOWFLAKE_ROLE")
    if role:
        config["role"] = role
    success, message = check_snowflake_connection(config)
    assert success, message


def test_real_redshift_connection():
    config = _required(
        "STYCH_TEST_REDSHIFT",
        ("HOST", "DATABASE", "USER", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"),
    )
    config["port"] = int(os.getenv("STYCH_TEST_REDSHIFT_PORT", "5439"))
    for name in ("SESSION_TOKEN", "REGION", "CLUSTER_IDENTIFIER"):
        value = os.getenv(f"STYCH_TEST_REDSHIFT_{name}")
        if value:
            config[name.lower()] = value
    success, message = check_redshift_connection(config)
    assert success, message


def _bigquery_service_account_json() -> str:
    path = os.getenv("STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON_FILE", "")
    if path:
        return Path(path).read_text()
    return os.getenv("STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON", "")


def test_real_bigquery_connection():
    service_account_json = _bigquery_service_account_json()
    if not service_account_json:
        pytest.skip(
            "Set STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON_FILE or "
            "STYCH_TEST_BIGQUERY_SERVICE_ACCOUNT_JSON"
        )
    config: dict[str, Any] = {"service_account_json": service_account_json}
    project_id = os.getenv("STYCH_TEST_BIGQUERY_PROJECT_ID")
    if project_id:
        config["project_id"] = project_id
    success, message = check_bigquery_connection(config)
    assert success, message


def test_real_databricks_connection():
    config = _required(
        "STYCH_TEST_DATABRICKS",
        ("SERVER_HOSTNAME", "HTTP_PATH", "ACCESS_TOKEN", "CATALOG"),
    )
    schema = os.getenv("STYCH_TEST_DATABRICKS_SCHEMA", "default")
    table = os.getenv("STYCH_TEST_DATABRICKS_TABLE")
    success, message = check_databricks_connection(config)
    assert success, message
    assert "writable" in message.lower() or "successful" in message.lower()

    from datasources.databricks import (
        normalize_scratch_config,
        run_databricks_query,
        scratch_location,
    )
    from datasources.warehouse import relation_ref

    config = normalize_scratch_config(config)
    catalog, scratch_schema = scratch_location(config)
    if not table:
        pytest.skip("Set STYCH_TEST_DATABRICKS_TABLE to run clone + pushdown")

    from agents.sql_agent.databricks_runner import DatabricksSQLRunner

    runner = DatabricksSQLRunner(config)
    schema_name, copy_table = runner.create_temp_table_from_source(
        session_id="itest",
        source_schema=schema,
        source_table=table,
        custom_query=None,
        table_name="stych_itest_clone",
    )
    try:
        assert schema_name == scratch_schema
        quoted = runner.quote_relation(schema_name, copy_table)
        names, rows = run_databricks_query(
            config, f"SELECT COUNT(*) AS c FROM {quoted}", timeout_seconds=120
        )
        assert names[0].lower() == "c"
        assert int(rows[0][0]) >= 0
        src = relation_ref(database=catalog, schema=schema, table=table, quote="`")
        _, src_rows = run_databricks_query(
            config, f"SELECT COUNT(*) AS c FROM {src}", timeout_seconds=120
        )
        assert int(rows[0][0]) == int(src_rows[0][0])
    finally:
        runner.drop_temp_table(copy_table)


def test_real_databricks_preview_load_still_bounded():
    config = _required(
        "STYCH_TEST_DATABRICKS",
        ("SERVER_HOSTNAME", "HTTP_PATH", "ACCESS_TOKEN", "CATALOG"),
    )
    schema = os.getenv("STYCH_TEST_DATABRICKS_SCHEMA", "default")
    table = os.getenv("STYCH_TEST_DATABRICKS_TABLE")
    if not table:
        pytest.skip("Set STYCH_TEST_DATABRICKS_TABLE to run preview load")
    from datasources.databricks import load_from_databricks

    frame, label = load_from_databricks(config, schema=schema, table=table, preview_rows=10)
    assert len(frame) <= 10
    assert schema in label or table in label
