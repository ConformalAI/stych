"""The boundary that keeps agent-authored writes off the customer's tables.

Every case here defeated the previous substring-based guard. The point of the
file is that "the scratch schema name appears in the statement" is not the same
question as "this statement writes to the scratch schema".
"""

from __future__ import annotations

import pytest

from agents.sql_agent.write_guard import (
    WriteGuardError,
    relation_schema,
    validate_write_statements,
    write_targets,
)

SCRATCH = "stych_temp"


def _blocked(sql: str, *, allowed: str = SCRATCH, fold: str = "lower") -> bool:
    try:
        validate_write_statements([sql], allowed_schema=allowed, fold=fold)
        return False
    except WriteGuardError:
        return True


# -- writes that reach the customer's own tables ----------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Reads the scratch schema, writes the source. The shape a cleaning
        # agent produces when asked to "apply the changes back".
        'INSERT INTO public.customers SELECT * FROM "stych_temp"."t1"',
        "UPDATE public.customers c SET name = s.name FROM stych_temp.t1 s WHERE c.id = s.id",
        "DELETE FROM public.customers WHERE id IN (SELECT id FROM stych_temp.t1)",
        "MERGE INTO public.customers t USING stych_temp.t1 s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET t.n = s.n",
        # The marker smuggled somewhere it has no effect.
        "DROP TABLE public.customers -- stych_temp.",
        "UPDATE public.customers SET note = 'stych_temp.'",
        "/* stych_temp */ TRUNCATE TABLE public.customers",
        # A CTE in front of the write.
        "WITH x AS (SELECT 1 FROM stych_temp.t1) INSERT INTO public.customers SELECT * FROM x",
        # No mutating verb at all, still writes.
        "SELECT * INTO public.newtable FROM stych_temp.t",
        # Unqualified: cannot be proven to be in the scratch schema.
        "DROP TABLE customers",
        "INSERT INTO customers SELECT * FROM stych_temp.t1",
    ],
)
def test_writes_outside_the_scratch_schema_are_refused(sql):
    assert _blocked(sql), f"guard allowed: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "UNLOAD ('SELECT * FROM stych_temp.t') TO 's3://bucket/x'",
        "COPY INTO 's3://bucket/x' FROM stych_temp.t",
        "EXPORT DATA OPTIONS(uri='gs://bucket/*') AS SELECT * FROM stych_temp.t",
        "PUT file:///tmp/x @%stych_temp",
        "GRANT SELECT ON stych_temp.t TO PUBLIC",
    ],
)
def test_statements_that_move_data_out_of_the_engine_are_refused(sql):
    """These write outside the database entirely — a residency concern, not
    just a source-table one."""
    assert _blocked(sql), f"guard allowed exfiltration: {sql}"


def test_stacked_statements_are_refused():
    assert _blocked('UPDATE "stych_temp"."t" SET a = 1; DROP TABLE public.customers')


def test_an_unrecognised_write_shape_fails_closed():
    """A statement whose target cannot be identified is refused, not allowed."""
    assert _blocked("CREATE MATERIALIZED VIEW public.mv AS SELECT 1")


# -- legitimate scratch writes still work -----------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        'UPDATE "stych_temp"."t" SET a = 1 WHERE id = 2',
        'DELETE FROM "stych_temp"."t" WHERE a IS NULL',
        'CREATE TABLE "stych_temp"."x" AS SELECT * FROM public.orders',
        'DROP TABLE IF EXISTS "stych_temp"."x"',
        'ALTER TABLE "stych_temp"."t" ADD COLUMN b INT',
        'INSERT INTO "stych_temp"."t" SELECT * FROM public.orders',
        "TRUNCATE TABLE stych_temp.t",
        # A pure read alongside writes is harmless.
        "SELECT count(*) FROM public.orders",
    ],
)
def test_scratch_writes_are_allowed(sql):
    assert not _blocked(sql), f"guard wrongly refused: {sql}"


# -- identifier folding -----------------------------------------------------


def test_snowflake_folds_identifiers_upward():
    assert not _blocked(
        'UPDATE "ANALYTICS"."STYCH_TEMP"."t" SET a = 1', allowed="STYCH_TEMP", fold="upper"
    )
    assert _blocked(
        'INSERT INTO "ANALYTICS"."PUBLIC"."c" SELECT 1', allowed="STYCH_TEMP", fold="upper"
    )


def test_backtick_and_bracket_quoting_are_understood():
    assert not _blocked("UPDATE `proj`.`stych_temp`.`t` SET a = 1")
    assert _blocked("UPDATE `proj`.`public`.`t` SET a = 1")


def test_bigquery_whole_path_backticks_are_understood():
    """BigQuery quotes the entire dotted path, not each part. Treating that as
    one unqualified identifier would refuse every legitimate write."""
    assert not _blocked("UPDATE `proj.stych_temp.t` SET a = 1")
    assert _blocked("DROP TABLE `proj.public.orders`")
    assert relation_schema("`proj.ds.tbl`") == "ds"


# -- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("schema.table", "schema"),
        ("db.schema.table", "schema"),
        ('"Db"."Schema"."Table"', "Schema"),
        ("`p`.`ds`.`t`", "ds"),
        ("table", None),
    ],
)
def test_relation_schema_extraction(relation, expected):
    assert relation_schema(relation) == expected


def test_write_targets_identifies_the_written_relation():
    assert write_targets("INSERT INTO a.b SELECT * FROM c.d") == ["a.b"]
    assert write_targets("UPDATE a.b SET x = 1") == ["a.b"]
    assert write_targets("SELECT * FROM a.b") == []


# -- every runner routes through the shared guard ---------------------------


def test_alter_set_schema_cannot_escape_scratch():
    assert _blocked("ALTER TABLE stych_temp.t SET SCHEMA public")
    assert not _blocked("ALTER TABLE stych_temp.t SET SCHEMA stych_temp")


def test_allowed_table_confines_copy_writes():
    validate_write_statements(
        ["UPDATE public.copy_orders SET qty = 0"],
        allowed_schema="public",
        allowed_table="copy_orders",
    )
    with pytest.raises(WriteGuardError, match="copy_orders"):
        validate_write_statements(
            ["UPDATE public.customers SET qty = 0"],
            allowed_schema="public",
            allowed_table="copy_orders",
        )


def test_all_source_side_runners_share_the_guard():
    """A runner with its own weaker check would be a hole; they must delegate.

    Delegation is asserted on the resolved class: each runner's
    ``_validate_write_statements`` must be the shared implementation from
    ``runner`` (Postgres) or ``warehouse_base`` (the warehouses), not a
    module-local copy.
    """
    import inspect

    from agents.sql_agent.bigquery_runner import BigQuerySQLRunner
    from agents.sql_agent.databricks_runner import DatabricksSQLRunner
    from agents.sql_agent.redshift_runner import RedshiftSQLRunner
    from agents.sql_agent.runner import PostgresSQLRunner
    from agents.sql_agent.snowflake_runner import SnowflakeSQLRunner
    from agents.sql_agent.warehouse_base import WarehouseSQLRunner

    shared_modules = {
        inspect.getmodule(PostgresSQLRunner._validate_write_statements).__name__,
        inspect.getmodule(WarehouseSQLRunner._validate_write_statements).__name__,
    }
    for cls in (
        PostgresSQLRunner,
        SnowflakeSQLRunner,
        RedshiftSQLRunner,
        BigQuerySQLRunner,
        DatabricksSQLRunner,
    ):
        owner = inspect.getmodule(cls._validate_write_statements).__name__
        assert owner in shared_modules, f"{cls.__name__} bypasses the shared write guard"
