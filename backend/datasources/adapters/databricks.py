"""Live preview adapter for Databricks SQL relations."""

from __future__ import annotations

from typing import Any, Iterator, List, Optional

import pandas as pd

from datasources.databricks import (
    DatabricksLoadError,
    open_connection,
    run_databricks_query,
    stream_databricks_query,
)
from datasources.warehouse import quote_ident

from .base import (
    EXPORT_BATCH_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    sql_type_kind,
)
from .warehouse import WarehouseTableAdapter


class DatabricksTableAdapter(WarehouseTableAdapter):
    dialect = "databricks"
    placeholder = "?"
    ident_quote = "`"
    string_type = "STRING"
    like_operator = "ILIKE"
    fold_like = False
    # Databricks SQL rejects an explicit ESCAPE clause; retain the escaped
    # bound pattern and rely on its default backslash semantics.
    escape_clause = ""

    def _open_session(self):
        return open_connection(self.connection_config, socket_timeout=8)

    def _run(self, statement: str, parameters: tuple[Any, ...] = ()):
        try:
            return run_databricks_query(
                self.connection_config, statement, parameters, **self._session_kwargs()
            )
        except DatabricksLoadError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                raise AdapterTimeout("Query timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Full scan through one statement, fetched in chunks.

        Databricks materializes the result set once and we page the cursor over
        it, so the export is a single consistent snapshot of the relation.
        """
        names = [column.name for column in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(quote_ident(name, self.ident_quote) for name in keep)
        statement = f"SELECT {select_list} FROM {self.source_ref}"
        if max_rows is not None:
            statement += f" LIMIT {int(max_rows)}"
        try:
            for chunk_names, rows in stream_databricks_query(
                self.connection_config, statement, batch_rows=int(batch_rows)
            ):
                yield pd.DataFrame.from_records(rows, columns=chunk_names or keep)
        except DatabricksLoadError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def _load_table_schema(self) -> List[AdapterColumn]:
        catalog = self.database or self.connection_config.get("catalog")
        if not catalog:
            raise AdapterError("Databricks catalog is required for schema discovery.")
        statement = (
            f"SELECT column_name, data_type FROM {quote_ident(str(catalog), '`')}."
            "information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position"
        )
        _, rows = self._run(statement, (self.db_schema, self.table))
        return [
            AdapterColumn(name=str(row[0]), db_type=str(row[1]), kind=sql_type_kind(str(row[1])))
            for row in rows
        ]
