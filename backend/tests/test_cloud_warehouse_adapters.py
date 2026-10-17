"""Snowflake/Redshift contract, extraction, and preview adapter tests."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pytest

from datasources import redshift as redshift_source
from datasources import snowflake as snowflake_source
from datasources.adapters import resolve_adapter, supports_pushdown
from datasources.adapters import warehouse as warehouse_adapter
from datasources.adapters.base import AdapterTimeout
from datasources.warehouse import (
    WarehouseError,
    decode_connection_config,
    encode_connection_config,
    quote_ident,
    validate_read_only_query,
)


def _ctx(source: str, connection: dict, **metadata):
    return SimpleNamespace(
        metadata={
            "source": source,
            "connection_config": encode_connection_config(connection),
            **metadata,
        },
        working_dataframe=pd.DataFrame({"id": [1], "name": ["a"]}),
    )


def test_warehouse_identifier_and_read_only_query_validation():
    assert quote_ident('odd"name') == '"odd""name"'
    assert validate_read_only_query("WITH x AS (SELECT 1) SELECT * FROM x;").startswith("WITH")
    assert validate_read_only_query("-- a harmless comment\nSELECT 1").endswith("SELECT 1")
    for query in (
        "DELETE FROM events",
        "SELECT 1; DROP TABLE events",
        "SELECT * INTO new_events FROM events",
        "WITH x AS (SELECT * FROM events) SELECT * INTO new_events FROM x",
        "WITH x AS (SELECT 1) UNLOAD ('SELECT * FROM x') TO 's3://bucket/path'",
        "WITH x AS (DELETE FROM events RETURNING *) SELECT * FROM x",
    ):
        with pytest.raises(WarehouseError):
            validate_read_only_query(query)


def test_connection_config_round_trip_and_missing_credentials_fail_closed():
    config = {
        "account": "org-account",
        "user": "u",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
    }
    encoded = encode_connection_config(config)
    assert decode_connection_config(encoded) == config
    assert supports_pushdown({"source": "snowflake", "connection_config": encoded}) is True
    assert supports_pushdown({"source": "snowflake"}) is False


@pytest.mark.parametrize(
    ("placeholder", "expected"), [("?", "LIMIT ? OFFSET ?"), ("%s", "LIMIT %s OFFSET %s")]
)
def test_shared_warehouse_query_builder_binds_values(placeholder, expected):
    statement, params = warehouse_adapter.build_select_sql(
        '"analytics"."orders"',
        ["id", 'odd"name'],
        placeholder=placeholder,
        offset=20,
        limit=10,
        sort_by='odd"name',
        sort_dir="desc",
        filters={"id": "50%"},
        search="abc",
    )
    assert '"odd""name" DESC NULLS LAST' in statement
    assert expected in statement
    assert params[-2:] == (10, 20)
    assert params[0] == "%50\\%%"
    assert "abc" not in statement


def test_resolve_snowflake_and_redshift_adapters_from_encrypted_metadata():
    snowflake = resolve_adapter(
        _ctx(
            "snowflake",
            {
                "account": "org-account",
                "user": "u",
                "private_key": "-----BEGIN PRIVATE KEY-----\nsnowflake-secret\n-----END PRIVATE KEY-----",
                "warehouse": "COMPUTE_WH",
                "database": "ANALYTICS",
            },
            database="ANALYTICS",
            schema="PUBLIC",
            table="ORDERS",
        )
    )
    assert snowflake.dialect == "snowflake"
    assert snowflake.source_ref == '"ANALYTICS"."PUBLIC"."ORDERS"'
    assert "snowflake-secret" not in repr(snowflake.cache_info.key)
    assert "BEGIN PRIVATE KEY" not in repr(snowflake.cache_info.key)

    redshift = resolve_adapter(
        _ctx(
            "redshift",
            {
                "host": "cluster.example",
                "database": "dev",
                "user": "u",
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "redshift-secret",
                "cluster_identifier": "analytics",
            },
            schema="public",
            table="orders",
        )
    )
    assert redshift.dialect == "redshift"
    assert redshift.source_ref == '"public"."orders"'
    assert "redshift-secret" not in repr(redshift.cache_info.key)

    bigquery = resolve_adapter(
        _ctx(
            "bigquery",
            {
                "service_account_json": '{"type":"service_account","private_key":"bq-secret"}',
                "project_id": "demo-proj",
            },
            database="demo-proj",
            schema="analytics",
            table="events",
        )
    )
    assert bigquery.dialect == "bigquery"
    assert bigquery.source_ref == "`demo-proj`.`analytics`.`events`"
    assert "bq-secret" not in repr(bigquery.cache_info.key)

    databricks = resolve_adapter(
        _ctx(
            "databricks",
            {
                "server_hostname": "adb.example",
                "http_path": "/sql/1.0/warehouses/abc",
                "access_token": "dapi-secret",
                "catalog": "main",
            },
            database="main",
            schema="gold",
            table="orders",
        )
    )
    assert databricks.dialect == "databricks"
    assert databricks.source_ref == "`main`.`gold`.`orders`"
    assert "dapi-secret" not in repr(databricks.cache_info.key)


def test_redshift_adapter_renders_named_filter_binds_for_driver(monkeypatch):
    adapter = resolve_adapter(
        _ctx(
            "redshift",
            {
                "host": "cluster.example",
                "database": "dev",
                "user": "u",
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "redshift-secret",
                "cluster_identifier": "analytics",
            },
            schema="public",
            table="orders",
        )
    )
    captured = {}

    def fake_run(statement, parameters=()):
        captured["statement"] = statement
        captured["parameters"] = parameters
        return [], []

    monkeypatch.setattr(adapter, "_run", fake_run)
    assert adapter.escape_clause == r" ESCAPE '\\'"
    adapter.run('SELECT * FROM "public"."orders" WHERE "item" = :fp0', {"fp0": "Salad"})

    assert captured == {
        "statement": 'SELECT * FROM "public"."orders" WHERE "item" = %s',
        "parameters": ("Salad",),
    }


class _SnowflakeCursor:
    description = [("ID",), ("NAME",)]

    def __init__(self):
        self.statement = None

    def execute(self, statement, parameters=(), timeout=None):
        self.statement = statement
        return self

    def fetch_arrow_batches(self):
        yield pa.table({"ID": [1, 2], "NAME": ["a", "b"]})

    def fetchall(self):
        return []

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_snowflake_load_uses_arrow_batches(monkeypatch):
    cursor = _SnowflakeCursor()
    monkeypatch.setattr(snowflake_source, "_connect", lambda config: _Connection(cursor))
    frame, label = snowflake_source.load_from_snowflake(
        {"account": "a"}, database="DB", schema="PUBLIC", table="ORDERS", preview_rows=50
    )
    assert label == "DB.PUBLIC.ORDERS"
    assert frame.to_dict(orient="list") == {"ID": [1, 2], "NAME": ["a", "b"]}
    assert '"DB"."PUBLIC"."ORDERS"' in cursor.statement
    assert "LIMIT 50" in cursor.statement


class _RedshiftCursor:
    description = [("id",), ("name",)]

    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        return self

    def fetchall(self):
        return [(1, "a"), (2, "b")]

    def close(self):
        pass


def test_redshift_load_builds_bounded_read(monkeypatch):
    cursor = _RedshiftCursor()
    monkeypatch.setattr(redshift_source, "_connect", lambda config: _Connection(cursor))
    frame, label = redshift_source.load_from_redshift(
        {"host": "h"}, schema="public", table="orders", preview_rows=25
    )
    assert label == "public.orders"
    assert frame.to_dict(orient="list") == {"id": [1, 2], "name": ["a", "b"]}
    assert cursor.statements[-1][0] == 'SELECT * FROM "public"."orders" LIMIT 25'


def test_redshift_discovery_uses_show_tables_for_external_and_shared_catalogs(monkeypatch):
    captured = {}

    def run(config, statement, parameters=(), **kwargs):
        captured["statement"] = statement
        return ["database_name", "schema_name", "table_name", "table_type"], [
            ("dev", "analytics", "events", "TABLE")
        ]

    monkeypatch.setattr(redshift_source, "run_redshift_query", run)
    tables = redshift_source.list_redshift_tables({"database": "dev"}, schema="analytics", limit=50)
    assert tables == [{"schema": "analytics", "table": "events"}]
    assert captured["statement"] == 'SHOW TABLES FROM SCHEMA "dev"."analytics" LIMIT 50'


def test_warehouse_adapter_maps_provider_timeout(monkeypatch):
    adapter = resolve_adapter(
        _ctx(
            "snowflake",
            {
                "account": "org-account",
                "user": "u",
                "private_key": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
                "warehouse": "COMPUTE_WH",
                "database": "ANALYTICS",
            },
            database="ANALYTICS",
            schema="PUBLIC",
            table="ORDERS",
        )
    )
    monkeypatch.setattr(
        "datasources.adapters.snowflake.run_snowflake_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            snowflake_source.SnowflakeLoadError("query timeout")
        ),
    )
    with pytest.raises(AdapterTimeout):
        adapter.schema()


def test_bigquery_like_fold_and_backtick_quoting():
    statement, params = warehouse_adapter.build_select_sql(
        "`demo`.`analytics`.`events`",
        ["id", "name"],
        placeholder="?",
        offset=0,
        limit=10,
        quote="`",
        string_type="STRING",
        fold_like=True,
        filters={"name": "Acme"},
    )
    assert "`name` DESC" not in statement
    assert "LOWER(CAST(`name` AS STRING)) LIKE LOWER(?)" in statement
    assert params[0] == "%Acme%"
    assert quote_ident("odd`name", "`") == r"`odd\`name`"


def test_cloud_warehouse_filters_omit_unsupported_escape_clause():
    for like_operator, fold_like in (("LIKE", True), ("ILIKE", False)):
        statement, _ = warehouse_adapter.build_select_sql(
            "`demo`.`analytics`.`events`",
            ["name"],
            placeholder="?",
            offset=0,
            limit=1,
            quote="`",
            string_type="STRING",
            like_operator=like_operator,
            fold_like=fold_like,
            escape_clause="",
            filters={"name": "Acme"},
        )
        assert "ESCAPE" not in statement


def test_bigquery_load_quotes_project_dataset_table(monkeypatch):
    from datasources import bigquery as bigquery_source

    captured = {}

    def run(config, statement, parameters=(), **kwargs):
        captured["statement"] = statement
        return ["id", "name"], [(1, "a")]

    monkeypatch.setattr(bigquery_source, "run_bigquery_query", run)
    frame, label = bigquery_source.load_from_bigquery(
        {
            "service_account_json": (
                '{"type":"service_account","project_id":"demo-proj","private_key":"k",'
                '"client_email":"stych@demo-proj.iam.gserviceaccount.com"}'
            )
        },
        schema="analytics",
        table="events",
        preview_rows=25,
    )
    assert label == "demo-proj.analytics.events"
    assert frame.to_dict(orient="list") == {"id": [1], "name": ["a"]}
    assert "`demo-proj`.`analytics`.`events`" in captured["statement"]
    assert "LIMIT 25" in captured["statement"]


def test_databricks_load_quotes_catalog_schema_table(monkeypatch):
    from datasources import databricks as databricks_source

    captured = {}

    def run(config, statement, parameters=(), **kwargs):
        captured["statement"] = statement
        return ["id"], [(1,)]

    monkeypatch.setattr(databricks_source, "run_databricks_query", run)
    frame, label = databricks_source.load_from_databricks(
        {"catalog": "main"},
        schema="gold",
        table="orders",
        preview_rows=10,
    )
    assert label == "main.gold.orders"
    assert frame.to_dict(orient="list") == {"id": [1]}
    assert "`main`.`gold`.`orders`" in captured["statement"]


def test_bigquery_discovery_quotes_dataset_information_schema(monkeypatch):
    from datasources import bigquery as bigquery_source

    captured = {}

    def run(config, statement, parameters=(), **kwargs):
        captured["statement"] = statement
        captured["parameters"] = parameters
        return ["table_schema", "table_name"], [("analytics", "events")]

    monkeypatch.setattr(bigquery_source, "run_bigquery_query", run)
    tables = bigquery_source.list_bigquery_tables(
        {
            "service_account_json": (
                '{"type":"service_account","project_id":"demo-proj","private_key":"k",'
                '"client_email":"stych@demo-proj.iam.gserviceaccount.com"}'
            )
        },
        schema="analytics",
        limit=50,
    )
    assert tables == [{"schema": "analytics", "table": "events"}]
    assert captured["parameters"] == ()
    assert (
        captured["statement"]
        == "SELECT table_schema, table_name FROM `demo-proj`.`analytics`.INFORMATION_SCHEMA.TABLES "
        "WHERE table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name LIMIT 50"
    )


def test_databricks_discovery_filters_schema_with_bound_param(monkeypatch):
    from datasources import databricks as databricks_source

    captured = {}

    def run(config, statement, parameters=(), **kwargs):
        captured["statement"] = statement
        captured["parameters"] = parameters
        return ["table_schema", "table_name"], [("gold", "orders")]

    monkeypatch.setattr(databricks_source, "run_databricks_query", run)
    tables = databricks_source.list_databricks_tables({"catalog": "main"}, schema="gold", limit=25)
    assert tables == [{"schema": "gold", "table": "orders"}]
    assert captured["parameters"] == ("gold",)
    assert "`main`.information_schema.tables" in captured["statement"]
    assert "LIMIT 25" in captured["statement"]


def test_bigquery_adapter_maps_timeout(monkeypatch):
    from datasources import bigquery as bigquery_source

    adapter = resolve_adapter(
        _ctx(
            "bigquery",
            {
                "service_account_json": '{"type":"service_account","private_key":"k"}',
                "project_id": "demo-proj",
            },
            database="demo-proj",
            schema="analytics",
            table="events",
        )
    )
    monkeypatch.setattr(
        "datasources.adapters.bigquery.run_bigquery_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            bigquery_source.BigQueryLoadError("job exceeded timeout")
        ),
    )
    with pytest.raises(AdapterTimeout):
        adapter.schema()


def test_databricks_adapter_maps_timeout(monkeypatch):
    from datasources import databricks as databricks_source

    adapter = resolve_adapter(
        _ctx(
            "databricks",
            {
                "server_hostname": "adb.example",
                "http_path": "/sql/1.0/warehouses/abc",
                "access_token": "dapi-secret",
                "catalog": "main",
            },
            database="main",
            schema="gold",
            table="orders",
        )
    )
    monkeypatch.setattr(
        "datasources.adapters.databricks.run_databricks_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            databricks_source.DatabricksLoadError("timed out")
        ),
    )
    with pytest.raises(AdapterTimeout):
        adapter.schema()


def test_preview_page_reuses_one_warehouse_session(monkeypatch):
    from api.preview_service import build_adapter_page
    from datasources import databricks as databricks_source
    from datasources.adapters.base import clear_caches

    clear_caches()
    connects = {"n": 0}

    class FakeCursor:
        def __init__(self):
            self.description = [("column_name",), ("data_type",)]
            self._rows = [("id", "bigint")]

        def execute(self, statement, parameters=None):
            del parameters
            lowered = statement.lower()
            if "count(" in lowered:
                self.description = [("c",)]
                self._rows = [(10,)]
            elif "limit" in lowered and "information_schema" not in lowered:
                self.description = [("id",)]
                self._rows = [(1,)]
            else:
                self.description = [("column_name",), ("data_type",)]
                self._rows = [("id", "bigint")]
            return self

        def fetchall(self):
            return self._rows

        def close(self):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    def _connect(config, *, socket_timeout=60):
        del config, socket_timeout
        connects["n"] += 1
        return FakeConn()

    monkeypatch.setattr(databricks_source, "_connect", _connect)
    adapter = resolve_adapter(
        _ctx(
            "databricks",
            {
                "server_hostname": "adb.example",
                "http_path": "/sql/1.0/warehouses/abc",
                "access_token": "dapi-secret",
                "catalog": "main",
            },
            database="main",
            schema="gold",
            table="orders",
        )
    )
    page = build_adapter_page(adapter, session_id="s1", page=1, page_size=100)
    assert connects["n"] == 1
    assert page["total_rows"] == 10
    assert page["rows"] == [[1]]
