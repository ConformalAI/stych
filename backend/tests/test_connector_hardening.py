"""Regressions for the connector review findings.

Each test here corresponds to a defect that shipped: a working-copy adapter that
resolved to the wrong engine, an internal column leaking into user-facing output,
and SQL syntax only some engines accept.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from agents.sql_agent import working_copies as wc
from datasources.sql_dialect import get_dialect
from datasources.warehouse import encode_connection_config

CONFIGS = {
    "snowflake": {
        "account": "a",
        "user": "u",
        "private_key": "k",
        "warehouse": "w",
        "database": "ANALYTICS",
    },
    "bigquery": {"service_account_json": "{}", "project_id": "proj"},
    "redshift": {
        "host": "h",
        "database": "d",
        "user": "u",
        "access_key_id": "k",
        "secret_access_key": "s",
    },
}


def _ctx(source: str):
    return SimpleNamespace(
        metadata={
            "source": source,
            "connection_config": encode_connection_config(CONFIGS[source]),
            "temp_schema": "stych_temp",
            "temp_table": "stych_x",
        },
        working_dataframe=pd.DataFrame({"id": [1]}),
    )


# -- working-copy adapters resolve to their own engine ----------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("snowflake", "SnowflakeTableAdapter"),
        ("bigquery", "BigQueryTableAdapter"),
        ("redshift", "RedshiftTableAdapter"),
    ],
)
def test_warehouse_working_copies_get_their_own_adapter(source, expected):
    """These used to fall through to a Postgres adapter built with a None URL,
    breaking every working-copy feature with an unhandled 500."""
    from datasources import adapters

    adapter = adapters.resolve_adapter(_ctx(source), "temp")
    assert type(adapter).__name__ == expected
    assert adapter.dialect == source


def test_working_copy_relations_are_qualified_for_the_engine():
    from datasources import adapters

    assert adapters.resolve_adapter(_ctx("bigquery"), "temp").source_ref.startswith("`proj`")
    snowflake = adapters.resolve_adapter(_ctx("snowflake"), "temp")
    assert snowflake.source_ref.startswith('"ANALYTICS"."STYCH_TEMP"')


def test_an_unsupported_source_is_refused_with_an_accurate_message():
    from datasources import adapters

    # Credentials are validated first, so supply them and let the support gate
    # be the thing that refuses.
    # Every shipped connector is source-side now, so this uses an unknown id.
    ctx = SimpleNamespace(
        metadata={"source": "upload", "connection_url": "postgresql://h/db"},
        working_dataframe=pd.DataFrame(),
    )
    with pytest.raises(adapters.AdapterError, match="upload"):
        adapters.resolve_adapter(ctx, "temp")


# -- the stamped row id stays internal --------------------------------------


def test_visible_columns_drops_the_internal_stamp():
    assert wc.visible_columns([wc.ROW_ID_COLUMN, "id", "name"]) == ["id", "name"]
    assert wc.is_internal_column(wc.ROW_ID_COLUMN) is True
    assert wc.is_internal_column("customer_id") is False


def test_preview_page_hides_the_stamp_from_columns_and_rows():
    """It is our alignment key, not the customer's data — it must not reach the
    grid, and dropping the column must drop its value from every row too."""
    from api import preview_service
    from datasources.adapters.base import AdapterColumn, CountResult, SourceCacheInfo

    class _Adapter:
        dialect = "snowflake"
        supports_stats = False
        cache_info = SourceCacheInfo(key=("t",))

        def schema(self):
            return [
                AdapterColumn(name=wc.ROW_ID_COLUMN, db_type="number", kind="number"),
                AdapterColumn(name="id", db_type="int", kind="number"),
            ]

        def count(self, *_a, **_k):
            return CountResult(1)

        def fetch_page(self, **_k):
            return [[99, 7]]

    page = preview_service.build_adapter_page(_Adapter(), session_id="s")
    assert [c["name"] for c in page["columns"]] == ["id"]
    assert page["rows"] == [[7]]


def test_export_requests_only_visible_columns():
    """A downloaded CSV must not contain Stych's internal column."""
    import inspect

    from api import export_service

    source = inspect.getsource(export_service.iter_source_batches)
    assert "visible_columns" in source
    assert "columns=columns" in source


# -- dialect-specific SQL the generators emit -------------------------------


@pytest.mark.parametrize(
    ("dialect", "fragment"),
    [
        ("postgres", "FILTER (WHERE"),
        ("databricks", "FILTER (WHERE"),
        ("snowflake", "SUM(CASE WHEN"),
        ("redshift", "SUM(CASE WHEN"),
        ("bigquery", "COUNTIF("),
    ],
)
def test_count_if_uses_a_form_the_engine_accepts(dialect, fragment):
    """COUNT(*) FILTER (WHERE ...) is a hard syntax error on three of five."""
    assert fragment in get_dialect(dialect).count_if("x > 1")


