import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SQLCleaningService:
    def __init__(self, agent: Any):
        self.agent = agent

    @staticmethod
    def _bump_temp_generation(state: Any) -> None:
        """Invalidate preview/diff caches keyed on the working table's content."""
        meta = state.data_context.metadata
        meta["temp_table_generation"] = int(meta.get("temp_table_generation") or 0) + 1

    @staticmethod
    def split_sql_statements(sql_script: str) -> List[str]:
        statements: List[str] = []
        current: List[str] = []
        in_single = False
        in_double = False
        prev = ""
        for ch in sql_script:
            if ch == "'" and not in_double and prev != "\\":
                in_single = not in_single
            elif ch == '"' and not in_single and prev != "\\":
                in_double = not in_double
            if ch == ";" and not in_single and not in_double:
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
            else:
                current.append(ch)
            prev = ch
        tail = "".join(current).strip()
        if tail:
            statements.append(tail)
        return statements

    async def run_cleaning_sql(
        self,
        state: Any,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        temp_schema, temp_table = await self.agent.ensure_temp_table(state)
        runner = self.agent._runner_for(state)

        def _do_cleaning():
            before_rows = runner.get_row_count(temp_schema, temp_table)
            before_cols = len(runner.get_table_columns(temp_schema, temp_table))
            applied = []
            rows_removed = 0
            missing_before = 0
            executed_sql: List[str] = []

            for op in operations:
                if op.get("type") == "remove_duplicates":
                    removed = runner.delete_duplicates_keep_first(
                        temp_schema,
                        temp_table,
                        subset_columns=op.get("columns"),
                        sql_log=executed_sql,
                    )
                    rows_removed += removed
                    applied.append({**op, "removed_rows": removed})
                elif op.get("type") == "handle_missing":
                    cols = runner.get_table_columns(temp_schema, temp_table)
                    filled_cols = []
                    for c in cols:
                        col_name = c["column_name"]
                        quoted_col = f'"{col_name}"'
                        quoted_table = runner.quote_relation(temp_schema, temp_table)
                        null_count_rows = runner._execute_read(
                            f"SELECT COUNT(*) AS c FROM {quoted_table} WHERE {quoted_col} IS NULL"
                        )
                        n = int(null_count_rows[0]["c"]) if null_count_rows else 0
                        if n == 0:
                            continue
                        missing_before += n
                        dtype = (c.get("data_type") or "").upper()
                        if dtype in (
                            "INTEGER",
                            "BIGINT",
                            "SMALLINT",
                            "NUMERIC",
                            "DECIMAL",
                            "REAL",
                            "DOUBLE PRECISION",
                            "FLOAT",
                        ):
                            sql = (
                                f"UPDATE {quoted_table} t SET {quoted_col} = sub.med FROM "
                                f"(SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {quoted_col}) AS med "
                                f"FROM {quoted_table} WHERE {quoted_col} IS NOT NULL) sub "
                                f"WHERE t.{quoted_col} IS NULL"
                            )
                            executed_sql.append(sql)
                            runner.execute_statements_transaction([sql])
                        else:
                            sql = (
                                f"UPDATE {quoted_table} t SET {quoted_col} = sub.v FROM "
                                f"(SELECT {quoted_col} AS v FROM {quoted_table} "
                                f"WHERE {quoted_col} IS NOT NULL LIMIT 1) sub "
                                f"WHERE t.{quoted_col} IS NULL"
                            )
                            executed_sql.append(sql)
                            runner.execute_statements_transaction([sql])
                        filled_cols.append(f"{col_name}: {n}")
                    if filled_cols:
                        applied.append(
                            {
                                "type": "handle_missing",
                                "strategy": op.get("strategy", "auto"),
                                "column_operations": filled_cols,
                            }
                        )
                elif op.get("type") == "custom_sql":
                    raw_sql = str(op.get("sql") or "").strip()
                    if not raw_sql:
                        continue
                    quoted_table = runner.quote_relation(temp_schema, temp_table)
                    sql = raw_sql.replace("{table}", quoted_table)
                    executed_sql.append(sql)
                    runner.execute_statements_transaction([sql])
                    applied.append({"type": "custom_sql"})

            after_rows = runner.get_row_count(temp_schema, temp_table)
            return {
                "original_shape": (before_rows, before_cols),
                "cleaned_shape": (after_rows, before_cols),
                "rows_removed": rows_removed,
                "missing_before": missing_before,
                "missing_after": 0,
                "missing_resolved": missing_before,
                "operations": applied,
                "executed_sql": executed_sql,
                "data": None,
                "code": None,
                "sql_mode": True,
                "temp_schema": temp_schema,
                "temp_table": temp_table,
                "temp_table_name": f"{temp_schema}.{temp_table}",
            }

        result = await asyncio.to_thread(_do_cleaning)
        self._bump_temp_generation(state)
        return result

    async def run_cleaning_sql_script(
        self,
        state: Any,
        sql_script: str,
    ) -> Dict[str, Any]:
        temp_schema, temp_table = await self.agent.ensure_temp_table(state)
        runner = self.agent._runner_for(state)

        def _do_cleaning():
            before_rows = runner.get_row_count(temp_schema, temp_table)
            before_cols = len(runner.get_table_columns(temp_schema, temp_table))
            executed_sql: List[str] = []

            quoted_table = runner.quote_relation(temp_schema, temp_table)
            existing_cols = {
                c["column_name"] for c in runner.get_table_columns(temp_schema, temp_table)
            }
            statements = self.split_sql_statements(sql_script)
            exec_statements: List[str] = []

            for stmt in statements:
                upper = stmt.strip().upper()
                if not upper.startswith("UPDATE"):
                    continue
                try:
                    import re

                    set_match = re.search(
                        r"\\bSET\\b(.+?)(\\bWHERE\\b|$)",
                        stmt,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if not set_match:
                        continue
                    set_clause = set_match.group(1)
                    targets = re.findall(r"(\"?[A-Za-z_][\\w$]*\"?)\\s*=", set_clause)
                    for raw_col in targets:
                        col = raw_col.strip('"')
                        if col in existing_cols:
                            continue
                        alter_sql = (
                            f'ALTER TABLE {quoted_table} ADD COLUMN IF NOT EXISTS "{col}" TEXT'
                        )
                        executed_sql.append(alter_sql)
                        exec_statements.append(alter_sql)
                        existing_cols.add(col)
                except Exception:
                    pass

            for stmt in statements:
                rewritten = stmt.replace("{table}", quoted_table)
                executed_sql.append(rewritten)
                exec_statements.append(rewritten)

            try:
                runner.execute_statements_transaction(exec_statements, allowed_schema=temp_schema)
            except Exception as exc:
                return {
                    "error": str(exc),
                    "failed_sql": exec_statements[-1] if exec_statements else None,
                }

            after_rows = runner.get_row_count(temp_schema, temp_table)
            missing_after = 0
            view_name = f"cleaned_{state.session_id.replace('-', '_')}"
            runner.create_or_replace_view("public", view_name, temp_schema, temp_table)
            return {
                "original_shape": (before_rows, before_cols),
                "cleaned_shape": (after_rows, before_cols),
                "rows_removed": before_rows - after_rows,
                "missing_before": 0,
                "missing_after": missing_after,
                "missing_resolved": 0,
                "operations": ["SQL script executed"],
                "executed_sql": executed_sql,
                "data": None,
                "code": None,
                "sql_mode": True,
                "temp_schema": temp_schema,
                "temp_table": temp_table,
                "temp_table_name": f"{temp_schema}.{temp_table}",
                "view_name": f"public.{view_name}",
            }

        result = await asyncio.to_thread(_do_cleaning)
        if not result.get("error"):
            self._bump_temp_generation(state)
        return result

    async def get_temp_preview(self, state: Any, limit: int = 10) -> Dict[str, Any]:
        temp_schema, temp_table = await self.agent.ensure_temp_table(state)
        runner = self.agent._runner_for(state)

        def _load():
            quoted = runner.quote_relation(temp_schema, temp_table)
            df = runner._execute_read_df(f"SELECT * FROM {quoted} LIMIT {limit}")
            rows = df.to_dict(orient="records") if df is not None else []
            columns = list(df.columns) if df is not None else []
            missing_total = int(df.isnull().sum().sum()) if df is not None else 0
            return {
                "temp_schema": temp_schema,
                "temp_table": temp_table,
                "columns": columns,
                "rows": rows,
                "missing_total": missing_total,
            }

        return await asyncio.to_thread(_load)

    async def apply_cleaning_to_copy_table(self, state: Any, sql_script: str) -> Dict[str, Any]:
        ctx = state.data_context
        if not ctx or not ctx.is_sql_datasource():
            return {"error": "SQL datasource not available."}
        if not ctx.metadata.get("table"):
            return {"error": "Cannot apply changes for custom SQL query sources."}

        if ctx.metadata.get("source") == "databricks":
            return {"error": "Writing back to the Databricks source table is not supported."}
        connection_url = ctx.metadata["connection_url"]
        runner = self.agent._runner(connection_url)
        source_schema = ctx.metadata.get("schema", "public")
        source_table = ctx.metadata.get("table")
        if source_table.startswith("copy_"):
            copy_table = source_table
        else:
            copy_table = f"copy_{source_table}"

        def _apply():
            if not runner.table_exists(source_schema, copy_table):
                create_sql = (
                    f'CREATE TABLE "{source_schema}"."{copy_table}" '
                    f'(LIKE "{source_schema}"."{source_table}" INCLUDING ALL)'
                )
                insert_sql = (
                    f'INSERT INTO "{source_schema}"."{copy_table}" '
                    f'SELECT * FROM "{source_schema}"."{source_table}"'
                )
                logger.info(
                    "Creating copy table %s.%s from %s.%s",
                    source_schema,
                    copy_table,
                    source_schema,
                    source_table,
                )
                runner._execute(create_sql)
                runner._execute(insert_sql)

            target = f'"{source_schema}"."{copy_table}"'
            applied_sql: List[str] = []
            statements = self.split_sql_statements(sql_script or "")
            if not statements:
                return {"error": "No SQL script available to apply."}
            exec_statements: List[str] = []
            for sql in statements:
                if "{table}" not in sql:
                    return {"error": "SQL script missing {table} placeholder."}
                rewritten = sql.replace("{table}", target)
                applied_sql.append(rewritten)
                exec_statements.append(rewritten)
            try:
                runner.execute_statements_transaction(exec_statements, allowed_schema=source_schema)
            except Exception as exc:
                return {
                    "error": str(exc),
                    "failed_sql": exec_statements[-1] if exec_statements else None,
                }
            logger.info(
                "Applied %d SQL statements to copy table %s.%s",
                len(applied_sql),
                source_schema,
                copy_table,
            )
            view_name = f"cleaned_{state.session_id.replace('-', '_')}"
            runner.create_or_replace_view("public", view_name, source_schema, copy_table)

            copy_exists = runner.table_exists(source_schema, copy_table)
            return {
                "copy_table_name": f"{source_schema}.{copy_table}",
                "applied_sql": "\n\n".join(applied_sql),
                "copy_exists": copy_exists,
                "view_name": f"public.{view_name}",
            }

        return await asyncio.to_thread(_apply)
