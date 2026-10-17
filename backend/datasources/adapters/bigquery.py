"""Live preview adapter for BigQuery relations."""

from __future__ import annotations

from typing import Any, Iterator, List, Optional

import pandas as pd

from datasources.bigquery import (
    BigQueryLoadError,
    open_client,
    resolved_project,
    run_bigquery_query,
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


class BigQueryTableAdapter(WarehouseTableAdapter):
    dialect = "bigquery"
    placeholder = "?"
    ident_quote = "`"
    string_type = "STRING"
    like_operator = "LIKE"
    fold_like = True
    # BigQuery Standard SQL rejects an explicit ESCAPE clause; backslash
    # escaping remains in the bound pattern.
    escape_clause = ""

    def _open_session(self):
        return open_client(self.connection_config)

    def _session_kwargs(self) -> dict[str, Any]:
        if self._session is None:
            return {}
        return {"client": self._session}

    def _run(self, statement: str, parameters: tuple[Any, ...] = ()):
        try:
            return run_bigquery_query(
                self.connection_config, statement, parameters, **self._session_kwargs()
            )
        except BigQueryLoadError as exc:
            lowered = str(exc).lower()
            if "timeout" in lowered or "timed out" in lowered or "job exceeded" in lowered:
                raise AdapterTimeout("Query timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Full scan over one query job, pulled a page at a time.

        The job materializes its result once and the client pages that result,
        so the export is a single consistent snapshot of the relation.
        """
        names = [column.name for column in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(quote_ident(name, self.ident_quote) for name in keep)
        statement = f"SELECT {select_list} FROM {self.source_ref}"
        if max_rows is not None:
            statement += f" LIMIT {int(max_rows)}"
        try:
            client = open_client(self.connection_config)
            job = client.query(statement)
            # Iterate pages of rows rather than to_dataframe_iterable(), which
            # pulls in the optional db-dtypes package; the rest of this adapter
            # already builds frames from row tuples.
            for page in job.result(page_size=int(batch_rows)).pages:
                rows = [tuple(row.values()) for row in page]
                if rows:
                    yield pd.DataFrame.from_records(rows, columns=keep)
        except BigQueryLoadError as exc:
            raise AdapterError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — surfaced as an adapter error
            lowered = str(exc).lower()
            if "timeout" in lowered or "timed out" in lowered:
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(exc)) from exc

    def _load_table_schema(self) -> List[AdapterColumn]:
        project = self.database or resolved_project(self.connection_config)
        statement = (
            f"SELECT column_name, data_type FROM {quote_ident(project, '`')}."
            f"{quote_ident(self.db_schema, '`')}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE table_name = ? ORDER BY ordinal_position"
        )
        _, rows = self._run(statement, (self.table,))
        return [
            AdapterColumn(name=str(row[0]), db_type=str(row[1]), kind=sql_type_kind(str(row[1])))
            for row in rows
        ]