def test_bigquery_like_has_no_ilike_or_escape_clause():
    d = get_dialect("bigquery")
    assert "ILIKE" not in d.ilike("c", ":p")
    assert "ESCAPE" not in d.like("c", ":p")


def test_other_engines_keep_ilike():
    assert "ILIKE" in get_dialect("postgres").ilike("c", ":p")
    assert "ILIKE" in get_dialect("snowflake").ilike("c", ":p")


def test_summary_generators_no_longer_hardcode_the_filter_clause():
    import inspect

    from api import sql_diff_service
    from filters import sql_service

    for module in (sql_service, sql_diff_service):
        assert "COUNT(*) FILTER (WHERE" not in inspect.getsource(module), module.__name__


# -- runner contract --------------------------------------------------------


def test_every_runner_declares_whether_its_writes_are_atomic():
    """Callers that need two statements to land together must be able to ask."""
    from agents.sql_agent.bigquery_runner import BigQuerySQLRunner
    from agents.sql_agent.databricks_runner import DatabricksSQLRunner
    from agents.sql_agent.redshift_runner import RedshiftSQLRunner
    from agents.sql_agent.runner import PostgresSQLRunner
    from agents.sql_agent.snowflake_runner import SnowflakeSQLRunner

    assert PostgresSQLRunner.atomic_writes is True
    for cls in (
        DatabricksSQLRunner,
        BigQuerySQLRunner,
        SnowflakeSQLRunner,
        RedshiftSQLRunner,
    ):
        assert cls.atomic_writes is False, cls.__name__


def test_scratch_cleanup_is_generic_rather_than_per_connector():
    """A new source-side connector must not silently skip cleanup and leave
    full copies of the customer's tables behind."""
    import inspect

    from api import source_lifecycle

    source = inspect.getsource(source_lifecycle.drop_scratch_tables)
    assert "runner_for" in source and "flt_" in source


def test_mongodb_cleanup_drops_registered_orphan_and_annotation_collections(monkeypatch):
    from agents.mongo_agent import MongoScratchRunner
    from api.source_lifecycle import drop_scratch_tables
    from filters.annotate_mongo import session_collection_prefix

    session_id = "session-mongo-cleanup"
    work_orphan = "stych_session_mongo_cleanup_orphan"
    annotation = f"{session_collection_prefix(session_id)}step_hash"

    class _Runner:
        def __init__(self):
            self.dropped = []

        def list_temp_tables_with_prefix(self, prefix):
            return [name for name in (work_orphan, annotation) if name.startswith(prefix)]

        def drop_temp_table(self, table):
            self.dropped.append(table)

    runner = _Runner()
    monkeypatch.setattr(MongoScratchRunner, "from_meta", lambda meta: runner)
    meta = {
        "source": "mongodb",
        "connection_url": "mongodb://localhost:27017",
        "database": "sales",
        "working_copies": [{"name": "working", "table": "stych_registered"}],
    }

    drop_scratch_tables(session_id, meta)

    assert set(runner.dropped) == {"stych_registered", work_orphan, annotation}


# -- the analysis agent must speak the engine's own SQL ---------------------


def test_analysis_prompt_names_the_real_engine_not_postgres():
    """Telling the model it is writing PostgreSQL against BigQuery produced
    silently wrong answers, not errors: a double-quoted identifier is a string
    literal there, so every row came back as its own column names."""
    from agents.analysis.agent import _sql_react_prompt

    for name in ("bigquery", "snowflake", "redshift", "databricks"):
        prompt = _sql_react_prompt(get_dialect(name))
        assert get_dialect(name).display_name in prompt
        assert "PostgreSQL" not in prompt, name


def test_bigquery_prompt_warns_that_double_quotes_are_string_literals():
    from agents.analysis.agent import _sql_react_prompt

    prompt = _sql_react_prompt(get_dialect("bigquery"))
    assert "STRING LITERAL" in prompt
    assert "backticks" in prompt


def test_every_dialect_carries_prompt_guidance():
    for name in ("postgres", "databricks", "duckdb", "bigquery", "snowflake", "redshift"):
        d = get_dialect(name)
        assert d.display_name and d.syntax_notes


def test_analysis_builds_the_table_reference_through_the_runner():
    """A hardcoded '"schema"."table"' was handed to the model verbatim."""
    import inspect

    from agents.analysis import agent
    from agents.sql_agent import agent as sql_agent

    source = inspect.getsource(agent)
    sql_source = inspect.getsource(sql_agent)
    assert "describe_read_target" in source
    assert "runner.quote_relation" in sql_source
    assert 'temp_ref = f\'"{temp_schema}"."{temp_table}"\'' not in source
