"""
Tools for SQL-based data linking (joining) operations.

Handles loading heterogeneous data (CSV, JSON, dict) into PostgreSQL
temporary tables, executing JOIN queries, and analysing join quality.
"""

import io
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)


def _esc_id(name: str) -> str:
    """Return a safely double-quoted Postgres identifier, escaping internal double-quotes."""
    return '"' + name.replace('"', '""') + '"'


# -------------------------------------------------------------------------
# Postgres connection helper
# -------------------------------------------------------------------------


def get_connection(conn_params: Optional[Dict[str, Any]] = None):
    """
    Create a psycopg2 connection.

    Resolution order:
      1. If `conn_params` is non-empty, its keys override everything else
         (back-compat with callers that pass an explicit dict).
      2. Otherwise, read connection settings from environment variables,
         in priority order:
            LINKING_DATABASE_URL   — dedicated linking-agent DB (e.g. the
                                     pgvector container on port 5433)
            APP_DATABASE_URL       — main app DB
            DATABASE_URL           — legacy / fallback
      3. If no env var is set either, fall back to local defaults
         (localhost:5432, dbname=stych, user=postgres, password="").

    Expected URL format:
        postgresql://user:password@host:port/dbname

    Expected conn_params keys (when used as overrides):
        host, port, dbname, user, password
    """
    # Local defaults — used only if neither env vars nor conn_params provide them
    settings: Dict[str, Any] = {
        "host": "localhost",
        "port": 5432,
        "dbname": "stych",
        "user": "postgres",
        "password": "",
    }

    # Pull from env: linking-specific URL wins, then app URL, then legacy URL.
    db_url = (
        os.getenv("LINKING_DATABASE_URL")
        or os.getenv("APP_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )
    if db_url:
        parsed = urlparse(db_url)
        if parsed.hostname:
            settings["host"] = parsed.hostname
        if parsed.port:
            settings["port"] = parsed.port
        if parsed.username:
            settings["user"] = parsed.username
        if parsed.password is not None:
            settings["password"] = parsed.password
        if parsed.path and len(parsed.path) > 1:
            settings["dbname"] = parsed.path.lstrip("/")

    # Explicit conn_params still override env (back-compat)
    if conn_params:
        settings.update(conn_params)

    return psycopg2.connect(**settings)


# -------------------------------------------------------------------------
# DataLinkingTools
# -------------------------------------------------------------------------


class DataLinkingTools:
    """Static helper methods for SQL-based data linking."""

    # ----- data ingestion ------------------------------------------------

    @staticmethod
    def load_to_temp_table(
        conn,
        dataframe: pd.DataFrame,
        table_name: str,
    ) -> Dict[str, Any]:
        """
        Load a pandas DataFrame into a PostgreSQL **temporary** table.

        The table is created inside the current session and will be
        dropped automatically when the connection closes.

        Returns metadata about the created table.
        """
        try:
            dtype_map = DataLinkingTools._pg_dtype_map(dataframe)
            col_defs = ", ".join(f"{_esc_id(col)} {pg_type}" for col, pg_type in dtype_map.items())

            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {_esc_id(table_name)};")
                cur.execute(f"CREATE TEMP TABLE {_esc_id(table_name)} ({col_defs});")

                # Bulk insert via execute_values for performance
                cols = list(dataframe.columns)
                col_list = ", ".join(_esc_id(c) for c in cols)
                tpl = "(" + ", ".join(["%s"] * len(cols)) + ")"

                rows = [
                    tuple(None if pd.isna(v) else v for v in row)
                    for row in dataframe.itertuples(index=False, name=None)
                ]

                execute_values(
                    cur,
                    f"INSERT INTO {_esc_id(table_name)} ({col_list}) VALUES %s",
                    rows,
                    template=tpl,
                    page_size=2000,
                )
            conn.commit()

            return {
                "table_name": table_name,
                "rows": len(dataframe),
                "columns": cols,
                "dtypes": {col: str(dt) for col, dt in dataframe.dtypes.items()},
            }

        except Exception as e:
            conn.rollback()
            logger.error("load_to_temp_table failed: %s", e)
            return {"error": str(e)}

    # ----- join execution -----------------------------------------------

    @staticmethod
    def execute_join(
        conn,
        left_table: str,
        right_table: str,
        join_type: str,
        left_on: str,
        right_on: str,
        selected_columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a SQL JOIN between two (temp) tables and return the result
        as a list-of-dicts plus metadata.

        Parameters
        ----------
        join_type : one of INNER, LEFT, RIGHT, FULL OUTER
        left_on / right_on : column names to join on
        selected_columns : optional list of "table.column" strings to
            include; if *None* all columns are selected (with alias
            prefixing to avoid name collisions).
        """
        valid_joins = {"INNER", "LEFT", "RIGHT", "FULL OUTER"}
        join_upper = join_type.strip().upper()
        if join_upper not in valid_joins:
            return {"error": f"Invalid join type '{join_type}'. Must be one of {valid_joins}"}

        try:
            select_clause = DataLinkingTools._build_select_clause(
                conn, left_table, right_table, selected_columns
            )

            query = (
                f"SELECT {select_clause} "
                f"FROM {_esc_id(left_table)} "
                f"{join_upper} JOIN {_esc_id(right_table)} "
                f"ON {_esc_id(left_table)}.{_esc_id(left_on)} = {_esc_id(right_table)}.{_esc_id(right_on)}"
            )

            with conn.cursor() as cur:
                cur.execute(query)
                col_names = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

            result_df = pd.DataFrame(rows, columns=col_names)

            return {
                "query": query,
                "rows": len(result_df),
                "columns": col_names,
                "data": result_df.to_dict(orient="list"),
            }

        except Exception as e:
            logger.error("execute_join failed: %s", e)
            return {"error": str(e)}

    # ----- multi-key join -----------------------------------------------

    @staticmethod
    def execute_join_multi_key(
        conn,
        left_table: str,
        right_table: str,
        join_type: str,
        join_keys: List[Dict[str, str]],
        selected_columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a JOIN on **multiple** key pairs.

        join_keys is a list of {"left": "<col>", "right": "<col>"} dicts.
        """
        valid_joins = {"INNER", "LEFT", "RIGHT", "FULL OUTER"}
        join_upper = join_type.strip().upper()
        if join_upper not in valid_joins:
            return {"error": f"Invalid join type '{join_type}'. Must be one of {valid_joins}"}

        if not join_keys:
            return {"error": "join_keys must contain at least one key pair"}

        try:
            select_clause = DataLinkingTools._build_select_clause(
                conn, left_table, right_table, selected_columns
            )

            on_parts = [
                f"{_esc_id(left_table)}.{_esc_id(k['left'])} = {_esc_id(right_table)}.{_esc_id(k['right'])}"
                for k in join_keys
            ]
            on_clause = " AND ".join(on_parts)

            query = (
                f"SELECT {select_clause} "
                f"FROM {_esc_id(left_table)} "
                f"{join_upper} JOIN {_esc_id(right_table)} "
                f"ON {on_clause}"
            )

            with conn.cursor() as cur:
                cur.execute(query)
                col_names = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

            result_df = pd.DataFrame(rows, columns=col_names)

            return {
                "query": query,
                "rows": len(result_df),
                "columns": col_names,
                "data": result_df.to_dict(orient="list"),
            }

        except Exception as e:
            logger.error("execute_join_multi_key failed: %s", e)
            return {"error": str(e)}

    # ----- join quality analysis ----------------------------------------

    @staticmethod
    def analyze_join_quality(
        conn,
        left_table: str,
        right_table: str,
        left_on: str,
        right_on: str,
    ) -> Dict[str, Any]:
        """
        Analyse the quality of a potential join **before** running it.

        Returns key overlap statistics, null counts, type compatibility,
        and cardinality information so the caller (or the LLM) can decide
        the best join strategy.
        """
        try:
            lt = _esc_id(left_table)
            rt = _esc_id(right_table)
            lo = _esc_id(left_on)
            ro = _esc_id(right_on)

            with conn.cursor() as cur:
                # Row counts
                cur.execute(f"SELECT COUNT(*) FROM {lt}")
                left_count = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {rt}")
                right_count = cur.fetchone()[0]

                # Null counts on join keys
                cur.execute(f"SELECT COUNT(*) FROM {lt} WHERE {lo} IS NULL")
                left_nulls = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {rt} WHERE {ro} IS NULL")
                right_nulls = cur.fetchone()[0]

                # Distinct key counts
                cur.execute(f"SELECT COUNT(DISTINCT {lo}) FROM {lt}")
                left_distinct = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(DISTINCT {ro}) FROM {rt}")
                right_distinct = cur.fetchone()[0]

                # Key overlap (INNER match count)
                cur.execute(
                    f"SELECT COUNT(*) FROM "
                    f"(SELECT DISTINCT {lo} FROM {lt}) l "
                    f"INNER JOIN "
                    f"(SELECT DISTINCT {ro} FROM {rt}) r "
                    f"ON l.{lo} = r.{ro}"
                )
                overlap_count = cur.fetchone()[0]

                # Predicted row counts per join type
                cur.execute(
                    f"SELECT COUNT(*) FROM {lt} " f"INNER JOIN {rt} ON {lt}.{lo} = {rt}.{ro}"
                )
                inner_count = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM {lt} " f"LEFT JOIN {rt} ON {lt}.{lo} = {rt}.{ro}"
                )
                left_join_count = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM {lt} " f"RIGHT JOIN {rt} ON {lt}.{lo} = {rt}.{ro}"
                )
                right_join_count = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM {lt} " f"FULL OUTER JOIN {rt} ON {lt}.{lo} = {rt}.{ro}"
                )
                full_join_count = cur.fetchone()[0]

            overlap_pct_left = round(overlap_count / max(left_distinct, 1) * 100, 2)
            overlap_pct_right = round(overlap_count / max(right_distinct, 1) * 100, 2)

            return {
                "left_table": {
                    "name": left_table,
                    "rows": left_count,
                    "key_column": left_on,
                    "key_nulls": left_nulls,
                    "key_distinct": left_distinct,
                },
                "right_table": {
                    "name": right_table,
                    "rows": right_count,
                    "key_column": right_on,
                    "key_nulls": right_nulls,
                    "key_distinct": right_distinct,
                },
                "overlap": {
                    "matching_keys": overlap_count,
                    "overlap_pct_left": overlap_pct_left,
                    "overlap_pct_right": overlap_pct_right,
                },
                "predicted_row_counts": {
                    "INNER": inner_count,
                    "LEFT": left_join_count,
                    "RIGHT": right_join_count,
                    "FULL OUTER": full_join_count,
                },
                "cardinality": DataLinkingTools._detect_cardinality(
                    left_count, right_count, left_distinct, right_distinct
                ),
            }

        except Exception as e:
            logger.error("analyze_join_quality failed: %s", e)
            return {"error": str(e)}

    # ----- schema inspection --------------------------------------------

    @staticmethod
    def get_table_schema(conn, table_name: str) -> Dict[str, Any]:
        """Return column names and Postgres types for a temp table."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_name = %s "
                    "ORDER BY ordinal_position",
                    (table_name,),
                )
                cols = [{"column_name": row[0], "data_type": row[1]} for row in cur.fetchall()]
            return {"table_name": table_name, "columns": cols}
        except Exception as e:
            logger.error("get_table_schema failed: %s", e)
            return {"error": str(e)}

    # ----- code generation ----------------------------------------------

    @staticmethod
    def generate_join_code(
        left_table: str,
        right_table: str,
        join_type: str,
        left_on: str,
        right_on: str,
    ) -> str:
        """Generate reproducible SQL and equivalent pandas code."""
        sql_code = (
            f"-- SQL\n"
            f"SELECT *\n"
            f'FROM "{left_table}"\n'
            f'{join_type.upper()} JOIN "{right_table}"\n'
            f'  ON "{left_table}"."{left_on}" = "{right_table}"."{right_on}";\n'
        )

        pandas_how = {
            "INNER": "inner",
            "LEFT": "left",
            "RIGHT": "right",
            "FULL OUTER": "outer",
        }.get(join_type.upper(), "inner")

        pandas_code = (
            f"# Pandas equivalent\n"
            f"result = {left_table}.merge(\n"
            f"    {right_table},\n"
            f"    how='{pandas_how}',\n"
            f"    left_on='{left_on}',\n"
            f"    right_on='{right_on}',\n"
            f")\n"
        )

        return f"{sql_code}\n{pandas_code}"

    # =====================================================================
    # Internal helpers
    # =====================================================================

    @staticmethod
    def _pg_dtype_map(df: pd.DataFrame) -> Dict[str, str]:
        """Map pandas dtypes → Postgres column types."""
        mapping: Dict[str, str] = {}
        for col in df.columns:
            dtype = df[col].dtype
            if pd.api.types.is_integer_dtype(dtype):
                mapping[col] = "BIGINT"
            elif pd.api.types.is_float_dtype(dtype):
                mapping[col] = "DOUBLE PRECISION"
            elif pd.api.types.is_bool_dtype(dtype):
                mapping[col] = "BOOLEAN"
            elif pd.api.types.is_datetime64_any_dtype(dtype):
                mapping[col] = "TIMESTAMP"
            else:
                mapping[col] = "TEXT"
        return mapping

    @staticmethod
    def _build_select_clause(
        conn,
        left_table: str,
        right_table: str,
        selected_columns: Optional[List[str]] = None,
    ) -> str:
        """
        Build a SELECT column list.

        When *selected_columns* is None, selects all columns from both
        tables with ``table_column`` aliasing to avoid name collisions.
        """
        if selected_columns:
            # Escape every caller-supplied identifier so a malicious column
            # name like `*; DROP TABLE` cannot escape the SELECT clause.
            return ", ".join(_esc_id(c) for c in selected_columns)

        # Auto-generate aliased column list
        left_schema = DataLinkingTools.get_table_schema(conn, left_table)
        right_schema = DataLinkingTools.get_table_schema(conn, right_table)

        left_cols = [c["column_name"] for c in left_schema.get("columns", [])]
        right_cols = [c["column_name"] for c in right_schema.get("columns", [])]

        parts: List[str] = []
        for c in left_cols:
            alias = f"{left_table}_{c}" if c in right_cols else c
            parts.append(f"{_esc_id(left_table)}.{_esc_id(c)} AS {_esc_id(alias)}")
        for c in right_cols:
            alias = f"{right_table}_{c}" if c in left_cols else c
            parts.append(f"{_esc_id(right_table)}.{_esc_id(c)} AS {_esc_id(alias)}")

        return ", ".join(parts)

    @staticmethod
    def _detect_cardinality(
        left_rows: int,
        right_rows: int,
        left_distinct: int,
        right_distinct: int,
    ) -> str:
        """Heuristic cardinality label: 1:1, 1:N, N:1, N:N."""
        left_is_unique = left_distinct == left_rows
        right_is_unique = right_distinct == right_rows

        if left_is_unique and right_is_unique:
            return "1:1"
        elif left_is_unique:
            return "1:N"
        elif right_is_unique:
            return "N:1"
        return "N:N"


# -------------------------------------------------------------------------
# Data parsing helpers (CSV, JSON, dict, file paths, etc. → DataFrame)
# -------------------------------------------------------------------------


def parse_input_to_dataframe(data: Any, fmt: Optional[str] = None) -> pd.DataFrame:
    """
    Convert various input formats to a pandas DataFrame.

    Supported:
      - pandas DataFrame (passthrough)
      - dict of lists  (column-oriented, same as cleaning agent)
      - list of dicts  (row-oriented / JSON records)
      - CSV string
      - TSV string
      - JSON string    (records or column-oriented)
      - file path      (.csv, .tsv, .json, .xlsx, .xls, .parquet)
      - bytes / base64 (Excel or Parquet binary)
    """
    import base64
    from pathlib import Path

    if isinstance(data, pd.DataFrame):
        return data

    # Dict of lists → column-oriented DataFrame
    if isinstance(data, dict):
        # Check if it looks like a single record vs column-oriented
        if all(isinstance(v, list) for v in data.values()):
            return pd.DataFrame(data)
        # Single record dict → wrap in list
        return pd.DataFrame([data])

    # List of dicts → row-oriented DataFrame
    if isinstance(data, list):
        if len(data) == 0:
            return pd.DataFrame()
        if isinstance(data[0], dict):
            return pd.DataFrame(data)
        # List of lists → try as rows
        if isinstance(data[0], list):
            return pd.DataFrame(data)

    # Bytes → try Excel or Parquet
    if isinstance(data, (bytes, bytearray)):
        return _parse_bytes(data, fmt)

    if isinstance(data, str):
        fmt_lower = (fmt or "").lower()

        # Base64-encoded binary (Excel, Parquet)
        if fmt_lower in ("xlsx", "xls", "parquet", "base64"):
            try:
                raw_bytes = base64.b64decode(data)
                return _parse_bytes(raw_bytes, fmt_lower)
            except Exception:
                pass

        # File path
        path = Path(data)
        if path.exists() and path.is_file():
            return _parse_file(path, fmt_lower)

        # TSV string
        if fmt_lower == "tsv" or ("\t" in data and "\n" in data):
            return pd.read_csv(io.StringIO(data), sep="\t")

        # CSV string
        if fmt_lower == "csv" or ("," in data and "\n" in data):
            return pd.read_csv(io.StringIO(data))

        # JSON string
        try:
            parsed = json.loads(data)
            return parse_input_to_dataframe(parsed, fmt=fmt)
        except (json.JSONDecodeError, ValueError):
            pass

        # Semicolon-separated
        if ";" in data and "\n" in data:
            return pd.read_csv(io.StringIO(data), sep=";")

        # Last resort: try CSV
        return pd.read_csv(io.StringIO(data))

    raise ValueError(f"Unsupported data type: {type(data).__name__}")


def _parse_bytes(raw: bytes, fmt: Optional[str] = None) -> pd.DataFrame:
    """Parse binary data (Excel or Parquet)."""
    fmt = (fmt or "").lower()
    if fmt in ("parquet",):
        return pd.read_parquet(io.BytesIO(raw))
    # Default: try Excel
    try:
        return pd.read_excel(io.BytesIO(raw), engine="openpyxl")
    except Exception:
        return pd.read_parquet(io.BytesIO(raw))


def _parse_file(path, fmt: Optional[str] = None) -> pd.DataFrame:
    """Read a DataFrame from a file path, inferring format from extension."""
    from pathlib import Path

    suffix = Path(path).suffix.lower()
    fmt = fmt or suffix.lstrip(".")

    readers = {
        "csv": lambda: pd.read_csv(path),
        "tsv": lambda: pd.read_csv(path, sep="\t"),
        "json": lambda: pd.read_json(path),
        "xlsx": lambda: pd.read_excel(path, engine="openpyxl"),
        "xls": lambda: pd.read_excel(path),
        "parquet": lambda: pd.read_parquet(path),
    }
    reader = readers.get(fmt)
    if reader:
        return reader()

    # Fallback: try CSV
    return pd.read_csv(path)
