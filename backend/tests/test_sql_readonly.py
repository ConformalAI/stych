"""Read-only SQL parser used by run_sql / warehouse custom queries."""

from __future__ import annotations

import pytest

from datasources.sql_readonly import (
    ReadOnlySQLError,
    parse_read_only_statements,
    require_attached_asset_placeholder,
)
from datasources.warehouse import WarehouseError, validate_read_only_query


def test_leading_comment_select_is_allowed():
    statements = parse_read_only_statements(
        "\n-- 1. OVERVIEW STATISTICS\nSELECT COUNT(*) AS n FROM t\n"
    )
    assert len(statements) == 1
    assert statements[0].upper().startswith("--")
    assert "SELECT" in statements[0].upper()


def test_block_comment_and_parenthesized_select():
    statements = parse_read_only_statements("/* c */ (SELECT 1)")
    assert len(statements) == 1
    assert "(SELECT 1)" in statements[0]


def test_values_table_and_from_pipe_are_query_forms():
    assert parse_read_only_statements("VALUES (1), (2)") == ["VALUES (1), (2)"]
    assert parse_read_only_statements("TABLE events") == ["TABLE events"]
    statements = parse_read_only_statements("FROM events |> SELECT *")
    assert statements == ["FROM events |> SELECT *"]


def test_semicolon_batch_splits_and_keeps_section_comments():
    statements = parse_read_only_statements(
        """
        -- 1. OVERVIEW
        SELECT 1 AS n;
        -- 2. PRODUCTS
        SELECT 2 AS n
        """
    )
    assert len(statements) == 2
    assert "SELECT 1" in statements[0]
    assert "SELECT 2" in statements[1]


def test_attached_asset_guard_allows_ctes_derived_from_placeholder():
    require_attached_asset_placeholder(
        "WITH grouped AS (SELECT item, COUNT(*) n FROM {table} GROUP BY item) "
        "SELECT * FROM grouped"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secret.orders",
        "SELECT * FROM {table} JOIN secret.customers ON 1=1",
        "SELECT * FROM {table} UNION ALL SELECT * FROM secret.orders",
        "SELECT * FROM {table} attached, secret.orders leaked",
        "SELECT * FROM (SELECT * FROM secret.orders) leaked JOIN {table} ON 1=1",
        "SELECT '{table}' AS text FROM secret.orders",
    ],
)
def test_attached_asset_guard_rejects_unattached_relations(sql):
    with pytest.raises(ReadOnlySQLError, match="attached|outside this chat"):
        require_attached_asset_placeholder(sql)


def test_trailing_write_rejects_the_whole_batch():
    with pytest.raises(ReadOnlySQLError, match="got DELETE"):
        parse_read_only_statements("SELECT 1; DELETE FROM t")


def test_select_into_and_mutating_cte_are_writes():
    with pytest.raises(ReadOnlySQLError, match="got INTO"):
        parse_read_only_statements("SELECT * INTO new_events FROM events")
    with pytest.raises(ReadOnlySQLError, match="got INSERT"):
        parse_read_only_statements("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")


def test_in_predicate_is_not_into():
    statements = parse_read_only_statements("SELECT * FROM t WHERE x IN (1, 2)")
    assert len(statements) == 1


def test_update_and_empty_are_rejected():
    with pytest.raises(ReadOnlySQLError, match="got UPDATE"):
        parse_read_only_statements("UPDATE t SET a = 1")
    with pytest.raises(ReadOnlySQLError, match="empty"):
        parse_read_only_statements("   ")
    with pytest.raises(ReadOnlySQLError, match="empty/comment-only"):
        parse_read_only_statements("-- just a comment")


def test_warehouse_custom_query_stays_single_select():
    assert validate_read_only_query("-- a harmless comment\nSELECT 1").endswith("SELECT 1")
    with pytest.raises(WarehouseError):
        validate_read_only_query("VALUES (1)")
    with pytest.raises(WarehouseError):
        validate_read_only_query("SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM OPENQUERY(other, 'INSERT INTO t VALUES (1)')",
        "SELECT dblink('dbname=x', 'INSERT INTO t VALUES (1)')",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'server'; 'INSERT INTO t VALUES (1)')",
    ],
)
def test_linked_server_carriers_are_rejected(sql):
    with pytest.raises(ReadOnlySQLError, match="OPENQUERY|DBLINK|OPENROWSET"):
        parse_read_only_statements(sql)
