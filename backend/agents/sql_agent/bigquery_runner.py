"""Scratch-dataset SQL runner for BigQuery.

Working copies live in a Stych-owned dataset (default ``stych_temp``) inside the
customer's own project. The source table is never mutated.

Two things make BigQuery unlike the other source-side runners:

- **Datasets are located.** A query cannot read a table in one location and write
  to another, so the scratch dataset must be created in the *same* location as
  the source it copies. There is no portable default; ``US`` is only correct for
  sources that happen to live there. The location is resolved from the source
  dataset and cached on the runner.
- **No cross-statement transaction.** Statements are submitted as independent
  jobs, so a failure part-way leaves earlier statements applied. Writes are
  confined to the scratch dataset, so the blast radius is a scratch table.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from agents.sql_agent.runner import SQLRunnerError
from agents.sql_agent.warehouse_base import (
    WRITE_TIMEOUT_SECONDS,
    WarehouseSQLRunner,
    validated_scratch_schema,
)
from datasources.bigquery import (
    BigQueryLoadError,
    _client,
    resolved_project,
    run_bigquery_query,
)
from datasources.sql_dialect import get_dialect

logger = logging.getLogger(__name__)

DEFAULT_SCRATCH_DATASET = "stych_temp"
# Dataset ids are letters, digits, and underscores only.
_DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


def scratch_location(config: Mapping[str, Any]) -> str:
    """Scratch dataset id, with the connect-time default."""
    return validated_scratch_schema(
        config,
        default=DEFAULT_SCRATCH_DATASET,
        pattern=_DATASET_RE,
        label="BigQuery scratch dataset",
        hint="use letters, digits, and underscores",
    )


class BigQuerySQLRunner(WarehouseSQLRunner):
    """Clone source relations into a scratch dataset and run SQL there."""

    engine_label = "BigQuery"

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        self.connection_config = cfg
        self.project = resolved_project(cfg)
        if not self.project:
            raise SQLRunnerError(
                "BigQuery project is required for the scratch working copy — reconnect the table."
            )
        self._temp_dataset = scratch_location(cfg)
        self._temp_schema = self._temp_dataset
        self._dialect = get_dialect("bigquery")
        self._location: Optional[str] = str(cfg.get("scratch_location") or "") or None
        self._client_handle = None

    @classmethod
    def _apply_meta_overrides(cls, meta: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
        out = dict(cfg)
        for key in ("scratch_schema", "scratch_location"):
            if meta.get(key):
                out[key] = meta[key]
        if meta.get("schema"):
            out.setdefault("source_dataset", meta["schema"])
        return out

    # -- identity -----------------------------------------------------------

    @property
    def credential_key(self) -> str:
        return f"{self.project}|{self._temp_dataset}"

    def quote_relation(self, schema: str, table: str) -> str:
        return self._dialect.relation(schema, table, database=self.project)

    # -- execution ----------------------------------------------------------

    def _client(self):
        if self._client_handle is None:
            self._client_handle = _client(self.connection_config)
        return self._client_handle

    def close(self) -> None:
        """Release the gRPC transport and thread pool held by the client.

        Runners are built per tool call rather than per session, so there is
        no session-teardown hook; ``__del__`` covers the common path and
        callers that hold one for longer close it explicitly.
        """
        # ``__init__`` may raise before the handle exists.
        handle = getattr(self, "_client_handle", None)
        self._client_handle = None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:  # noqa: BLE001 — best-effort release
            pass

    def __del__(self) -> None:
        self.close()

    def _execute(self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS) -> None:
        """Run one DDL/DML job. BigQuery has no per-connection transaction."""
        try:
            job = self._client().query(sql)
            job.result(timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 — surfaced as a runner error
            raise SQLRunnerError(str(exc)) from exc

    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        try:
            names, rows = run_bigquery_query(
                self.connection_config,
                sql,
                parameters,
                timeout_seconds=timeout_seconds,
                client=self._client(),
            )
        except BigQueryLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return pd.DataFrame.from_records(rows, columns=names)

    # -- location -----------------------------------------------------------

    def resolve_location(self, source_dataset: Optional[str] = None) -> Optional[str]:
        """Location the scratch dataset must share with its source.

        Returns None when it cannot be determined, which means "let BigQuery
        choose" — correct only when no source dataset was named.
        """
        if self._location:
            return self._location
        dataset = source_dataset or self.connection_config.get("source_dataset")
        if not dataset:
            return None
        try:
            info = self._client().get_dataset(f"{self.project}.{dataset}")
        except Exception as exc:  # noqa: BLE001 — fall back to BigQuery's default
            logger.warning("Could not read location of dataset %s: %s", dataset, exc)
            return None
        self._location = info.location
        return self._location

    def ensure_temp_schema(self, *, source_dataset: Optional[str] = None) -> None:
        """Create the scratch dataset, co-located with the source."""
        from google.cloud import bigquery  # noqa: PLC0415

        ref = bigquery.Dataset(f"{self.project}.{self._temp_dataset}")
        location = self.resolve_location(source_dataset)
        if location:
            ref.location = location
        try:
            self._client().create_dataset(ref, exists_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise SQLRunnerError(
                f"Cannot create the scratch dataset {self.project}.{self._temp_dataset}. "
                f"Grant BigQuery Data Editor on the project. ({exc})"
            ) from exc

    def _ensure_scratch_for_copy(self, source_schema: str) -> None:
        self.ensure_temp_schema(source_dataset=source_schema)

    # -- introspection ------------------------------------------------------

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        frame = self._execute_read_df(
            f"SELECT table_name FROM "
            f"{self._dialect.quote(self.project)}.{self._dialect.quote(self._temp_dataset)}"
            ".INFORMATION_SCHEMA.TABLES"
        )
        if frame.empty:
            return []
        return [name for name in frame["table_name"].astype(str) if name.startswith(prefix)]

    def table_exists(self, schema: str, table: str) -> bool:
        try:
            self._client().get_table(f"{self.project}.{schema}.{table}")
            return True
        except Exception:  # noqa: BLE001 — absence is the answer, not an error
            return False

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        # The table name is a bound parameter, never spliced into the literal.
        frame = self._execute_read_df(
            f"SELECT column_name, data_type, is_nullable FROM "
            f"{self._dialect.quote(self.project)}.{self._dialect.quote(schema)}"
            ".INFORMATION_SCHEMA.COLUMNS WHERE table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [
            {
                "column_name": str(r.column_name),
                "data_type": str(r.data_type),
                # Callers (analysis, cleaning) index this key; omitting it was a
                # KeyError only on the engines that left it out.
                "is_nullable": str(getattr(r, "is_nullable", "YES")),
            }
            for r in frame.itertuples()
        ]
