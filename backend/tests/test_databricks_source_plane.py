"""Databricks SOURCE-plane working copy, SQL dialect, and adapter tests."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from agents.sql_agent.databricks_runner import DatabricksSQLRunner
from agents.sql_agent.runner import SQLRunnerError
from datasources.adapters import resolve_adapter
from datasources.adapters.warehouse import named_to_qmark
from datasources.warehouse import encode_connection_config
from viz.catalog import build_catalog_sql
from viz.compile_sql import compile_spec_sql
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec


def _config(**overrides):
    cfg = {
        "server_hostname": "adb.example.net",
        "http_path": "/sql/1.0/warehouses/abc",
        "access_token": "dapi-secret",
        "catalog": "main",
        "scratch_catalog": "main",
        "scratch_schema": "stych_temp",
    }
    cfg.update(overrides)
    return cfg


def test_named_to_qmark_skips_postgres_casts():
    sql, values = named_to_qmark("SELECT CAST(x AS STRING), :s, :t", {"s": "gold", "t": "orders"})
    assert sql == "SELECT CAST(x AS STRING), ?, ?"
    assert values == ("gold", "orders")


def test_databricks_runner_quotes_three_level_names_and_confines_dml(monkeypatch):
    executed: list[str] = []

    def execute(config, statement, parameters=(), *, timeout_seconds=600, connection=None):
        del config, parameters, timeout_seconds, connection
        executed.append(statement)

    monkeypatch.setattr("agents.sql_agent.databricks_runner.execute_databricks_statement", execute)
    monkeypatch.setattr(
        "datasources.databricks._connect", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    runner = DatabricksSQLRunner(_config())
    assert runner.quote_relation("stych_temp", "stych_abc") == "`main`.`stych_temp`.`stych_abc`"

    runner.create_temp_table_from_source(
        session_id="sess-1",
        source_schema="gold",
        source_table="orders",
        custom_query=None,
        table_name="stych_sess_1_abcd1234",
    )
    # Materialization is a stamping CTAS now; DEEP CLONE moved to branching.
    assert any("CREATE TABLE" in sql and "_stych_row_id" in sql for sql in executed)
    assert any("`main`.`gold`.`orders`" in sql for sql in executed)
    assert any("`main`.`stych_temp`.`stych_sess_1_abcd1234`" in sql for sql in executed)

    with pytest.raises(SQLRunnerError, match="stych_temp"):
        runner.execute_statements_transaction(
            ["UPDATE `main`.`gold`.`orders` SET x = 1"],
            allowed_schema="stych_temp",
        )
    runner.execute_statements_transaction(
        ["UPDATE `main`.`stych_temp`.`stych_sess_1_abcd1234` SET x = 1"],
        allowed_schema="stych_temp",
    )


def test_databricks_materialization_stamps_a_row_id_instead_of_deep_cloning(monkeypatch):
    """DEEP CLONE reproduces the source schema exactly, so it cannot carry the
    row-id stamp that lets two copies be aligned. Materialization pays for one
    CTAS; branching still deep-clones and stays cheap."""
    executed: list[str] = []

    def execute(config, statement, parameters=(), *, timeout_seconds=600, connection=None):
        del config, parameters, timeout_seconds, connection
        executed.append(statement)

    monkeypatch.setattr("agents.sql_agent.databricks_runner.execute_databricks_statement", execute)
    monkeypatch.setattr(
        "datasources.databricks._connect", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    runner = DatabricksSQLRunner(_config())
    schema, table = runner.create_temp_table_from_source(
        session_id="sess-1",
        source_schema="gold",
        source_table="orders_view",
        custom_query=None,
        table_name="stych_sess_1_view",
    )
    assert schema == "stych_temp"
    assert table == "stych_sess_1_view"
    ctas = [sql for sql in executed if "CREATE TABLE" in sql and "SELECT" in sql]
    assert ctas, executed
    assert "_stych_row_id" in ctas[0]
    assert "`main`.`gold`.`orders_view`" in ctas[0]
    assert not any("DEEP CLONE" in sql for sql in executed)

    executed.clear()
    runner.duplicate_temp_table("stych_sess_1_view", "stych_sess_1_branch")
    assert any("DEEP CLONE" in sql for sql in executed), executed


def test_compile_spec_sql_databricks_uses_backticks_and_casts():
    catalog = build_catalog_sql(
        "stych_temp",
        "working",
        [
            {"column_name": "item", "data_type": "string"},
            {"column_name": "qty", "data_type": "string"},
        ],
        row_count=5,
        preview_df=pd.DataFrame({"item": ["a"], "qty": ["1"]}),
    )
    spec = normalize_spec(
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "item", "type": "nominal"},
                    "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                },
            }
        ),
        catalog,
    )
    sql = compile_spec_sql(
        build_plan(spec),
        catalog,
        "stych_temp",
        "working",
        dialect="databricks",
        database="main",
    )
    assert "`main`.`stych_temp`.`working`" in sql
    assert '"stych_temp"' not in sql
    assert "CAST(" in sql or "RLIKE" in sql
    assert "to_char" not in sql


def test_resolve_adapter_databricks_working_copy_points_at_scratch():
    ctx = SimpleNamespace(
        metadata={
            "source": "databricks",
            "connection_config": encode_connection_config(_config()),
            "scratch_catalog": "main",
            "scratch_schema": "stych_temp",
            "temp_schema": "stych_temp",
            "temp_table": "stych_sess_working",
            "schema": "gold",
            "table": "orders",
            "database": "main",
        },
        working_dataframe=pd.DataFrame({"id": [1]}),
    )
    base = resolve_adapter(ctx, "base")
    working = resolve_adapter(ctx, "temp")
    assert base.source_ref == "`main`.`gold`.`orders`"
    assert working.source_ref == "`main`.`stych_temp`.`stych_sess_working`"
    assert working.dialect == "databricks"


def test_filter_route_allows_databricks_named_working_copies(monkeypatch):
    from filters.routes import apply_spec
    from filters.spec import parse_spec

    ctx = SimpleNamespace(
        metadata={"source": "databricks", "temp_schema": "stych_temp", "temp_table": "t1"},
        file_id="dbx1",
        filename="Databricks: orders",
        dataframe=pd.DataFrame({"id": [1]}),
        working_dataframe=pd.DataFrame({"id": [1]}),
    )

    class _Session:
        data_context = ctx

        def get_all_data_contexts(self):
            return [ctx]

    captured: dict = {}

    def fake_resolve(context, variant):
        del context
        captured["variant"] = variant
        return SimpleNamespace(
            dialect="databricks",
            ident_quote="`",
            source_ref="`main`.`stych_temp`.`t1`",
            is_table=True,
            schema=lambda: [],
        )

    monkeypatch.setattr("filters.routes._resolve_sql_adapter", fake_resolve)
    monkeypatch.setattr(
        "filters.routes.preview_service.resolve_sql_source_id",
        lambda session, source_id: (ctx, "copy:working"),
    )
    monkeypatch.setattr(
        "filters.sql_service.build_sql_filter_page",
        lambda *args, **kwargs: {"kind": "filter", "engine": "sql"},
    )

    page = apply_spec(
        _Session(),
        "s1",
        "user-1",
        parse_spec({"name": "t"}),
        source="sql",
        source_id="dbx1::copy:working",
    )
    assert captured["variant"] == "copy:working"
    assert page["engine"] == "sql"


async def test_field_catalog_does_not_clone_a_source_table(monkeypatch):
    from datasources.adapters.base import AdapterColumn, CountResult
    from execution.session_plane import DataScope, PlaneKind
    from viz.catalog import get_field_catalog

    cloned: list[str] = []

    class FakeAdapter:
        db_schema = "default"
        table = "cafe_sales_data"

        def bound_session(self):
            from contextlib import nullcontext

            return nullcontext()

        def schema(self):
            return [AdapterColumn(name="id", db_type="bigint", kind="number")]

        def count(self, filters=None, search=None):
            del filters, search
            return CountResult(10)

    class Ctx:
        file_id = "dbx1"
        filename = "Databricks"
        working_dataframe = pd.DataFrame({"id": [1]})
        metadata = {
            "source": "databricks",
            "schema": "default",
            "table": "cafe_sales_data",
            "connection_config": encode_connection_config(_config()),
        }

        def get_sql_target(self):
            return ("default", "cafe_sales_data")

    class State:
        session_id = "s1"
        data_context = Ctx()

    class Agent:
        async def ensure_temp_table(self, state):
            del state
            cloned.append("clone")
            return ("stych_temp", "x")

        async def get_table_columns(self, state):
            del state
            cloned.append("cols")
            return []

        async def get_row_count(self, state):
            del state
            cloned.append("count")
            return 0

    monkeypatch.setattr(
        "execution.session_plane.resolve_data_plane",
        lambda *args, **kwargs: SimpleNamespace(
            kind=PlaneKind.SOURCE, connector_id="databricks", scope=DataScope.FULL
        ),
    )
    monkeypatch.setattr("datasources.adapters.resolve_adapter", lambda ctx, variant: FakeAdapter())

    catalog = await get_field_catalog(State(), sql_agent=Agent())
    assert cloned == []
    assert catalog.get("id") is not None
    assert catalog.row_count == 10


@pytest.mark.parametrize(
    ("dialect", "database", "expected_ref"),
    [
        ("bigquery", "proj", "`proj`.`stych_temp`.`working`"),
        ("snowflake", "ANALYTICS", '"ANALYTICS"."stych_temp"."working"'),
        ("redshift", None, '"stych_temp"."working"'),
    ],
)
def test_compile_spec_sql_qualifies_for_every_source_side_engine(dialect, database, expected_ref):
    """Compiling as Postgres emitted "stych_temp"."working", which BigQuery reads
    as a string literal — the 'Unexpected string literal' visualization failure."""
    catalog = build_catalog_sql(
        "stych_temp",
        "working",
        [
            {"column_name": "item", "data_type": "string"},
            {"column_name": "qty", "data_type": "string"},
        ],
        row_count=5,
        preview_df=pd.DataFrame({"item": ["a"], "qty": ["1"]}),
    )
    spec = normalize_spec(
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "item", "type": "nominal"},
                    "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                },
            }
        ),
        catalog,
    )
    kwargs = {"dialect": dialect}
    if database:
        kwargs["database"] = database
    sql = compile_spec_sql(build_plan(spec), catalog, "stych_temp", "working", **kwargs)
    assert expected_ref in sql
    if dialect == "bigquery":
        # Postgres-isms that BigQuery rejects outright.
        assert "to_char" not in sql
        assert "ILIKE" not in sql
        assert "DOUBLE PRECISION" not in sql


def test_viz_executor_compiles_for_the_connector_not_just_databricks():
    """The executor used to set the dialect only for Databricks, so every other
    source-side connector silently compiled as Postgres."""
    import inspect

    from viz import executor

    source = inspect.getsource(executor)
    assert 'compile_kw: dict = {"dialect": plane.connector_id}' in source
