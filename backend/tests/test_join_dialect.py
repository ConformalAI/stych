"""Statement-shape tests for source-side join dialects."""

from __future__ import annotations

import pytest

from datasources.join_dialect import get_join_dialect


@pytest.mark.parametrize(
    ("name", "relation", "materialized", "preview"),
    [
        (
            "postgres",
            '"public"."orders"',
            'CREATE TABLE "stych_temp"."joined" AS SELECT * FROM (SELECT "id" FROM "public"."orders") AS _stych_join',
            'SELECT * FROM (SELECT "id" FROM "public"."orders") AS _stych_preview LIMIT 25',
        ),
        (
            "bigquery",
            "`project`.`dataset`.`orders`",
            "CREATE TABLE `project`.`stych_temp`.`joined` AS SELECT * FROM (SELECT id FROM `project`.`dataset`.`orders`) AS _stych_join",
            "SELECT * FROM (SELECT id FROM `project`.`dataset`.`orders`) AS _stych_preview LIMIT 25",
        ),
        (
            "databricks",
            "`main`.`gold`.`orders`",
            "CREATE TABLE `main`.`stych_temp`.`joined` TBLPROPERTIES ('delta.columnMapping.mode' = 'name', 'delta.minReaderVersion' = '2', 'delta.minWriterVersion' = '5') AS SELECT * FROM (SELECT `Transaction ID` FROM `main`.`gold`.`orders`) AS _stych_join",
            "SELECT * FROM (SELECT `Transaction ID` FROM `main`.`gold`.`orders`) AS _stych_preview LIMIT 25",
        ),
        (
            "sqlserver",
            "[dbo].[orders]",
            "SELECT * INTO [stych_temp].[joined] FROM (SELECT [id] FROM [dbo].[orders]) AS _stych_join",
            "SELECT TOP 25 * FROM (SELECT [id] FROM [dbo].[orders]) AS _stych_preview",
        ),
    ],
)
def test_join_dialect_owns_relation_materialization_and_preview(
    name, relation, materialized, preview
):
    dialect = get_join_dialect(name)
    schema = (
        "dbo"
        if name == "sqlserver"
        else ("dataset" if name == "bigquery" else ("gold" if name == "databricks" else "public"))
    )
    database = "project" if name == "bigquery" else ("main" if name == "databricks" else None)
    assert dialect.quote_relation(schema, "orders", database=database) == relation
    target = (
        "[stych_temp].[joined]"
        if name == "sqlserver"
        else (
            "`project`.`stych_temp`.`joined`"
            if name == "bigquery"
            else (
                "`main`.`stych_temp`.`joined`" if name == "databricks" else '"stych_temp"."joined"'
            )
        )
    )
    query = (
        "SELECT [id] FROM [dbo].[orders]"
        if name == "sqlserver"
        else (
            "SELECT id FROM `project`.`dataset`.`orders`"
            if name == "bigquery"
            else (
                "SELECT `Transaction ID` FROM `main`.`gold`.`orders`"
                if name == "databricks"
                else 'SELECT "id" FROM "public"."orders"'
            )
        )
    )
    assert dialect.materialize_join(target, query) == materialized
    assert dialect.preview_query(query, 25) == preview


def test_join_dialect_quotes_identifiers_with_connector_rules():
    assert get_join_dialect("sqlserver").quote_identifier("a]b") == "[a]]b]"
    assert get_join_dialect("bigquery").quote_identifier("a`b") == r"`a\`b`"


def test_join_dialect_rejects_empty_or_negative_preview_inputs():
    dialect = get_join_dialect("postgres")
    with pytest.raises(ValueError, match="query"):
        dialect.preview_query("", 10)
    with pytest.raises(ValueError, match="negative"):
        dialect.preview_query("SELECT 1", -1)
