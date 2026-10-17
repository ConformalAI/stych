"""Live preview adapter for Snowflake relations."""

from __future__ import annotations

from typing import Any, Iterator, List, Optional

import pandas as pd

from datasources.snowflake import (
    SnowflakeLoadError,
    open_connection,
    run_snowflake_query,
    stream_snowflake_query,
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


class SnowflakeTableAdapter(WarehouseTableAdapter):
    dialect = "snowflake"
    placeholder = "?"

    def _open_session(self):
        return open_connection(self.connection_config)

    def _run(self, statement: str, parameters: tuple[Any, ...] = ()):
        try:
            return run_snowflake_query(
                self.connection_config, statement, parameters, **self._session_kwargs()
            )
        except SnowflakeLoadError as exc:
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
        """Full scan over one statement, fetched in chunks.

        Snowflake materializes the result set once and the cursor pages it, so
        the export is a single consistent snapshot of the relation.
        """
        names = [column.name for column in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(quote_ident(name, self.ident_quote) for name in keep)
        statement = f"SELECT {select_list} FROM {self.source_ref}"
        if max_rows is not None:
            statement += f" LIMIT {int(max_rows)}"
        try:
            for chunk_names, rows in stream_snowflake_query(
                self.connection_config, statement, batch_rows=int(batch_rows)
            ):
                yield pd.DataFrame.from_records(rows, columns=chunk_names or keep)
        except SnowflakeLoadError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def _load_table_schema(self) -> List[AdapterColumn]:
        if not self.database:
            raise AdapterError("Snowflake database is required for schema discovery.")
        statement = (
            f"SELECT COLUMN_NAME, DATA_TYPE FROM {quote_ident(self.database)}."
            "INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
        )
        _, rows = self._run(statement, (self.db_schema, self.table))
        return [
            AdapterColumn(name=str(row[0]), db_type=str(row[1]), kind=sql_type_kind(str(row[1])))
            for row in rows
        ]
