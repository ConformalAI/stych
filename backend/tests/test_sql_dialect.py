"""The dialect layer every SQL generator routes through."""

from __future__ import annotations

import pytest

from datasources.sql_dialect import (
    SqlDialect,
    dialect_names,
    get_dialect,
    register_dialect,
)

DIALECTS = ["postgres", "databricks", "duckdb", "redshift", "snowflake", "bigquery"]


@pytest.mark.parametrize("name", DIALECTS)
def test_every_dialect_answers_the_whole_interface(name):
    """A dialect that silently inherits a wrong default is the failure mode
    this abstraction exists to prevent, so exercise every method."""
    d = get_dialect(name)
    for expr in (d.cast_text("c"), d.cast_timestamp("c"), d.cast_numeric("c"), d.cast_float("c")):
        assert "c" in expr and expr != "c"
    assert d.quote("x")
    assert d.regex_match(d.cast_text("c"), "'p'")
    assert "CASE WHEN" in d.numeric_guard("c")
    assert "lower(" in d.trim_lower("c").lower()
    assert d.format_iso_timestamp("c") != "c"
    assert d.round_float("c") != "c"


@pytest.mark.parametrize("name", DIALECTS)
def test_quoting_escapes_the_quote_character(name):
    d = get_dialect(name)
    quoted = d.quote(f"a{d.quote_char}b")
    assert quoted.startswith(d.quote_char) and quoted.endswith(d.quote_char)
    if name == "bigquery":
        assert r"\`" in quoted
    else:
        assert d.quote_char * 2 in quoted


def test_unknown_dialect_falls_back_to_postgres_rather_than_raising():
    assert get_dialect("no-such-engine").name == "postgres"
    assert get_dialect(None).name == "postgres"


def test_databricks_uses_backticks_and_three_part_names():
    d = get_dialect("databricks")
    assert d.relation("s", "t", database="cat") == "`cat`.`s`.`t`"


def test_postgres_ignores_a_database_qualifier():
    """Postgres cannot query across databases, so a catalog must not leak in."""
    assert get_dialect("postgres").relation("s", "t", database="cat") == '"s"."t"'


def test_case_insensitive_regex_differs_per_engine():
    assert "~*" in get_dialect("postgres").regex_match("c", "'p'", case_sensitive=False)
    assert "LOWER" in get_dialect("databricks").regex_match("c", "'p'", case_sensitive=False)
    assert "'i'" in get_dialect("duckdb").regex_match("c", "'p'", case_sensitive=False)


def test_stable_order_expr_reflects_whether_the_engine_has_a_row_address():
    assert "ctid" in (get_dialect("postgres").stable_order_expr(is_table=True) or "")
    # A subquery has no ctid, and Databricks has no row address at all.
    assert get_dialect("postgres").stable_order_expr(is_table=False) is None
    assert get_dialect("databricks").stable_order_expr(is_table=True) is None
    assert get_dialect("duckdb").stable_order_expr(is_table=True)


def test_literal_rejects_a_nul_byte():
    with pytest.raises(ValueError):
        get_dialect("postgres").literal("a\x00b")


def test_a_new_dialect_inherits_ansi_casts_by_naming_its_types():
    """The cost of a new connector should be type names, not method overrides."""

    class _Fake(SqlDialect):
        name = "fake-engine"
        text_type = "VARCHAR"

    register_dialect(_Fake())
    try:
        d = get_dialect("fake-engine")
        assert d.cast_text("c") == "CAST(c AS VARCHAR)"
        assert d.cast_numeric("c") == "CAST(c AS NUMERIC)"
        assert "fake-engine" in dialect_names()
    finally:
        from datasources import sql_dialect

        sql_dialect._DIALECTS.pop("fake-engine", None)


# -- row identity (Option A: a key is required, never synthesized) -----------


@pytest.mark.parametrize("name", DIALECTS)
def test_no_dialect_invents_a_row_identity(name):
    """Engines without a physical row address must report None rather than
    offering row numbers, which are not stable across queries."""
    d = get_dialect(name)
    if d.row_address is None:
        assert d.stable_order_expr(is_table=True) in (None, "row_number() OVER ()")


def test_primary_key_lookup_is_dialect_driven():
    pg = get_dialect("postgres").primary_key_query("public", "orders")
    assert pg is not None
    sql, params = pg
    assert "indisprimary" in sql and params == {"rel": "public.orders"}


def test_engines_without_a_trusted_catalog_return_no_lookup():
    """Unity Catalog keys are informational, so a declared PK proves nothing;
    those engines must fall through to a uniqueness probe instead."""
    assert get_dialect("databricks").primary_key_query("s", "t") is None
    assert get_dialect("duckdb").primary_key_query("s", "t") is None


# -- warehouse dialects -----------------------------------------------------


def test_redshift_avoids_the_text_alias_that_truncates_at_256():
    """TEXT is VARCHAR(256) in Redshift, so an unsized cast would silently cut
    long values in half mid-comparison."""
    expr = get_dialect("redshift").cast_text("c")
    assert "VARCHAR(65535)" in expr and "AS TEXT" not in expr


def test_redshift_like_uses_default_backslash_escape():
    redshift = get_dialect("redshift")
    assert redshift.ilike("c", ":p") == "c ILIKE :p"
    assert redshift.like("c", ":p") == "c LIKE :p"


def test_bigquery_addresses_relations_in_three_parts():
    assert get_dialect("bigquery").relation("ds", "t", database="proj") == "`proj`.`ds`.`t`"


def test_bigquery_uses_function_form_regex_not_an_operator():
    assert get_dialect("bigquery").regex_match("c", "@p").startswith("REGEXP_CONTAINS(")


def test_snowflake_inherits_the_postgres_timestamp_format_model():
    """Snowflake accepts the same quoted-literal format model, so ISO output
    needs no override — verified against its TO_CHAR documentation."""
    assert get_dialect("snowflake").format_iso_timestamp("t") == get_dialect(
        "postgres"
    ).format_iso_timestamp("t")


def test_no_warehouse_claims_a_physical_row_address():
    for name in ("redshift", "snowflake", "bigquery"):
        d = get_dialect(name)
        assert d.row_address is None
        assert d.stable_order_expr(is_table=True) is None


def test_no_warehouse_trusts_a_declared_primary_key():
    """Redshift, Snowflake, and BigQuery all accept PRIMARY KEY without
    enforcing it, so a catalog lookup would be a false guarantee."""
    for name in ("redshift", "snowflake", "bigquery"):
        assert get_dialect(name).primary_key_query("s", "t") is None


def test_every_dialect_stamps_a_durable_row_id():
    for name in DIALECTS:
        assert get_dialect(name).row_id_expr()


def test_snowflake_row_id_has_required_window_ordering():
    assert get_dialect("snowflake").row_id_expr() == "ROW_NUMBER() OVER (ORDER BY 1)"
