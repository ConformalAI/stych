"""Source identity, join planning, join SQL generation, and strategy gating."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from agents.linking import strategy_support as ss
from agents.linking.source_join import (
    SourceJoinError,
    _relation_columns,
    build_join_sql,
    execute_source_join,
)
from datasources.join_planner import MATERIALIZED, SOURCE, plan_join
from datasources.source_identity import same_source, source_key

PG_URL = "postgresql://user:pw@db.example.com:5432/analytics"


def _pg(table="orders", **meta):
    frame = pd.DataFrame({"id": [1], "v": ["a"]})
    return SimpleNamespace(
        filename=table,
        dataframe=frame,
        metadata={
            "source": "postgres",
            "connection_url": PG_URL,
            "schema": "public",
            "table": table,
            **meta,
        },
    )


def _dbx(table="orders", **meta):
    from datasources.warehouse import encode_connection_config

    frame = pd.DataFrame({"id": [1]})
    config = {
        "server_hostname": "dbc-1.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc",
        "access_token": "tok",
        "catalog": "main",
    }
    return SimpleNamespace(
        filename=table,
        dataframe=frame,
        metadata={
            "source": "databricks",
            "connection_config": encode_connection_config(config),
            "schema": "sales",
            "table": table,
            **meta,
        },
    )


def _upload():
    frame = pd.DataFrame({"id": [1]})
    return SimpleNamespace(filename="f.csv", dataframe=frame, metadata={"source": "upload"})


def _mongo(collection="orders", **meta):
    frame = pd.DataFrame({"id": [1]})
    return SimpleNamespace(
        filename=collection,
        dataframe=frame,
        metadata={
            "source": "mongodb",
            "connection_url": "mongodb://user:pw@db.example.com:27017/analytics",
            "database": "analytics",
            "collection": collection,
            **meta,
        },
    )


# -- source identity --------------------------------------------------------


def test_two_tables_on_one_connection_share_identity():
    assert same_source(_pg("orders").metadata, _pg("customers").metadata)


def test_a_different_database_is_a_different_source():
    other = _pg("orders", connection_url=PG_URL.replace("/analytics", "/warehouse"))
    assert not same_source(_pg("orders").metadata, other.metadata)


def test_a_different_role_is_a_different_source():
    """The join runs as one role; a role that cannot see both sides is not safe."""
    other = _pg("orders", connection_url=PG_URL.replace("user:", "other:"))
    assert not same_source(_pg("orders").metadata, other.metadata)


def test_redacted_credentials_never_match():
    ctx = _pg("orders", connection_url="[REDACTED]")
    assert source_key(ctx.metadata) is None
    assert not same_source(ctx.metadata, ctx.metadata)


def test_uploads_have_no_source_identity():
    assert source_key(_upload().metadata) is None


def test_databricks_identity_ignores_catalog_but_not_workspace():
    assert same_source(_dbx("a").metadata, _dbx("b", schema="other").metadata)


def test_identity_never_leaks_credentials():
    key = source_key(_pg().metadata)
    assert key and "pw" not in key and "db.example.com" not in key


@pytest.mark.parametrize(
    ("source", "config", "expected_prefix"),
    [
        (
            "bigquery",
            {
                "project_id": "analytics",
                "service_account_json": '{"type":"service_account","client_email":"stych@example.iam.gserviceaccount.com","private_key":"key"}',
            },
            "bigquery:",
        ),
        (
            "snowflake",
            {
                "account": "org-account",
                "user": "stych",
                "warehouse": "compute",
                "database": "ANALYTICS",
                "private_key": "-----BEGIN PRIVATE KEY-----secret",
            },
            "snowflake:",
        ),
        (
            "redshift",
            {
                "host": "cluster.example.com",
                "database": "analytics",
                "user": "stych",
                "access_key_id": "AKIA123",
                "secret_access_key": "secret",
            },
            "redshift:",
        ),
    ],
)
def test_cloud_sql_connectors_have_fail_closed_identity(source, config, expected_prefix):
    from datasources.warehouse import encode_connection_config

    meta = {"source": source, "connection_config": encode_connection_config(config)}
    key = source_key(meta)
    assert key and key.startswith(expected_prefix)
    assert "secret" not in key and "PRIVATE" not in key
    assert same_source(meta, dict(meta))


def test_sqlserver_url_has_connector_identity_without_leaking_credentials():
    meta = {
        "source": "sqlserver",
        "connection_url": "mssql+pymssql://user:pw@db.example.com:1433/analytics",
    }
    key = source_key(meta)
    assert key and key.startswith("sqlserver:")
    assert "pw" not in key and "db.example.com" not in key


def test_mongodb_identity_and_planning_follow_lookup_database_boundary():
    left, right = _mongo("orders"), _mongo("customers")
    key = source_key(left.metadata)
    assert key and key.startswith("mongodb:") and "pw" not in key
    plan = plan_join([left, right])
    assert plan.source_side and plan.connector == "mongodb"
    assert plan.relations[0].schema == "analytics"
    assert plan.relations[0].table == "orders"
    assert not same_source(left.metadata, _mongo("x", database="archive").metadata)


def test_cloud_sql_identity_refuses_missing_or_redacted_principals():
    from datasources.warehouse import encode_connection_config

    cases = [
        ("bigquery", {"project_id": "p", "service_account_json": "[REDACTED]"}),
        (
            "snowflake",
            {
                "account": "a",
                "user": "u",
                "warehouse": "w",
                "database": "d",
                "private_key": "[REDACTED]",
            },
        ),
        (
            "redshift",
            {
                "host": "h",
                "database": "d",
                "user": "u",
                "access_key_id": "a",
                "secret_access_key": "[REDACTED]",
            },
        ),
        ("sqlserver", {"connection_url": "mssql+pymssql://[REDACTED]@h/d"}),
    ]
    for source, fields in cases:
        meta = {"source": source}
        if source == "sqlserver":
            meta.update(fields)
        else:
            meta["connection_config"] = encode_connection_config(fields)
        assert source_key(meta) is None


# -- planning ---------------------------------------------------------------


def test_same_connection_plans_a_source_side_join():
    plan = plan_join([_pg("orders"), _pg("customers")])
    assert plan.mode == SOURCE
    assert plan.source_side


def test_mixed_sources_fall_back_to_materialized():
    assert plan_join([_pg("orders"), _upload()]).mode == MATERIALIZED
    assert plan_join([_pg("orders"), _dbx("x")]).mode == MATERIALIZED


def test_a_single_dataset_is_never_a_source_join():
    assert plan_join([_pg("orders")]).mode == MATERIALIZED


def test_working_copy_is_preferred_over_the_base_table():
    ctx = _pg("orders", temp_schema="stych_temp", temp_table="stych_abc")
    relation = plan_join([ctx, _pg("customers")]).relations[0]
    assert (relation.schema, relation.table) == ("stych_temp", "stych_abc")


# -- SQL generation ---------------------------------------------------------


def _relations():
    return plan_join([_pg("orders"), _pg("customers")]).relations


def test_join_sql_chains_and_qualifies_every_column():
    cols = {0: ["id", "total"], 1: ["id", "name"]}
    steps = [{"left": 0, "right": 1, "left_on": "id", "right_on": "id", "join_type": "LEFT"}]
    sql, labels = build_join_sql(_relations(), steps, cols, "postgres")
    assert 'LEFT JOIN "public"."customers" AS "t1"' in sql
    assert '"t0"."id" = "t1"."id"' in sql
    # The right-hand "id" survives under a suffixed label instead of vanishing.
    assert labels == ["id", "total", "id_t1", "name"]


def test_join_sql_supports_multi_key_steps():
    cols = {0: ["a", "b"], 1: ["a", "b"]}
    steps = [
        {
            "left": 0,
            "right": 1,
            "join_type": "INNER",
            "keys": [{"left": "a", "right": "a"}, {"left": "b", "right": "b"}],
        }
    ]
    sql, _ = build_join_sql(_relations(), steps, cols, "postgres")
    assert '"t0"."a" = "t1"."a" AND "t0"."b" = "t1"."b"' in sql


def test_join_sql_resolves_agent_dataset_names_and_starts_from_logical_left():
    cols = {0: ["id"], 1: ["customer_id"]}
    steps = [
        {
            "left": "customers",
            "right": "orders",
            "left_on": "customer_id",
            "right_on": "id",
            "join_type": "LEFT",
        }
    ]
    sql, _ = build_join_sql(_relations(), steps, cols, "postgres")
    assert 'FROM "public"."customers" AS "t1"' in sql
    assert 'LEFT JOIN "public"."orders" AS "t0"' in sql
    assert '"t1"."customer_id" = "t0"."id"' in sql


def test_join_sql_uses_backticks_for_databricks():
    plan = plan_join([_dbx("orders"), _dbx("customers")])
    steps = [{"left": 0, "right": 1, "left_on": "id", "right_on": "id"}]
    sql, _ = build_join_sql(plan.relations, steps, {0: ["id"], 1: ["id"]}, "databricks")
    assert "`sales`.`customers` AS `t1`" in sql


def test_databricks_join_preserves_each_relation_catalog():
    left = _dbx("orders", database="main")
    right = _dbx("customers", database="archive")
    plan = plan_join([left, right])
    sql, _ = build_join_sql(
        plan.relations,
        [{"left": 0, "right": 1, "left_on": "id", "right_on": "id"}],
        {0: ["id"], 1: ["id"]},
        "databricks",
    )
    assert "`main`.`sales`.`orders`" in sql
    assert "`archive`.`sales`.`customers`" in sql


def test_databricks_cross_catalog_columns_do_not_query_the_wrong_catalog():
    relation = plan_join([_dbx("orders"), _dbx("customers", database="archive")]).relations[1]

    class Runner:
        scratch_catalog = "main"

        def get_table_columns(self, *_args):
            raise AssertionError("would introspect the runner's main catalog")

    assert _relation_columns(Runner(), relation, "databricks") == ["id"]


def test_unknown_join_type_is_rejected():
    steps = [{"left": 0, "right": 1, "left_on": "id", "right_on": "id", "join_type": "CROSS"}]
    with pytest.raises(SourceJoinError, match="Unsupported join type"):
        build_join_sql(_relations(), steps, {0: ["id"], 1: ["id"]}, "postgres")


def test_disconnected_datasets_are_rejected():
    """Every dataset must be reachable, or the result would be a silent cross join."""
    relations = plan_join([_pg("a"), _pg("b"), _pg("c")]).relations
    steps = [{"left": 0, "right": 1, "left_on": "id", "right_on": "id"}]
    with pytest.raises(SourceJoinError, match="No join step connects"):
        build_join_sql(relations, steps, {0: ["id"], 1: ["id"], 2: ["id"]}, "postgres")


def test_injection_in_a_column_name_stays_quoted():
    steps = [{"left": 0, "right": 1, "left_on": 'id" OR 1=1 --', "right_on": "id"}]
    sql, _ = build_join_sql(_relations(), steps, {0: ["id"], 1: ["id"]}, "postgres")
    assert '"id"" OR 1=1 --"' in sql
    assert 'OR 1=1 --".' not in sql.replace('"id"" OR 1=1 --"', "")


class _SqlServerJoinRunner:
    temp_schema = "stych_temp"
    atomic_writes = True

    def __init__(self):
        self.statements = []
        self.reads = []

    def quote_relation(self, schema, table):
        return f"[{schema}].[{table}]"

    def get_table_columns(self, schema, table):
        return [{"column_name": "id"}]

    def ensure_temp_schema(self):
        return None

    def execute_statements_transaction(self, statements, *, allowed_schema=None):
        self.statements.extend(statements)

    def get_row_count(self, schema, table):
        return 1

    def run_sql_read_only(self, sql, params=None):
        self.reads.append(sql)
        return pd.DataFrame({"id": [1], "id_t1": [1]})


def test_execute_source_join_uses_sqlserver_statement_shapes(monkeypatch):
    url = "mssql+pymssql://user:pw@db.example.com:1433/analytics"

    def context(name):
        frame = pd.DataFrame({"id": [1]})
        return SimpleNamespace(
            filename=name,
            dataframe=frame,
            metadata={
                "source": "sqlserver",
                "connection_url": url,
                "schema": "dbo",
                "table": name,
            },
        )

    runner = _SqlServerJoinRunner()
    monkeypatch.setattr("agents.linking.source_join.runner_for", lambda _meta: runner)
    plan = plan_join([context("orders"), context("customers")])
    result = execute_source_join(
        plan,
        [
            {
                "left": "orders",
                "right": "customers",
                "left_on": "id",
                "right_on": "id",
                "join_type": "INNER",
            }
        ],
        session_id="session",
        state=SimpleNamespace(),
    )
    assert runner.statements[0].startswith("SELECT * INTO [stych_temp].[")
    assert runner.reads[0].startswith("SELECT TOP 500 * FROM [stych_temp].[")
    assert result.rows == 1


# -- strategy gating --------------------------------------------------------


def test_exact_joins_push_into_the_source():
    plan = plan_join([_pg("a"), _pg("b")])
    assert ss.resolve_strategy("exact", plan).mode == ss.SOURCE_SIDE


def test_semantic_is_refused_for_connected_sources():
    decision = ss.resolve_strategy("semantic", plan_join([_pg("a"), _pg("b")]))
    assert decision.mode == ss.UNAVAILABLE
    assert "outside your database" in (decision.user_message or "")


def test_lexical_strategies_use_full_materialization_not_a_sample():
    decision = ss.resolve_strategy("levenshtein", plan_join([_pg("a"), _pg("b")]))
    assert decision.mode == ss.MATERIALIZED
    assert decision.reason == "full_source_stream"
    assert "complete source inputs" in (decision.user_message or "")


def test_local_datasets_keep_current_parity_strategies():
    plan = plan_join([_upload(), _upload()])
    for name in ["exact", *ss.LEXICAL_STRATEGIES]:
        assert ss.resolve_strategy(name, plan).mode == ss.MATERIALIZED
    assert ss.resolve_strategy("semantic", plan).mode == ss.UNAVAILABLE


def test_unknown_strategy_is_reported():
    assert ss.resolve_strategy("telepathy", plan_join([_pg("a"), _pg("b")])).mode == ss.UNAVAILABLE
