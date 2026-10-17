"""One place where SQL dialects differ.

Every subsystem that generates SQL — viz aggregation pushdown, filter
predicates, the keyed diff, annotations — needs the same handful of things
spelled differently per engine: how to quote, how to cast, how to match a
regex, how to format a timestamp, how to order rows stably. Those were binary
``if dialect == "databricks"`` branches scattered across those modules, with
Postgres as the implicit default; adding a connector meant editing every site.

Here each engine answers the same questions once. A new connector is a subclass
plus a registry entry, and the generators never name an engine.

Postgres is the base because the existing generators were written in its
idiom; a subclass overrides only what genuinely differs.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Optional

# ISO-8601 second precision, per engine's own format vocabulary.
_PG_ISO = 'YYYY-MM-DD"T"HH24:MI:SS'
_JAVA_ISO = "yyyy-MM-dd'T'HH:mm:ss"
_STRFTIME_ISO = "%Y-%m-%dT%H:%M:%S"

# Strings that look like a number, used to guard a text→numeric cast.
NUMERIC_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"


class UnsupportedDialectFeature(NotImplementedError):
    """The engine has no faithful equivalent, so the feature is refused."""


class SqlDialect:
    """Postgres semantics; subclasses override only real differences."""

    name = "postgres"
    #: What to call this engine when instructing a model to write SQL for it.
    display_name = "PostgreSQL"
    #: Engine-specific syntax an LLM must follow. Telling a model it is writing
    #: Postgres when it is not produces silently wrong results rather than
    #: errors — a double-quoted identifier is a *string literal* on BigQuery, so
    #: every row comes back as its own column names.
    syntax_notes = (
        'Quote identifiers with double quotes ("schema"."table"). Use x::numeric / '
        "x::text for casts, ~ and ~* for regex, to_char for date formatting."
    )
    quote_char = '"'
    #: Identifier quoting for a relation reference; three-part names (catalog)
    #: are only meaningful on engines that address a database in the FROM list.
    supports_database_qualifier = False
    #: An expression giving each row a stable ordering token, or None when the
    #: engine offers no physical row address.
    row_address = "ctid"

    # Casts are rendered as ANSI ``CAST(x AS type)`` rather than any engine's
    # shorthand, so a new dialect usually only has to name its types.
    text_type = "TEXT"
    timestamp_type = "TIMESTAMP"
    numeric_type = "NUMERIC"
    float_type = "DOUBLE PRECISION"

    # -- identifiers --------------------------------------------------------

    def quote(self, name: str) -> str:
        q = self.quote_char
        return q + str(name).replace(q, q + q) + q

    def _pg_catalog_primary_key(self, schema: str, table: str) -> tuple[str, dict]:
        return (
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = CAST(:rel AS regclass) AND i.indisprimary",
            {"rel": f"{schema}.{table}"},
        )

    def relation(self, schema: str, table: str, *, database: Optional[str] = None) -> str:
        ref = f"{self.quote(schema)}.{self.quote(table)}"
        if database and self.supports_database_qualifier:
            return f"{self.quote(database)}.{ref}"
        return ref

    # -- casts --------------------------------------------------------------

    def _cast(self, expr: str, type_name: str) -> str:
        return f"CAST({expr} AS {type_name})"

    def cast_text(self, expr: str) -> str:
        return self._cast(expr, self.text_type)

    def cast_timestamp(self, expr: str) -> str:
        return self._cast(expr, self.timestamp_type)

    def cast_numeric(self, expr: str) -> str:
        return self._cast(expr, self.numeric_type)

    def cast_float(self, expr: str) -> str:
        return self._cast(expr, self.float_type)

    # -- predicates ---------------------------------------------------------

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        """Boolean regex test. *pattern_sql* is already a literal or bind marker."""
        return f"{expr} {'~' if case_sensitive else '~*'} {pattern_sql}"

    def numeric_guard(self, expr: str) -> str:
        """Value as a number when it parses as one, else NULL."""
        text = self.cast_text(expr)
        return (
            f"CASE WHEN {self.regex_match(text, self.literal(NUMERIC_PATTERN))} "
            f"THEN {self.cast_numeric(text)} END"
        )

    def trim_lower(self, expr: str) -> str:
        return f"btrim(lower({self.cast_text(expr)}))"

    def count_if(self, predicate: str) -> str:
        """Count rows matching *predicate*.

        The SQL-standard ``COUNT(*) FILTER (WHERE ...)`` is not accepted by
        Snowflake, Redshift, or BigQuery, so engines that lack it fall back to a
        conditional SUM with identical semantics.
        """
        return f"COUNT(*) FILTER (WHERE {predicate})"

    def _count_if_sum(self, predicate: str) -> str:
        return f"SUM(CASE WHEN ({predicate}) THEN 1 ELSE 0 END)"

    def like(self, expr: str, pattern_sql: str) -> str:
        """Case-sensitive LIKE, with the escape clause the engine accepts."""
        return f"{expr} LIKE {pattern_sql} ESCAPE '\\'"

    def ilike(self, expr: str, pattern_sql: str) -> str:
        """Case-insensitive LIKE, with the escape clause the engine accepts."""
        return f"{expr} ILIKE {pattern_sql} ESCAPE '\\'"

    # -- output formatting --------------------------------------------------

    def format_iso_timestamp(self, expr: str) -> str:
        return f"to_char({self.cast_timestamp(expr)}, '{_PG_ISO}')"

    def round_float(self, expr: str, places: int = 6) -> str:
        return self.cast_float(f"ROUND({self.cast_numeric(expr)}, {places})")

    # -- literals -----------------------------------------------------------

    # -- scalar functions ---------------------------------------------------

    def bool_literal(self, value: bool) -> str:
        """A boolean constant. T-SQL has no boolean type or literals."""
        return "TRUE" if value else "FALSE"

    def bool_to_text(self, predicate: str) -> str:
        """A predicate rendered as the text the pandas engine produces.

        Postgres can cast a boolean directly; T-SQL has no boolean *value*, so
        a predicate cannot be selected at all without a CASE around it.
        """
        return self.cast_text(predicate)

    def select_bool(self, predicate: str) -> str:
        """Render a predicate as a selectable value."""
        return f"({predicate})"

    def test_bool(self, expr: str) -> str:
        """Render a selected boolean value back as a predicate."""
        return expr

    def distinct_from(self, left: str, right: str) -> str:
        """NULL-safe inequality."""
        return f"({left} IS DISTINCT FROM {right})"

    def ceil(self, expr: str) -> str:
        return f"CEIL({expr})"

    def length(self, expr: str) -> str:
        return f"LENGTH({expr})"

    def least(self, left: str, right: str) -> str:
        """Smaller of two values. LEAST/GREATEST are not universal."""
        return f"LEAST({left}, {right})"

    def greatest(self, left: str, right: str) -> str:
        return f"GREATEST({left}, {right})"

    def date_trunc(self, unit: str, expr: str) -> str:
        """Period start for *unit*, as a timestamp."""
        return f"date_trunc('{unit}', {expr})"

    def literal(self, value: str) -> str:
        if "\x00" in value:
            raise ValueError("SQL literal contains NUL byte")
        return "'" + value.replace("'", "''") + "'"

    # -- statement shapes ---------------------------------------------------

    def select_prefix(self, limit: Optional[int]) -> str:
        """Text between SELECT and the column list, for engines that cap there.

        T-SQL has no LIMIT: it caps with ``SELECT TOP n``, a prefix rather than a
        suffix, so both ends are expressed here instead of assuming a suffix.
        """
        return ""

    def limit_suffix(self, limit: Optional[int]) -> str:
        """Trailing row cap, or empty when the engine caps with a prefix."""
        return "" if limit is None else f" LIMIT {int(limit)}"

    def pagination_suffix(self, limit_sql: str, offset_sql: str) -> str:
        """Bound-parameter pagination appended after ORDER BY."""
        return f" LIMIT {limit_sql} OFFSET {offset_sql}"

    #: Whether ``ORDER BY x NULLS LAST`` is accepted. T-SQL has no such
    #: modifier, and callers ordering by an output alias cannot simply wrap it
    #: in a CASE either — an alias may not appear inside an ORDER BY expression
    #: there — so they project a null-rank column instead.
    supports_nulls_ordering = True

    def order_term(self, expr: str, direction: str) -> str:
        """One ORDER BY term, nulls last in both directions.

        The engines disagree on where nulls sort by default, and every viz
        engine — pandas included — puts them last, so the ordering is stated
        rather than inherited.
        """
        order = "ASC" if direction == "asc" else "DESC"
        if not self.supports_nulls_ordering:
            return f"CASE WHEN {expr} IS NULL THEN 1 ELSE 0 END ASC, {expr} {order}"
        return f"{expr} {order} NULLS LAST"

    def create_table_as(self, dest: str, select_list: str, from_clause: str) -> str:
        """Materialize a query into *dest*.

        T-SQL has no CREATE TABLE AS SELECT; it writes ``SELECT ... INTO dest``.
        """
        return f"CREATE TABLE {dest} AS SELECT {select_list} FROM {from_clause}"

    def drop_table_if_exists(self, ref: str) -> str:
        return f"DROP TABLE IF EXISTS {ref}"

    # -- row identity -------------------------------------------------------

    def primary_key_query(self, schema: str, table: str) -> Optional[tuple[str, dict]]:
        """``(sql, params)`` selecting the single-column primary key, or None
        when this engine offers no catalog lookup we trust.

        Returning None is not "no key": callers fall back to probing id-like
        columns for uniqueness, which needs no catalog support.
        """
        return self._pg_catalog_primary_key(schema, table)

    def row_id_expr(self) -> str:
        """Expression stamping a durable row number when a copy is materialized.

        Evaluated once at write time and stored, so an unordered window is fine
        — we are assigning identity, not reproducing an order.
        """
        return "ROW_NUMBER() OVER ()"

    # -- row ordering -------------------------------------------------------

    def stable_order_expr(self, *, is_table: bool) -> Optional[str]:
        """Deterministic tiebreak for unsorted paging, or None when unavailable."""
        if self.row_address and is_table:
            return self.cast_text(self.row_address)
        return None


class DatabricksDialect(SqlDialect):
    name = "databricks"
    display_name = "Databricks SQL"
    syntax_notes = (
        "Quote identifiers with backticks (`catalog`.`schema`.`table`). Use "
        "CAST(x AS DOUBLE) / CAST(x AS STRING), RLIKE for regex, date_format and "
        "date_trunc for dates. Do not use Postgres syntax (to_char, ::numeric, ~)."
    )
    quote_char = "`"
    supports_database_qualifier = True
    # Delta tables expose no physical row address usable as an order key.
    row_address = None

    text_type = "STRING"
    numeric_type = "DOUBLE"
    float_type = "DOUBLE"

    def primary_key_query(self, schema: str, table: str):
        # Unity Catalog primary keys are informational and not enforced, so a
        # declared key proves nothing about uniqueness. Probe instead.
        return None

    def row_id_expr(self) -> str:
        # Spark's ROW_NUMBER requires an ORDER BY clause; a constant satisfies it.
        return "ROW_NUMBER() OVER (ORDER BY (SELECT 1))"

    def create_table_as(self, dest: str, select_list: str, from_clause: str) -> str:
        """Materialize Delta scratch tables without narrowing source schemas.

        Column mapping preserves user-facing names containing spaces and other
        Parquet-reserved characters.  This applies to generic CTAS consumers
        such as source-side joins; the Databricks runner uses the same property
        when it stamps a source working copy.
        """
        properties = (
            "TBLPROPERTIES ('delta.columnMapping.mode' = 'name', "
            "'delta.minReaderVersion' = '2', 'delta.minWriterVersion' = '5')"
        )
        return f"CREATE TABLE {dest} {properties} AS SELECT {select_list} FROM {from_clause}"

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        if case_sensitive:
            return f"{expr} RLIKE {pattern_sql}"
        return f"LOWER({expr}) RLIKE LOWER({pattern_sql})"

    def trim_lower(self, expr: str) -> str:
        return f"trim(lower({self.cast_text(expr)}))"

    def format_iso_timestamp(self, expr: str) -> str:
        escaped = _JAVA_ISO.replace("'", "\\'")
        return f"date_format({self.cast_timestamp(expr)}, '{escaped}')"


class DuckDBDialect(SqlDialect):
    name = "duckdb"
    # DuckDB has rowid, but the session snapshots are materialized relations
    # whose order is already deterministic; row_number keeps that explicit.
    row_address = None

    numeric_type = "DOUBLE"
    float_type = "DOUBLE"

    def primary_key_query(self, schema: str, table: str):
        return None

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        options = "'c'" if case_sensitive else "'i'"
        return f"regexp_matches({expr}, {pattern_sql}, {options})"

    def format_iso_timestamp(self, expr: str) -> str:
        return f"strftime({self.cast_timestamp(expr)}, '{_STRFTIME_ISO}')"

    def stable_order_expr(self, *, is_table: bool) -> Optional[str]:
        return "row_number() OVER ()"


class RedshiftDialect(SqlDialect):
    """Postgres-derived, but not Postgres where it counts."""

    name = "redshift"
    display_name = "Amazon Redshift SQL"
    syntax_notes = (
        'Quote identifiers with double quotes ("schema"."table"). CAST(x AS TEXT) is '
        "VARCHAR(256) and truncates — cast to VARCHAR(65535) instead. There is no "
        "COUNT(*) FILTER; use SUM(CASE WHEN ... THEN 1 ELSE 0 END)."
    )
    # Redshift defines no system columns at all — there is no ctid to page by.
    row_address = None
    # TEXT is an alias for VARCHAR(256) in Redshift and an unsized CAST inherits
    # that limit, so a long value would be silently truncated mid-comparison.
    # 65535 is the maximum width Redshift allows.
    text_type = "VARCHAR(65535)"

    def primary_key_query(self, schema: str, table: str):
        # Redshift accepts PRIMARY KEY but never enforces it, so a declared key
        # is a hint, not a guarantee of uniqueness. Probe instead.
        return None

    def count_if(self, predicate: str) -> str:
        # Redshift has no FILTER clause.
        return self._count_if_sum(predicate)

    def like(self, expr: str, pattern_sql: str) -> str:
        # Backslash is Redshift's default LIKE escape. An explicit ESCAPE
        # clause makes LIKE/ILIKE unsupported when the predicate is projected
        # from a table (the filter engine projects match flags).
        return f"{expr} LIKE {pattern_sql}"

    def ilike(self, expr: str, pattern_sql: str) -> str:
        return f"{expr} ILIKE {pattern_sql}"

    def select_bool(self, predicate: str) -> str:
        # Redshift accepts pattern predicates in WHERE/CASE but rejects a raw
        # LIKE/ILIKE predicate as a SELECT-list value.
        return f"CASE WHEN ({predicate}) THEN 1 ELSE 0 END"

    def test_bool(self, expr: str) -> str:
        return f"({expr} = 1)"


class SnowflakeDialect(SqlDialect):
    name = "snowflake"
    display_name = "Snowflake SQL"
    syntax_notes = (
        'Quote identifiers with double quotes ("DATABASE"."SCHEMA"."TABLE"); unquoted '
        "names fold to UPPER CASE. Use CAST(x AS FLOAT) / CAST(x AS VARCHAR), RLIKE "
        "for regex, TO_CHAR for dates. There is no COUNT(*) FILTER — use "
        "SUM(CASE WHEN ... THEN 1 ELSE 0 END)."
    )
    # Relations are addressed as "DATABASE"."SCHEMA"."TABLE".
    supports_database_qualifier = True
    row_address = None

    text_type = "VARCHAR"
    float_type = "FLOAT"

    def primary_key_query(self, schema: str, table: str):
        # Snowflake constraints are informational and unenforced.
        return None

    def row_id_expr(self) -> str:
        # Snowflake requires an ORDER BY in ROW_NUMBER window specifications.
        return "ROW_NUMBER() OVER (ORDER BY 1)"

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        if case_sensitive:
            return f"{expr} RLIKE {pattern_sql}"
        return f"LOWER({expr}) RLIKE LOWER({pattern_sql})"

    def trim_lower(self, expr: str) -> str:
        # No btrim; TRIM defaults to stripping spaces from both ends.
        return f"TRIM(LOWER({self.cast_text(expr)}))"

    def count_if(self, predicate: str) -> str:
        # Snowflake has no FILTER clause.
        return self._count_if_sum(predicate)


class BigQueryDialect(SqlDialect):
    name = "bigquery"
    display_name = "BigQuery Standard SQL"
    syntax_notes = (
        "Quote identifiers with backticks (`project.dataset.table`). Double quotes "
        "mean a STRING LITERAL, not an identifier — \"col\" returns the text 'col' "
        "for every row, so never quote a column that way. Use CAST(x AS FLOAT64) / "
        "CAST(x AS STRING), REGEXP_CONTAINS for regex, FORMAT_TIMESTAMP for dates, "
        "COUNTIF instead of COUNT(*) FILTER. There is no ILIKE."
    )
    quote_char = "`"
    # Relations are addressed as `project`.`dataset`.`table`.
    supports_database_qualifier = True
    row_address = None

    text_type = "STRING"
    numeric_type = "NUMERIC"
    float_type = "FLOAT64"

    def quote(self, name: str) -> str:
        # Backticks inside identifiers are backslash-escaped, not doubled.
        text = str(name).replace("\\", "\\\\").replace("`", "\\`")
        return f"`{text}`"

    def primary_key_query(self, schema: str, table: str):
        # BigQuery primary keys are unenforced metadata.
        return None

    def row_id_expr(self) -> str:
        # ROW_NUMBER needs a window spec here and there is no meaningful order to
        # give it. Identity only has to be unique and durable, which a UUID is.
        return "GENERATE_UUID()"

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        if case_sensitive:
            return f"REGEXP_CONTAINS({expr}, {pattern_sql})"
        return f"REGEXP_CONTAINS(LOWER({expr}), LOWER({pattern_sql}))"

    def trim_lower(self, expr: str) -> str:
        return f"TRIM(LOWER({self.cast_text(expr)}))"

    def format_iso_timestamp(self, expr: str) -> str:
        return f"FORMAT_TIMESTAMP('{_STRFTIME_ISO}', {self.cast_timestamp(expr)})"

    def count_if(self, predicate: str) -> str:
        # BigQuery has no FILTER clause; COUNTIF is its own spelling.
        return f"COUNTIF({predicate})"

    def like(self, expr: str, pattern_sql: str) -> str:
        # BigQuery LIKE takes no ESCAPE clause.
        return f"{expr} LIKE {pattern_sql}"

    def ilike(self, expr: str, pattern_sql: str) -> str:
        # No ILIKE operator and LIKE takes no ESCAPE clause.
        return f"LOWER({expr}) LIKE LOWER({pattern_sql})"


class SqlServerDialect(SqlDialect):
    """T-SQL. Closer to Postgres in spirit than in syntax."""

    name = "sqlserver"
    display_name = "Microsoft SQL Server T-SQL"
    syntax_notes = (
        "Quote identifiers with brackets ([schema].[table]). There is no LIMIT — "
        "use SELECT TOP n. There is no CREATE TABLE AS SELECT — use SELECT ... INTO. "
        "Cast with CAST(x AS NVARCHAR(MAX)) / CAST(x AS DECIMAL(38,10)); a bare "
        "NUMERIC cast truncates decimals. There is no ILIKE and no regex operator: "
        "use LOWER(x) LIKE LOWER(y). Use SUM(CASE WHEN ... THEN 1 ELSE 0 END) "
        "instead of COUNT(*) FILTER, and CONVERT(VARCHAR(19), x, 126) for ISO dates."
    )
    # T-SQL has %%physloc%%, but it moves when a row is updated, so it is not a
    # durable identity. Working copies carry the stamped row id like the rest.
    row_address = None

    # A bare NUMERIC cast is NUMERIC(18,0) — it silently drops the fractional
    # part, the same class of trap as Redshift's TEXT alias.
    text_type = "NVARCHAR(MAX)"
    timestamp_type = "DATETIME2"
    numeric_type = "DECIMAL(38,10)"
    float_type = "FLOAT"

    def quote(self, name: str) -> str:
        # Brackets always work; double quotes only when QUOTED_IDENTIFIER is ON.
        return "[" + str(name).replace("]", "]]") + "]"

    def primary_key_query(self, schema: str, table: str):
        # SQL Server enforces primary keys, so the catalog can be trusted.
        return (
            "SELECT c.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t "
            "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE c "
            "ON t.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
            "WHERE t.CONSTRAINT_TYPE = 'PRIMARY KEY' "
            "AND t.TABLE_SCHEMA = :schema AND t.TABLE_NAME = :tname",
            {"schema": schema, "tname": table},
        )

    def row_id_expr(self) -> str:
        # T-SQL requires an ORDER BY in the window; a constant satisfies it.
        return "ROW_NUMBER() OVER (ORDER BY (SELECT NULL))"

    def regex_match(self, expr: str, pattern_sql: str, *, case_sensitive: bool = True) -> str:
        raise UnsupportedDialectFeature(
            "SQL Server has no regular-expression operator, so regex matching is "
            "not available for this source. Use contains/starts-with instead."
        )

    def count_if(self, predicate: str) -> str:
        return self._count_if_sum(predicate)

    def like(self, expr: str, pattern_sql: str) -> str:
        return f"{expr} LIKE {pattern_sql} ESCAPE '\\'"

    def ilike(self, expr: str, pattern_sql: str) -> str:
        # Case sensitivity is a collation property, not an operator, so fold
        # both sides explicitly. Correct, but not index-friendly.
        return f"LOWER({expr}) LIKE LOWER({pattern_sql}) ESCAPE '\\'"

    def trim_lower(self, expr: str) -> str:
        return f"LTRIM(RTRIM(LOWER({self.cast_text(expr)})))"

    def format_iso_timestamp(self, expr: str) -> str:
        # Style 126 is ISO 8601 and is much cheaper than FORMAT().
        return f"CONVERT(VARCHAR(19), {self.cast_timestamp(expr)}, 126)"

    def select_prefix(self, limit: Optional[int]) -> str:
        return "" if limit is None else f"TOP {int(limit)} "

    # T-SQL sorts nulls first ascending and has no NULLS LAST modifier.
    supports_nulls_ordering = False

    def numeric_guard(self, expr: str) -> str:
        # The base implementation is regex-based and T-SQL has no regex. TRY_CAST
        # says the same thing directly, and its "parses as a number" is closer to
        # pandas' to_numeric than the strict pattern is.
        return f"TRY_CAST({self.cast_text(expr)} AS {self.numeric_type})"

    def bool_literal(self, value: bool) -> str:
        return "1" if value else "0"

    def bool_to_text(self, predicate: str) -> str:
        # There is no boolean value in T-SQL, so the predicate has to be
        # resolved by a CASE. Three-valued on purpose: an ELSE would fold NULL
        # into 'false', and a null dimension key is meant to drop its row, not
        # join the false bucket. Labels are lowercase to match a Postgres
        # boolean cast, which is what the pandas engine was tuned against.
        return (
            f"(CASE WHEN {predicate} THEN 'true' "
            f"WHEN NOT ({predicate}) THEN 'false' ELSE NULL END)"
        )

    def select_bool(self, predicate: str) -> str:
        return f"(CASE WHEN {predicate} THEN 1 ELSE 0 END)"

    def test_bool(self, expr: str) -> str:
        return f"({expr} = 1)"

    def distinct_from(self, left: str, right: str) -> str:
        return (
            f"(({left} <> {right}) OR ({left} IS NULL AND {right} IS NOT NULL) "
            f"OR ({left} IS NOT NULL AND {right} IS NULL))"
        )

    def ceil(self, expr: str) -> str:
        return f"CEILING({expr})"

    def length(self, expr: str) -> str:
        return f"LEN({expr})"

    def least(self, left: str, right: str) -> str:
        # LEAST arrived in SQL Server 2022; Azure SQL Edge and 2019 lack it.
        return f"(CASE WHEN {left} < {right} THEN {left} ELSE {right} END)"

    def greatest(self, left: str, right: str) -> str:
        return f"(CASE WHEN {left} > {right} THEN {left} ELSE {right} END)"

    def date_trunc(self, unit: str, expr: str) -> str:
        # DATETRUNC is SQL Server 2022+. DATEADD/DATEDIFF from a fixed epoch
        # works on every supported version and gives the same period start.
        part = {
            "year": "year",
            "quarter": "quarter",
            "month": "month",
            "week": "week",
            "day": "day",
            "hour": "hour",
        }[unit]
        if part == "week":
            # DATEDIFF(week, ...) depends on DATEFIRST; anchoring on a known
            # Monday (1900-01-01) makes the week start Monday like the others.
            return f"DATEADD(day, (DATEDIFF(day, '1900-01-01', {expr}) / 7) * 7, CAST('1900-01-01' AS DATETIME2))"
        return f"DATEADD({part}, DATEDIFF({part}, '1900-01-01', {expr}), CAST('1900-01-01' AS DATETIME2))"

    def limit_suffix(self, limit: Optional[int]) -> str:
        return ""

    def pagination_suffix(self, limit_sql: str, offset_sql: str) -> str:
        return f" OFFSET {offset_sql} ROWS FETCH NEXT {limit_sql} ROWS ONLY"

    def create_table_as(self, dest: str, select_list: str, from_clause: str) -> str:
        return f"SELECT {select_list} INTO {dest} FROM {from_clause}"

    def drop_table_if_exists(self, ref: str) -> str:
        # DROP TABLE IF EXISTS needs SQL Server 2016+; this works everywhere.
        return f"IF OBJECT_ID('{ref}', 'U') IS NOT NULL DROP TABLE {ref}"


_DIALECTS: Dict[str, SqlDialect] = {
    dialect.name: dialect
    for dialect in (
        SqlDialect(),
        DatabricksDialect(),
        DuckDBDialect(),
        RedshiftDialect(),
        SnowflakeDialect(),
        BigQueryDialect(),
        SqlServerDialect(),
    )
}


def register_dialect(dialect: SqlDialect) -> None:
    """Add or replace a dialect. Connectors register at import time."""
    _DIALECTS[dialect.name] = dialect


def get_dialect(name: Optional[str]) -> SqlDialect:
    """Resolve a dialect by engine name, defaulting to Postgres semantics."""
    return _DIALECTS.get(str(name or "postgres"), _DIALECTS["postgres"])


def dialect_names() -> tuple[str, ...]:
    return tuple(sorted(_DIALECTS))


# ---------------------------------------------------------------------------
# Ambient dialect
# ---------------------------------------------------------------------------

#: The dialect a statement is being compiled for. Set by the compiler and read
#: by the expression emitter, which cannot import the compiler back.
_current: ContextVar[str] = ContextVar("sql_dialect_current", default="postgres")


def current_dialect() -> SqlDialect:
    """The dialect of the statement currently being compiled."""
    return get_dialect(_current.get())


@contextmanager
def use_dialect(name: str):
    """Compile everything inside this block for *name*."""
    token = _current.set(name or "postgres")
    try:
        yield get_dialect(name)
    finally:
        _current.reset(token)
