"""
SQL Agent: creates temp tables and runs cleaning/analysis via SQL.
Used when session data comes from Postgres or Databricks so we never load
the full table into memory.
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from datasources.sql_readonly import (
    ReadOnlySQLError,
    parse_read_only_statements,
    require_attached_asset_placeholder,
)
from llm.infer import LLMRouter

from ..state import AgentState
from . import working_copies as wc
from .cleaning import SQLCleaningService
from .protocols import runner_for
from .runner import PostgresSQLRunner, SQLRunnerError, dataset_table_prefix

logger = logging.getLogger(__name__)

TEMP_SCHEMA = "stych_temp"


class SQLAgent:
    """
    Subagent that operates on a database via working copies in a scratch schema.
    - Maintains a registry of named working copies (branch/switch/drop/list);
      ``ensure_temp_table`` resolves the *active* copy, creating one on demand
    - Runs cleaning (dedup, fill nulls) as SQL on the active copy
    - Runs analysis by generating SELECT with LLM and executing on the active copy
    """

    def __init__(self, llm_router: LLMRouter):
        self.llm_router = llm_router
        self.cleaning = SQLCleaningService(self)
        # Physical tables verified to exist this process: {(credential_key, table)}.
        # A restored session's refs are checked once instead of being wiped.
        self._verified: set = set()
        # Dataset owners whose scratch prefix was GC-scanned this process.
        self._gc_done: set = set()

    def _runner(self, connection_url: str) -> PostgresSQLRunner:
        return PostgresSQLRunner(connection_url)

    def _runner_for(self, state: AgentState):
        meta = state.data_context.metadata
        if (state.data_context.metadata or {}).get("source") == "mongodb":
            from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415

            runner = MongoScratchRunner.from_meta(meta)
        else:
            runner = runner_for(meta)
        if wc.bind_scratch_schema(meta, runner.temp_schema):
            self._mark_dirty(state)
        return runner

    async def resolve_read_target(self, state: AgentState) -> Dict[str, Any]:
        """Return the live attached source, or the active working copy when one exists.

        Read-only analysis must not create scratch data. A working copy is selected
        only after a cleaning/mutation operation has explicitly created one.
        """
        meta = self._sql_meta(state)
        if meta.get("source") == "mongodb":
            raise SQLRunnerError("MongoDB uses its native pipeline read path")
        runner = self._runner_for(state)
        wc.adopt_legacy(meta)
        active = wc.active_copy(meta)
        if active:
            exists = await asyncio.to_thread(
                runner.table_exists, runner.temp_schema, active["table"]
            )
            if exists:
                return {
                    "ref": runner.quote_relation(runner.temp_schema, active["table"]),
                    "schema": runner.temp_schema,
                    "table": active["table"],
                    "working_copy": True,
                }
            wc.remove_copy(meta, active["name"])

        custom_query = meta.get("custom_query")
        if custom_query:
            try:
                statements = parse_read_only_statements(
                    custom_query, allowed_starts=frozenset({"SELECT", "WITH"})
                )
            except ReadOnlySQLError as exc:
                raise SQLRunnerError("Attached custom query is not read-only") from exc
            if len(statements) != 1:
                raise SQLRunnerError("Attached custom query must contain one statement")
            return {
                "ref": f"({statements[0]}) AS _stych_source",
                "schema": None,
                "table": None,
                "working_copy": False,
            }
        schema = str(meta.get("schema") or "public")
        table = meta.get("table")
        if not table:
            raise SQLRunnerError("The attached source table is unavailable")
        if meta.get("source") == "databricks":
            ref = runner._source_ref(schema, str(table))
        else:
            ref = runner.quote_relation(schema, str(table))
        return {"ref": ref, "schema": schema, "table": str(table), "working_copy": False}

    async def describe_read_target(self, state: AgentState) -> Dict[str, Any]:
        target = await self.resolve_read_target(state)
        runner = self._runner_for(state)

        def _describe() -> tuple[List[Dict[str, Any]], int]:
            empty = runner.run_sql_read_only(f"SELECT * FROM {target['ref']} WHERE 1 = 0")
            columns = [
                {
                    "column_name": str(name),
                    "data_type": str(dtype),
                    "is_nullable": "UNKNOWN",
                }
                for name, dtype in empty.dtypes.items()
                if not wc.is_internal_column(str(name))
            ]
            count = runner.run_sql_read_only(
                f"SELECT COUNT(*) AS __stych_count FROM {target['ref']}"
            )
            return columns, int(count.iloc[0, 0])

        columns, row_count = await asyncio.to_thread(_describe)
        return {**target, "columns": columns, "row_count": row_count}

    async def run_attached_sql_frames(self, state: AgentState, sql: str) -> List[Any]:
        """Execute an agent read after binding its only relation to this attachment."""
        require_attached_asset_placeholder(sql)
        target = await self.resolve_read_target(state)
        return await self.run_sql_frames(
            state,
            sql.replace("{table}", target["ref"]),
            ensure_working_copy=False,
        )

    @staticmethod
    def _sql_meta(state: AgentState) -> Dict[str, Any]:
        ctx = state.data_context
        if not ctx or not ctx.is_sql_datasource():
            raise SQLRunnerError("Data context is not a SQL datasource")
        return ctx.metadata

    @staticmethod
    def _mark_dirty(state: AgentState) -> None:
        """Registry changes must reach the snapshot store, not just memory."""
        try:
            from ..state import state_manager  # noqa: PLC0415 — avoid import cycle

            state_manager.mark_dirty(state.session_id)
        except Exception:  # noqa: BLE001 — snapshotting is best-effort
            pass

    def _physical_name(self, state: AgentState) -> str:
        return (
            f"{dataset_table_prefix(state.session_id, state.data_context.file_id)}"
            f"{uuid.uuid4().hex[:8]}"
        )

    def _table_exists(self, runner, table: str) -> bool:
        key = (runner.credential_key, table)
        if key in self._verified:
            return True
        if runner.table_exists(runner.temp_schema, table):
            self._verified.add(key)
            return True
        return False

    def _gc_orphans(self, state: AgentState, runner, meta: Dict) -> None:
        """Drop this dataset's unregistered scratch tables.

        Several attachments in one session may share the same warehouse and
        scratch schema.  Ownership therefore includes ``file_id``; scanning the
        older session-wide prefix could delete a sibling dataset's live copy.
        Legacy session-prefixed orphans are left for whole-session retirement.
        """
        owner = (runner.credential_key, state.session_id, state.data_context.file_id)
        if owner in self._gc_done:
            return
        self._gc_done.add(owner)
        try:
            prefix = dataset_table_prefix(state.session_id, state.data_context.file_id)
            registered = {c["table"] for c in wc.list_copies(meta)}
            for table in runner.list_temp_tables_with_prefix(prefix):
                if table not in registered:
                    runner.drop_temp_table(table)
                    logger.info(
                        "GC: dropped orphan working copy %s.%s (session %s)",
                        runner.temp_schema,
                        table,
                        state.session_id,
                    )
        except Exception as exc:  # noqa: BLE001 — GC must never fail the request
            logger.warning("GC scan failed for session %s: %s", state.session_id, exc)

    async def ensure_temp_table(self, state: AgentState) -> Tuple[str, str]:
        """Resolve the active working copy, creating one from source on demand.

        A restored session keeps its registry: the active table is verified to
        physically exist (once per process) and only recreated when it is
        actually gone — never wiped defensively.
        """
        meta = self._sql_meta(state)
        runner = self._runner_for(state)
        wc.adopt_legacy(meta)

        active = wc.active_copy(meta)
        if active:

            def _check():
                self._gc_orphans(state, runner, meta)
                return self._table_exists(runner, active["table"])

            if await asyncio.to_thread(_check):
                return (runner.temp_schema, active["table"])
            # Physically missing (dropped outside the registry) — recreate.
            logger.warning(
                "Working copy %r (table %s) is missing for session %s — recreating from source",
                active["name"],
                active["table"],
                state.session_id,
            )
            wc.remove_copy(meta, active["name"])

        entry = await self.create_working_copy(state, name=None, parent=wc.SOURCE_PARENT)
        return (runner.temp_schema, entry["table"])

    async def create_working_copy(
        self,
        state: AgentState,
        name: Optional[str] = None,
        parent: str = wc.SOURCE_PARENT,
    ) -> Dict[str, Any]:
        """Create a new working copy (from the source table or from another
        copy), register it, and make it active. Returns the registry entry."""
        meta = self._sql_meta(state)
        runner = self._runner_for(state)
        name = wc.validate_name(name) if name else wc.default_name(meta)
        if wc.get_copy(meta, name) is not None:
            raise wc.WorkingCopyError(f"A working copy named {name!r} already exists.")
        physical = self._physical_name(state)

        if parent == wc.SOURCE_PARENT:
            if meta.get("source") == "mongodb":
                schema = meta.get("database")
                table = meta.get("collection") or meta.get("table")
            else:
                schema = meta.get("schema", "public")
                table = meta.get("table")
            custom_query = meta.get("custom_query")
            max_rows = meta.get("max_rows", 0)

            pin = wc.pinned_base(meta)
            pin_physical = None if pin else self._physical_name(state)

            def _create():
                self._gc_orphans(state, runner, meta)
                if pin is None:
                    # Pin an immutable snapshot of the source first, then clone
                    # it. Cloning (rather than reading the source twice) is what
                    # guarantees the pin and the working copy carry identical row
                    # ids — two independent scans could number rows differently.
                    runner.create_temp_table_from_source(
                        session_id=state.session_id,
                        source_schema=schema,
                        source_table=table,
                        custom_query=custom_query,
                        max_rows=max_rows,
                        table_name=pin_physical,
                    )
                    runner.duplicate_temp_table(pin_physical, physical)
                else:
                    runner.duplicate_temp_table(pin["table"], physical)

        else:
            pin_physical = None
            parent_entry = wc.get_copy(meta, parent)
            if parent_entry is None:
                raise wc.WorkingCopyError(f"No working copy named {parent!r} to branch from.")

            def _create():
                runner.duplicate_temp_table(parent_entry["table"], physical)

        await asyncio.to_thread(_create)
        if parent == wc.SOURCE_PARENT and pin_physical is not None:
            wc.register_copy(
                meta,
                name=wc.PINNED_BASE_NAME,
                table=pin_physical,
                parent=wc.SOURCE_PARENT,
                role=wc.BASE_ROLE,
            )
            self._verified.add((runner.credential_key, pin_physical))
        entry = wc.register_copy(meta, name=name, table=physical, parent=parent)
        meta.setdefault("temp_table_generation", 0)
        self._verified.add((runner.credential_key, physical))
        self.cleaning._bump_temp_generation(state)
        self._mark_dirty(state)
        logger.info(
            "Created working copy %r (%s.%s, parent=%s) for session %s",
            name,
            runner.temp_schema,
            physical,
            parent,
            state.session_id,
        )
        return entry

    async def switch_working_copy(self, state: AgentState, name: str) -> Dict[str, Any]:
        """Make an existing copy the active one (what agents and the preview see)."""
        meta = self._sql_meta(state)
        entry = wc.set_active(meta, name)
        self.cleaning._bump_temp_generation(state)
        self._mark_dirty(state)
        return entry

    async def drop_working_copy(self, state: AgentState, name: str) -> Dict[str, Any]:
        """Drop a copy's physical table and remove it from the registry."""
        meta = self._sql_meta(state)
        entry = wc.get_copy(meta, name)
        if entry is None:
            raise wc.WorkingCopyError(f"No working copy named {name!r}.")
        if wc.is_base(entry):
            raise wc.WorkingCopyError(
                "The pinned base snapshot is read-only and cannot be dropped. "
                "It is removed automatically when the dataset is retired."
            )
        runner = self._runner_for(state)
        await asyncio.to_thread(runner.drop_temp_table, entry["table"])
        self._verified.discard((runner.credential_key, entry["table"]))
        wc.remove_copy(meta, name)
        self.cleaning._bump_temp_generation(state)
        self._mark_dirty(state)
        logger.info("Dropped working copy %r for session %s", name, state.session_id)
        return entry

    async def list_working_copies(self, state: AgentState) -> List[Dict[str, Any]]:
        """Registry entries plus row counts, active copy flagged."""
        meta = self._sql_meta(state)
        wc.adopt_legacy(meta)
        copies = wc.list_copies(meta)
        active_name = meta.get(wc.ACTIVE_KEY)
        runner = self._runner_for(state)

        def _counts() -> List[Dict[str, Any]]:
            out = []
            for c in copies:
                try:
                    rows = runner.get_row_count(runner.temp_schema, c["table"])
                except Exception:  # noqa: BLE001 — a missing table still gets listed
                    rows = None
                out.append({**c, "rows": rows, "active": c["name"] == active_name})
            return out

        return await asyncio.to_thread(_counts)

    async def get_table_columns(self, state: AgentState) -> List[Dict[str, Any]]:
        temp_schema, temp_table = await self.ensure_temp_table(state)
        runner = self._runner_for(state)
        return await asyncio.to_thread(runner.get_table_columns, temp_schema, temp_table)

    async def get_row_count(self, state: AgentState) -> int:
        temp_schema, temp_table = await self.ensure_temp_table(state)
        runner = self._runner_for(state)
        return await asyncio.to_thread(runner.get_row_count, temp_schema, temp_table)

    async def run_sql_frames(
        self, state: AgentState, select_sql: str, *, ensure_working_copy: bool = True
    ) -> List[Any]:
        """Run each read-only statement and return one DataFrame per statement."""
        if ensure_working_copy:
            await self.ensure_temp_table(state)
        runner = self._runner_for(state)
        try:
            statements = parse_read_only_statements(select_sql)
        except ReadOnlySQLError as exc:
            raise SQLRunnerError(str(exc)) from exc

        def _run() -> List[Any]:
            return [runner.run_sql_read_only(stmt) for stmt in statements]

        return await asyncio.to_thread(_run)

    async def run_sql(
        self, state: AgentState, select_sql: str, *, ensure_working_copy: bool = True
    ) -> List[Dict[str, Any]]:
        frames = await self.run_sql_frames(
            state, select_sql, ensure_working_copy=ensure_working_copy
        )
        if not frames:
            return []
        df = frames[-1]
        return df.to_dict(orient="records") if df is not None and not df.empty else []

    async def run_cleaning_sql(
        self,
        state: AgentState,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return await self.cleaning.run_cleaning_sql(state, operations)

    async def run_cleaning_sql_script(
        self,
        state: AgentState,
        sql_script: str,
    ) -> Dict[str, Any]:
        return await self.cleaning.run_cleaning_sql_script(state, sql_script)

    async def get_temp_preview(self, state: AgentState, limit: int = 10) -> Dict[str, Any]:
        return await self.cleaning.get_temp_preview(state, limit=limit)

    async def apply_cleaning_to_copy_table(
        self, state: AgentState, sql_script: str
    ) -> Dict[str, Any]:
        return await self.cleaning.apply_cleaning_to_copy_table(state, sql_script)
