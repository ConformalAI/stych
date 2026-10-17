"""Live preview adapter for Amazon Redshift relations."""

from __future__ import annotations

from typing import Any, Iterator, List, Optional

import pandas as pd

from datasources.redshift import (
    RedshiftLoadError,
    open_connection,
    run_redshift_query,
    stream_redshift_query,
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


class RedshiftTableAdapter(WarehouseTableAdapter):
    dialect = "redshift"
    placeholder = "%s"
    # Redshift consumes backslashes while parsing string literals, so the SQL
    # text needs two to express the single-character LIKE escape marker.
    escape_clause = r" ESCAPE '\\'"

    def _open_session(self):
        return open_connection(self.connection_config)

    def _run(self, statement: str, parameters: tuple[Any, ...] = ()):
        try:
            return run_redshift_query(
                self.connection_config, statement, parameters, **self._session_kwargs()
            )
        except RedshiftLoadError as exc:
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

        A single execute inside one session sees one snapshot, so the export is
        internally consistent even while the table is written.
        """
        names = [column.name for column in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(quote_ident(name, self.ident_quote) for name in keep)
        statement = f"SELECT {select_list} FROM {self.source_ref}"
        if max_rows is not None:
            statement += f" LIMIT {int(max_rows)}"
        try:
            for chunk_names, rows in stream_redshift_query(
                self.connection_config, statement, batch_rows=int(batch_rows)
            ):
                yield pd.DataFrame.from_records(rows, columns=chunk_names or keep)
        except RedshiftLoadError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def _load_table_schema(self) -> List[AdapterColumn]:
        statement = (
            "SELECT column_name, data_type FROM svv_all_columns "
            "WHERE database_name = %s AND schema_name = %s AND table_name = %s "
            "ORDER BY ordinal_position"
        )
        database = self.connection_config.get("database")
        _, rows = self._run(statement, (database, self.db_schema, self.table))
        return [
            AdapterColumn(name=str(row[0]), db_type=str(row[1]), kind=sql_type_kind(str(row[1])))
            for row in rows
        ]
