"""
Agent state management for conversation and data context.

By default, sessions are cached in-memory and also snapshotted to disk so
conversation/data context survives process restarts.
"""

import asyncio
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from database.connection import SessionLocal
from database.session_store import (
    append_message as db_append_message,
)
from database.session_store import (
    delete_session as db_delete_session,
)
from database.session_store import (
    ensure_session_row,
    get_session_row,
)
from database.session_store import (
    list_messages as db_list_messages,
)

logger = logging.getLogger(__name__)

# Metadata keys holding secrets. On snapshot these are encrypted at rest (so a
# restored SQL session can resume); legacy snapshots stored them as "[REDACTED]".
_SENSITIVE_META_KEYS = frozenset(
    {
        "connection_url",
        "connection_config",
        "password",
        "private_key",
        "private_key_passphrase",
        "api_key",
        "secret",
        "access_key",
        "access_key_id",
        "secret_access_key",
        "session_token",
        "access_token",
        "refresh_token",
        "service_account_json",
        "secret_key",
        "token",
    }
)
# Working-copy references (temp_schema/temp_table/working_copies) DO survive a
# restore: the copies are real tables in the user's database, and wiping the
# refs silently threw away days of work while orphaning the tables. SQLAgent
# verifies the active table still exists on first touch and recreates only
# when it is actually gone.
# Legacy placeholder written by older snapshots before encryption was added.
_REDACTED_PLACEHOLDER = "[REDACTED]"


def _encrypt_sensitive_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *meta* with sensitive values encrypted for storage.

    Falls back to the redaction placeholder if encryption is unavailable so a
    plaintext secret is never written to the snapshot.
    """
    from helpers.secrets_crypto import encrypt_value

    out: Dict[str, Any] = {}
    for k, v in (meta or {}).items():
        if k in _SENSITIVE_META_KEYS and isinstance(v, str) and v:
            enc = encrypt_value(v)
            out[k] = enc if enc is not None else _REDACTED_PLACEHOLDER
        else:
            out[k] = v
    return out


def _restore_sensitive_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Inverse of :func:`_encrypt_sensitive_meta` for restore.

    Decrypts encrypted sensitive values; drops any that are legacy-redacted or
    undecryptable (so a placeholder can never reach ``create_engine``).
    Working-copy references are kept — SQLAgent verifies them on first touch.
    """
    from helpers.secrets_crypto import decrypt_value, is_encrypted

    out: Dict[str, Any] = {}
    for k, v in (meta or {}).items():
        if k in _SENSITIVE_META_KEYS:
            if is_encrypted(v):
                dec = decrypt_value(v)
                if dec is not None:
                    out[k] = dec
                # else: undecryptable → drop (session needs reconnect)
            # legacy "[REDACTED]" or other non-encrypted secret → drop
            continue
        out[k] = v
    return out


def long_term_memory_allowed(state: "AgentState") -> bool:
    """True when this session may recall/retain Hindsight long-term memory."""
    meta = state.metadata or {}
    if meta.get("is_anonymous"):
        return False
    return bool(meta.get("long_term_memory_enabled", True))


@dataclass
class Message:
    """Conversation message."""

    role: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataContext:
    """Context for uploaded data.

    Maintains two dataframe views:
    - ``raw_dataframe``: the original upload, never mutated.
    - ``working_dataframe``: the mutable copy used by analysis / cleaning.

    The ``dataframe`` property is a backward-compatible accessor that reads
    and writes ``working_dataframe`` so existing code continues to work.
    """

    file_id: str
    filename: str
    raw_dataframe: pd.DataFrame
    working_dataframe: pd.DataFrame
    uploaded_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: Any) -> None:
        replacing_working_frame = name == "working_dataframe" and name in self.__dict__
        object.__setattr__(self, name, value)
        if replacing_working_frame:
            metadata = self.__dict__.get("metadata")
            if isinstance(metadata, dict):
                metadata["data_generation"] = int(metadata.get("data_generation") or 0) + 1

    @property
    def dataframe(self) -> pd.DataFrame:
        """
        Backward-compatible alias for current working dataframe.
        """
        return self.working_dataframe

    @dataframe.setter
    def dataframe(self, value: pd.DataFrame):
        self.working_dataframe = value

    def reset_working_to_raw(self):
        """Reset working dataframe to original uploaded dataframe."""
        self.working_dataframe = self.raw_dataframe.copy(deep=True)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of the data."""
        working_df = self.working_dataframe
        raw_df = self.raw_dataframe
        if self.is_remote_source():
            columns = self.metadata.get("source_columns") or []
            names = [
                str(column["name"])
                for column in columns
                if isinstance(column, dict) and column.get("name")
            ]
            return {
                "file_id": self.file_id,
                "filename": self.filename,
                "shape": (int(self.metadata.get("source_row_count") or 0), len(names)),
                "raw_shape": (0, len(names)),
                "working_shape": (0, len(names)),
                "columns": names,
                "dtypes": {
                    str(column["name"]): str(column.get("db_type") or "unknown")
                    for column in columns
                    if isinstance(column, dict) and column.get("name")
                },
                "is_cleaned_copy": bool(self.metadata.get("temp_table")),
                "uploaded_at": self.uploaded_at.isoformat(),
                "source": self.metadata.get("source"),
                "sql_temp_table": self.metadata.get("temp_schema")
                and self.metadata.get("temp_table"),
            }
        out = {
            "file_id": self.file_id,
            "filename": self.filename,
            "shape": working_df.shape,
            "raw_shape": raw_df.shape,
            "working_shape": working_df.shape,
            "columns": list(working_df.columns),
            "dtypes": working_df.dtypes.astype(str).to_dict(),
            "is_cleaned_copy": not raw_df.equals(working_df),
            "uploaded_at": self.uploaded_at.isoformat(),
        }
        if self.is_sql_datasource():
            out["source"] = self.metadata.get("source")
            out["sql_temp_table"] = self.metadata.get("temp_schema") and self.metadata.get(
                "temp_table"
            )
        return out

    def is_sql_datasource(self) -> bool:
        """True when the capability contract selects source-side analysis."""
        from datasources.contracts import Executor, ProductFeature  # noqa: PLC0415
        from datasources.feature_resolver import resolve_feature  # noqa: PLC0415
        from datasources.registry import get_manifest  # noqa: PLC0415

        source = self.metadata.get("source", "upload")
        try:
            analysis = resolve_feature(get_manifest(source), ProductFeature.ANALYZE)
        except KeyError:
            return False
        return analysis.execution_path == Executor.SOURCE and self._has_live_source_credentials()

    def is_remote_source(self) -> bool:
        """True for a metadata-only live database attachment."""
        return self.metadata.get("storage_mode") == "remote_reference"

    def should_use_remote_reference(self) -> bool:
        """True for every connector whose rows remain in the customer source."""
        from datasources.registry import SOURCE_WORKING_COPY_IDS  # noqa: PLC0415

        return self.is_remote_source() or self.metadata.get("source") in SOURCE_WORKING_COPY_IDS

    def convert_to_remote_reference(self) -> None:
        """Discard a legacy sampled frame while retaining its schema metadata."""
        if not self.should_use_remote_reference():
            return
        columns = self.metadata.get("source_columns") or []
        if not columns:
            columns = [
                {"name": str(name), "db_type": str(dtype), "kind": "string"}
                for name, dtype in self.working_dataframe.dtypes.items()
            ]
        self.metadata["source_columns"] = columns
        self.metadata["source_row_count"] = int(self.metadata.get("source_row_count") or 0)
        self.metadata["storage_mode"] = "remote_reference"
        empty = pd.DataFrame(
            columns=[
                str(column["name"])
                for column in columns
                if isinstance(column, dict) and column.get("name")
            ]
        )
        self.raw_dataframe = empty
        self.working_dataframe = empty

    def _has_live_source_credentials(self) -> bool:
        url = str(self.metadata.get("connection_url") or "").strip()
        if url and url != "[REDACTED]" and "://" in url:
            return True
        raw = self.metadata.get("connection_config")
        if not raw or raw == "[REDACTED]":
            return False
        try:
            from datasources.warehouse import decode_connection_config  # noqa: PLC0415

            decode_connection_config(raw)
            return True
        except Exception:  # noqa: BLE001 — missing/redacted creds are not live
            return False

    def get_sql_target(self) -> tuple:
        """
        For SQL datasource: (schema, table) to run queries against.
        Uses temp table if we already created one (after cleaning), else source table/query.
        Returns (None, None) if not SQL datasource.
        """
        if not self.is_sql_datasource():
            return (None, None)
        temp_schema = self.metadata.get("temp_schema")
        temp_table = self.metadata.get("temp_table")
        if temp_schema and temp_table:
            return (temp_schema, temp_table)
        # Use source: either (schema, table) or we use a virtual "query" table that we'll copy from
        schema = self.metadata.get("schema", "public")
        table = self.metadata.get("table")
        custom_query = self.metadata.get("custom_query")
        if custom_query:
            return (None, None)  # Caller must use custom_query for initial copy
        return (schema, table)


@dataclass
class AgentState:
    """State for an agent session."""

    session_id: str
    messages: List[Message] = field(default_factory=list)
    data_context: Optional[DataContext] = None
    # Additional datasets for multi-file operations (linking / joining).
    # The primary dataset lives in data_context; extras go here.
    additional_data_contexts: List[DataContext] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    _on_change: Optional[Callable[["AgentState"], None]] = field(
        default=None, repr=False, compare=False
    )

    def set_change_callback(self, callback: Optional[Callable[["AgentState"], None]]) -> None:
        """Register callback fired whenever this state mutates."""
        self._on_change = callback

    def _notify_changed(self) -> None:
        self.updated_at = datetime.now()
        if self._on_change:
            self._on_change(self)

    def add_message(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        """Add a message to the conversation history."""
        message = Message(
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.messages.append(message)
        self._notify_changed()

    def get_recent_messages(self, n: int = 5) -> List[Message]:
        """Get the n most recent messages."""
        return self.messages[-n:] if len(self.messages) > n else self.messages

    def set_data_context(self, data_context: DataContext):
        """Set the data context for this session."""
        self.data_context = data_context
        self._notify_changed()

    def add_data_context(self, data_context: DataContext):
        """
        Add a dataset to the session.

        If no primary data_context exists yet, this becomes the primary.
        Otherwise it is appended to additional_data_contexts (for
        linking / joining workflows).
        """
        if self.data_context is None:
            self.data_context = data_context
        else:
            # Avoid duplicates by file_id
            existing_ids = {dc.file_id for dc in self.additional_data_contexts}
            if (
                data_context.file_id not in existing_ids
                and data_context.file_id != self.data_context.file_id
            ):
                self.additional_data_contexts.append(data_context)
        self._notify_changed()

    def remove_data_context(self, file_id: str):
        """
        Remove a dataset by file_id.

        If the primary data_context is removed and there are additional
        contexts, the first additional one is promoted to primary.
        """
        if self.data_context and self.data_context.file_id == file_id:
            if self.additional_data_contexts:
                self.data_context = self.additional_data_contexts.pop(0)
            else:
                self.data_context = None
        else:
            self.additional_data_contexts = [
                dc for dc in self.additional_data_contexts if dc.file_id != file_id
            ]
        self._notify_changed()

    def get_all_data_contexts(self) -> List[DataContext]:
        """
        Return all datasets in the session (primary + additional).

        Useful for the linking agent which needs access to every
        uploaded file.
        """
        contexts: List[DataContext] = []
        if self.data_context is not None:
            contexts.append(self.data_context)
        contexts.extend(self.additional_data_contexts)
        return contexts

    def get_all_datasets_for_linking(self) -> List[Dict[str, Any]]:
        """
        Return all datasets formatted for the linking agent's
        multi_join / recommend_multi_join methods.

        Each entry has keys: "data" (DataFrame), "name" (filename),
        "format" (None for in-memory DataFrames).
        """
        return [
            {
                "data": dc.dataframe,
                "name": dc.filename,
                "format": None,
                "metadata": dc.metadata,
            }
            for dc in self.get_all_data_contexts()
        ]

    @property
    def dataset_count(self) -> int:
        """Total number of datasets in the session."""
        count = 0
        if self.data_context is not None:
            count += 1
        count += len(self.additional_data_contexts)
        return count

    def get_context_summary(self) -> str:
        """Get a text summary of the current context."""
        summary_parts = []

        if self.data_context:
            df = self.data_context.dataframe
            summary_parts.append(f"Dataset: {self.data_context.filename}")
            summary_parts.append(f"Shape: {df.shape[0]} rows × {df.shape[1]} columns")
            summary_parts.append(f"Columns: {', '.join(df.columns[:5])}")
            if len(df.columns) > 5:
                summary_parts.append(f"... and {len(df.columns) - 5} more columns")

        if self.additional_data_contexts:
            summary_parts.append(f"Additional datasets: {len(self.additional_data_contexts)}")
            for dc in self.additional_data_contexts:
                adf = dc.dataframe
                summary_parts.append(
                    f"  - {dc.filename}: {adf.shape[0]} rows × {adf.shape[1]} columns"
                )

        if self.messages:
            summary_parts.append(f"Conversation history: {len(self.messages)} messages")

        return "\n".join(summary_parts)


class StateManager:
    """Manages agent states with Postgres-backed conversation history."""

    def __init__(self):
        self.states: Dict[str, AgentState] = {}
        self._persisted_message_counts: Dict[str, int] = {}
        # Sessions whose working DataFrame has changed since last snapshot.
        # Populated by mark_dirty(); drained by snapshot_all_dirty().
        self._dirty_sessions: set = set()
        # Sessions with no remaining data whose stale snapshots still need removal.
        self._snapshot_cleanup_sessions: set = set()
        # LLM router injected at startup for background title generation.
        self._llm_router: Optional[Any] = None
        # Track sessions for which we've already fired a title-generation task
        # so we don't generate redundant LLM calls on every subsequent save.
        self._title_attempted: set = set()
        logger.info("State manager initialized with DB-backed session history")

    def set_llm_router(self, llm_router: Any) -> None:
        """Register the LLM router so _save_state can trigger title generation."""
        self._llm_router = llm_router

    def mark_dirty(self, session_id: str) -> None:
        """Mark a session as needing a snapshot on the next flush cycle."""
        self._dirty_sessions.add(session_id)

    def mark_snapshot_clean(self, session_id: str) -> None:
        """Clear the pending-snapshot marker after a confirmed storage write."""
        self._dirty_sessions.discard(session_id)
        self._snapshot_cleanup_sessions.discard(session_id)

    def mark_snapshot_cleanup(self, session_id: str) -> None:
        """Retry removal of stale session snapshots during the next flush cycle."""
        self._snapshot_cleanup_sessions.add(session_id)

    async def snapshot_session(self, session_id: str) -> None:
        """Immediately snapshot a single session to storage (used on delete/logout)."""
        state = self.states.get(session_id)
        if state and state.data_context is not None:
            await self.snapshot_data_async(state)
            self._dirty_sessions.discard(session_id)

    async def snapshot_all_dirty(self) -> None:
        """Snapshot every session marked dirty since the last call. Called by the
        periodic background task so S3 writes are batched rather than per-query."""
        if not self._dirty_sessions and not self._snapshot_cleanup_sessions:
            return
        pending = list(self._dirty_sessions)
        self._dirty_sessions.clear()
        for session_id in pending:
            state = self.states.get(session_id)
            if state and state.data_context is not None:
                try:
                    await self.snapshot_data_async(state, raise_on_error=True)
                except Exception as exc:
                    # Re-queue so the next flush cycle retries instead of
                    # silently losing the pending write.
                    self._dirty_sessions.add(session_id)
                    logger.warning(
                        "Periodic snapshot failed for session %s (will retry): %s",
                        session_id,
                        exc,
                    )

        cleanup_pending = list(self._snapshot_cleanup_sessions)
        self._snapshot_cleanup_sessions.clear()
        if cleanup_pending:
            from helpers.storage import storage_manager

        for session_id in cleanup_pending:
            try:
                await asyncio.to_thread(storage_manager.delete_session_snapshots, session_id)
            except Exception as exc:
                self._snapshot_cleanup_sessions.add(session_id)
                logger.warning(
                    "Periodic snapshot cleanup failed for session %s (will retry): %s",
                    session_id,
                    exc,
                )

    def _save_state(self, state: AgentState) -> None:
        user_id = str(state.metadata.get("user_id") or "").strip()
        if not user_id:
            return
        persisted = self._persisted_message_counts.get(state.session_id, 0)
        if len(state.messages) <= persisted:
            return
        new_messages = state.messages[persisted:]
        db = SessionLocal()
        try:
            ensure_session_row(
                db,
                session_id=state.session_id,
                app_user_id=user_id,
            )
            for msg in new_messages:
                token_count = None
                if isinstance(msg.metadata, dict):
                    raw_count = msg.metadata.get("token_count")
                    try:
                        token_count = int(raw_count) if raw_count is not None else None
                    except Exception:
                        token_count = None
                db_append_message(
                    db,
                    session_id=state.session_id,
                    app_user_id=user_id,
                    role=msg.role,
                    content=msg.content,
                    content_type="text",
                    token_count=token_count,
                    metadata=msg.metadata,
                    timestamp=msg.timestamp,
                )
            db.commit()
            self._persisted_message_counts[state.session_id] = len(state.messages)
            state.metadata.pop("persist_failed", None)
            state.metadata.pop("persist_failed_at", None)
        except Exception as exc:
            db.rollback()
            state.metadata["persist_failed"] = True
            state.metadata["persist_failed_at"] = datetime.utcnow().isoformat()
            logger.error(
                "Failed to persist session %s (%d unsaved message(s)): %s",
                state.session_id,
                len(state.messages) - persisted,
                exc,
            )
        finally:
            db.close()

        # Fire background title generation after the first assistant reply lands.
        # We check that:  (a) at least one of the new batch is an assistant message,
        # (b) we haven't already attempted this for the session, and
        # (c) an LLM router is available.
        if (
            self._llm_router is not None
            and state.session_id not in self._title_attempted
            and any(m.role == "assistant" for m in new_messages)
        ):
            self._title_attempted.add(state.session_id)
            # Snapshot the first 10 messages to pass to the async task.
            messages_snapshot = list(state.messages[:10])
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._async_generate_title(state.session_id, user_id, messages_snapshot)
                )
            except RuntimeError:
                pass  # no running loop (test/CLI context) — skip silently

    async def _async_generate_title(
        self,
        session_id: str,
        user_id: str,
        messages: list,
    ) -> None:
        """Background task: generate an LLM title and write it to the DB row."""
        try:
            from llm.title_generator import generate_session_title

            title = await generate_session_title(self._llm_router, messages)
            if not title:
                return
            db = SessionLocal()
            try:
                row = get_session_row(db, session_id)
                if row and row.app_user_id == user_id:
                    row.title = title
                    db.commit()
                    logger.debug("Generated title for session %s: %r", session_id, title)
            except Exception:
                db.rollback()
            finally:
                db.close()
        except Exception as exc:
            logger.debug("Background title task failed for %s: %s", session_id, exc)

    def _sync_anonymous_flag(
        self,
        state: AgentState,
        is_anonymous: bool,
        app_user_id: Optional[str],
    ) -> None:
        """Keep in-memory metadata and DB row aligned with the latest request flag."""
        current = bool(state.metadata.get("is_anonymous"))
        if is_anonymous == current:
            return
        if is_anonymous:
            state.metadata["is_anonymous"] = True
        else:
            state.metadata.pop("is_anonymous", None)
        if not app_user_id:
            return
        db = SessionLocal()
        try:
            ensure_session_row(
                db,
                session_id=state.session_id,
                app_user_id=app_user_id,
                is_anonymous=is_anonymous,
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning(
                "Failed to sync is_anonymous for session %s: %s",
                state.session_id,
                exc,
            )
        finally:
            db.close()

    def _load_state(
        self, session_id: str, app_user_id: Optional[str] = None
    ) -> Optional[AgentState]:
        if not app_user_id:
            return self.states.get(session_id)
        db = SessionLocal()
        try:
            row = get_session_row(db, session_id)
            if row is None:
                return None
            if row.app_user_id != app_user_id:
                return None
            session_metadata: dict = {"user_id": app_user_id}
            if getattr(row, "is_anonymous", False):
                session_metadata["is_anonymous"] = True
            state = AgentState(
                session_id=session_id,
                metadata=session_metadata,
                created_at=row.started_at or row.created_at or datetime.now(),
                updated_at=row.updated_at or datetime.now(),
            )
            db_messages = db_list_messages(
                db,
                session_id=session_id,
                app_user_id=app_user_id,
                limit=10_000,
            )
            for db_msg in db_messages:
                metadata = {}
                if db_msg.metadata_json:
                    try:
                        metadata = json.loads(db_msg.metadata_json)
                    except Exception:
                        metadata = {}
                state.messages.append(
                    Message(
                        role=db_msg.role,
                        content=db_msg.content,
                        timestamp=db_msg.created_at or datetime.now(),
                        metadata=metadata,
                    )
                )
            state.set_change_callback(self._save_state)
            self.states[session_id] = state
            self._persisted_message_counts[session_id] = len(state.messages)

            # Restore DataContext from snapshot if one exists (enables pick-up-where-left-off)
            self._restore_snapshot_sync(state)

            return state
        except Exception as exc:
            logger.warning("Failed to load session from DB %s: %s", session_id, exc)
            return None
        finally:
            db.close()

    def create_session(
        self,
        session_id: str,
        app_user_id: Optional[str] = None,
        is_anonymous: bool = False,
    ) -> AgentState:
        """Create a new agent session."""
        # Verify DB ownership before mutating in-memory cache so a conflicting
        # session_id cannot poison self.states on another user's worker.
        if app_user_id:
            db = SessionLocal()
            try:
                ensure_session_row(
                    db,
                    session_id=session_id,
                    app_user_id=app_user_id,
                    is_anonymous=is_anonymous,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        metadata = {"user_id": app_user_id} if app_user_id else {}
        if is_anonymous:
            metadata["is_anonymous"] = True
        state = AgentState(session_id=session_id, metadata=metadata)
        state.set_change_callback(self._save_state)
        self.states[session_id] = state
        self._persisted_message_counts[session_id] = len(state.messages)
        return state

    def get_session(
        self, session_id: str, app_user_id: Optional[str] = None
    ) -> Optional[AgentState]:
        """Get an existing session."""
        state = self.states.get(session_id)
        if state is not None:
            owner = state.metadata.get("user_id")
            if app_user_id and owner and owner != app_user_id:
                return None
            if app_user_id and not owner:
                state.metadata["user_id"] = app_user_id
            return state
        return self._load_state(session_id, app_user_id)

    def get_or_create_session(
        self,
        session_id: str,
        app_user_id: Optional[str] = None,
        is_anonymous: bool = False,
    ) -> AgentState:
        """Get existing session or create new one."""
        existing = self.get_session(session_id, app_user_id)
        if existing is not None:
            self._sync_anonymous_flag(existing, is_anonymous, app_user_id)
            return existing
        return self.create_session(session_id, app_user_id, is_anonymous=is_anonymous)

    def delete_session(
        self,
        session_id: str,
        app_user_id: Optional[str] = None,
        *,
        purge_storage: bool = True,
    ) -> bool:
        """Delete a session.

        ``purge_storage`` deletes snapshot objects. The HTTP delete route
        passes False so background cleanup can still read snapshot JSON
        for warehouse scratch drops before wiping S3.
        """
        existed = False
        owner_user_id = app_user_id
        in_memory = self.states.get(session_id)
        if owner_user_id is None and in_memory is not None:
            owner_user_id = in_memory.metadata.get("user_id")
        if (
            app_user_id
            and in_memory
            and in_memory.metadata.get("user_id") not in {None, app_user_id}
        ):
            raise PermissionError("Session belongs to a different user")
        existed = in_memory is not None
        if owner_user_id:
            db = SessionLocal()
            try:
                existed = (
                    db_delete_session(
                        db,
                        session_id=session_id,
                        app_user_id=owner_user_id,
                    )
                    or existed
                )
                db.commit()
            except PermissionError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        # Do not evict the cached state until its durable deletion has committed.
        # Otherwise an FK or transient database failure leaves a sidebar row that
        # cannot be retried from the current worker.
        if in_memory is not None:
            self.states.pop(session_id, None)
            self._persisted_message_counts.pop(session_id, None)
        self._dirty_sessions.discard(session_id)
        self._snapshot_cleanup_sessions.discard(session_id)

        # Direct StateManager callers do not pass through the API workspace
        # cleanup path, so kernel cleanup is intentionally idempotent here too.
        try:
            from execution.duckdb import duckdb_kernel_manager  # noqa: PLC0415

            duckdb_kernel_manager.close_session(session_id, owner_user_id)
        except Exception as exc:
            logger.warning("Failed to close DuckDB kernel for session %s: %s", session_id, exc)

        if purge_storage:
            try:
                from helpers.storage import storage_manager as _sm

                deleted = _sm.delete_session_snapshots(session_id)
                if deleted:
                    logger.info("Deleted %d snapshot object(s) for session %s", deleted, session_id)
            except Exception as exc:
                logger.warning("Failed to delete snapshots for session %s: %s", session_id, exc)

        return existed

    # ------------------------------------------------------------------
    # Session snapshot — persist / restore working DataFrames to storage
    # ------------------------------------------------------------------

    def _snapshot_data_sync(self, state: AgentState, *, raise_on_error: bool = False) -> None:
        """
        Serialize every DataContext in *state* to parquet + JSON metadata and
        persist them via the configured storage backend (local or S3/MinIO).

        Snapshot layout per session (stored under session-snapshots/{session_id}/):
          __manifest__.json            — primary file_id, additional file_ids, timestamp
          {file_id}.parquet            — working_dataframe serialised as parquet (uploads)
          {file_id}.meta.json          — filename, metadata dict, uploaded_at, shape

        Live database sources persist only the metadata JSON; their rows remain
        in the connected system and are queried on demand.
        """
        from helpers.storage import storage_manager as _sm

        session_id = state.session_id
        all_contexts = state.get_all_data_contexts()
        if not all_contexts:
            return

        primary_file_id = state.data_context.file_id if state.data_context else None
        additional_ids = [dc.file_id for dc in state.additional_data_contexts]

        try:
            for dc in all_contexts:
                if dc.should_use_remote_reference():
                    dc.convert_to_remote_reference()
                    # Remove legacy sampled previews if this session predates
                    # metadata-only remote attachments.
                    _sm.delete_snapshot_object(session_id, f"{dc.file_id}.parquet")
                    _sm.delete_snapshot_object(session_id, f"{dc.file_id}.raw.parquet")
                else:
                    # Parquet bytes for the working DataFrame.
                    buf = io.BytesIO()
                    dc.working_dataframe.to_parquet(buf, index=False, engine="pyarrow")
                    _sm.save_snapshot_object(session_id, f"{dc.file_id}.parquet", buf.getvalue())

                    # Also snapshot raw DataFrame separately so restore can distinguish
                    # original upload from cleaned working copy.  Only write it when
                    # the frames actually differ, avoiding a duplicate initial upload.
                    if not dc.raw_dataframe.equals(dc.working_dataframe):
                        try:
                            raw_buf = io.BytesIO()
                            dc.raw_dataframe.to_parquet(raw_buf, index=False, engine="pyarrow")
                            _sm.save_snapshot_object(
                                session_id, f"{dc.file_id}.raw.parquet", raw_buf.getvalue()
                            )
                        except Exception:
                            pass  # non-fatal — restore falls back to working copy

                # Metadata JSON — encrypt credentials before writing to storage so a
                # restored SQL session can resume (decryption needs the same SECRET_KEY).
                safe_meta = _encrypt_sensitive_meta(dc.metadata or {})
                meta = {
                    "file_id": dc.file_id,
                    "filename": dc.filename,
                    "uploaded_at": dc.uploaded_at.isoformat(),
                    "metadata": safe_meta,
                    "raw_shape": list(dc.raw_dataframe.shape),
                    "working_shape": list(dc.working_dataframe.shape),
                }
                _sm.save_snapshot_object(
                    session_id,
                    f"{dc.file_id}.meta.json",
                    json.dumps(meta).encode(),
                )

            # Manifest ties everything together.
            # user_id is stored so _restore_snapshot_sync can reject accidental
            # cross-user restores (e.g. if two users somehow share a session_id).
            manifest = {
                "session_id": session_id,
                "user_id": (state.metadata or {}).get("user_id", ""),
                "primary_file_id": primary_file_id,
                "additional_file_ids": additional_ids,
                "snapshot_at": datetime.utcnow().isoformat(),
            }
            _sm.save_snapshot_object(
                session_id,
                "__manifest__.json",
                json.dumps(manifest).encode(),
            )
            logger.debug(
                "Snapshot saved for session %s (%d dataset(s))",
                session_id,
                len(all_contexts),
            )
        except Exception as exc:
            logger.warning("Failed to snapshot session %s: %s", session_id, exc)
            if raise_on_error:
                raise

    async def snapshot_data_async(self, state: AgentState, *, raise_on_error: bool = False) -> None:
        """
        Async wrapper around _snapshot_data_sync so the caller can fire-and-forget
        with asyncio.create_task() without blocking the response stream.

        Uses asyncio.to_thread so that blocking storage I/O (especially S3 put_object)
        runs in a thread pool and does not stall the event loop.
        """
        try:
            await asyncio.to_thread(self._snapshot_data_sync, state, raise_on_error=raise_on_error)
        except Exception as exc:
            logger.warning("Async snapshot failed for session %s: %s", state.session_id, exc)
            if raise_on_error:
                raise

    def _restore_snapshot_sync(self, state: AgentState) -> None:
        """
        Attempt to restore DataContexts from a previous snapshot.

        Called from _load_state() after messages are rebuilt from the DB.
        If no snapshot exists the state is left unchanged (data_context stays None).
        """
        from helpers.storage import storage_manager as _sm

        session_id = state.session_id
        manifest_bytes = _sm.load_snapshot_object(session_id, "__manifest__.json")
        if not manifest_bytes:
            return  # No snapshot exists for this session

        try:
            manifest = json.loads(manifest_bytes)
        except Exception as exc:
            logger.warning("Could not parse snapshot manifest for session %s: %s", session_id, exc)
            return

        # Guard: reject snapshot if it was saved under a different user_id.
        # Manifests written before this check lacked a user_id key — those are
        # allowed through so existing snapshots keep working.
        manifest_user_id = manifest.get("user_id", "")
        current_user_id = (state.metadata or {}).get("user_id", "")
        if manifest_user_id and current_user_id and manifest_user_id != current_user_id:
            logger.warning(
                "Snapshot for session %s belongs to user %s but current user is %s — "
                "refusing to restore (cross-user contamination guard).",
                session_id,
                manifest_user_id,
                current_user_id,
            )
            return

        primary_file_id: Optional[str] = manifest.get("primary_file_id")
        additional_ids: List[str] = manifest.get("additional_file_ids", [])
        all_file_ids = ([primary_file_id] if primary_file_id else []) + additional_ids

        rebuilt: Dict[str, DataContext] = {}
        for file_id in all_file_ids:
            meta_bytes = _sm.load_snapshot_object(session_id, f"{file_id}.meta.json")
            if not meta_bytes:
                logger.warning(
                    "Snapshot metadata missing for session %s file_id %s — skipping restore",
                    session_id,
                    file_id,
                )
                continue
            try:
                meta = json.loads(meta_bytes)
                uploaded_at_str = meta.get("uploaded_at")
                try:
                    uploaded_at = (
                        datetime.fromisoformat(uploaded_at_str)
                        if uploaded_at_str
                        else datetime.now()
                    )
                except Exception:
                    uploaded_at = datetime.now()

                restored_metadata = _restore_sensitive_meta(meta.get("metadata", {}))
                from datasources.registry import SOURCE_WORKING_COPY_IDS  # noqa: PLC0415

                is_remote = (
                    restored_metadata.get("storage_mode") == "remote_reference"
                    or restored_metadata.get("source") in SOURCE_WORKING_COPY_IDS
                )
                if is_remote:
                    columns = [
                        str(column["name"])
                        for column in restored_metadata.get("source_columns", [])
                        if isinstance(column, dict) and column.get("name")
                    ]
                    if not columns:
                        legacy_bytes = _sm.load_snapshot_object(session_id, f"{file_id}.parquet")
                        if legacy_bytes:
                            legacy_frame = pd.read_parquet(
                                io.BytesIO(legacy_bytes), engine="pyarrow"
                            )
                            columns = [str(column) for column in legacy_frame.columns]
                            restored_metadata["source_columns"] = [
                                {"name": str(name), "db_type": str(dtype), "kind": "string"}
                                for name, dtype in legacy_frame.dtypes.items()
                            ]
                    restored_metadata["storage_mode"] = "remote_reference"
                    restored_metadata["source_row_count"] = int(
                        restored_metadata.get("source_row_count") or 0
                    )
                    raw_df = pd.DataFrame(columns=columns)
                    df = raw_df
                    _sm.delete_snapshot_object(session_id, f"{file_id}.parquet")
                    _sm.delete_snapshot_object(session_id, f"{file_id}.raw.parquet")
                    meta["metadata"] = _encrypt_sensitive_meta(restored_metadata)
                    meta["raw_shape"] = [0, len(columns)]
                    meta["working_shape"] = [0, len(columns)]
                    _sm.save_snapshot_object(
                        session_id,
                        f"{file_id}.meta.json",
                        json.dumps(meta).encode(),
                    )
                else:
                    parquet_bytes = _sm.load_snapshot_object(session_id, f"{file_id}.parquet")
                    if not parquet_bytes:
                        logger.warning(
                            "Snapshot data missing for session %s file_id %s — skipping restore",
                            session_id,
                            file_id,
                        )
                        continue
                    df = pd.read_parquet(io.BytesIO(parquet_bytes), engine="pyarrow")
                    # Try to load the separately snapshotted raw DataFrame.
                    # Falls back to a copy of the working frame for sessions
                    # snapshotted before this fix or when the data was unmodified.
                    raw_df = df.copy(deep=True)
                    raw_bytes = _sm.load_snapshot_object(session_id, f"{file_id}.raw.parquet")
                    if raw_bytes:
                        try:
                            raw_df = pd.read_parquet(io.BytesIO(raw_bytes), engine="pyarrow")
                        except Exception:
                            raw_df = df.copy(deep=True)

                dc = DataContext(
                    file_id=file_id,
                    filename=meta.get("filename", "unknown"),
                    raw_dataframe=raw_df,
                    working_dataframe=df,
                    uploaded_at=uploaded_at,
                    metadata=restored_metadata,
                )
                rebuilt[file_id] = dc
            except Exception as exc:
                logger.warning(
                    "Failed to restore DataContext for session %s file_id %s: %s",
                    session_id,
                    file_id,
                    exc,
                )

        if not rebuilt:
            return

        # Restore primary
        if primary_file_id and primary_file_id in rebuilt:
            state.data_context = rebuilt[primary_file_id]
        elif rebuilt:
            state.data_context = next(iter(rebuilt.values()))

        # Restore additional
        for fid in additional_ids:
            if fid in rebuilt:
                state.additional_data_contexts.append(rebuilt[fid])

        snap_at = manifest.get("snapshot_at", "unknown")
        filenames = [dc.filename for dc in rebuilt.values()]
        logger.warning(
            "SNAPSHOT RESTORE: session=%s restored %d dataset(s) %s from snapshot "
            "taken at %s — if this is a new session and no data was uploaded, "
            "check that the frontend is sending a fresh session_id.",
            session_id,
            len(rebuilt),
            filenames,
            snap_at,
        )

    def list_sessions(self, app_user_id: Optional[str] = None) -> List[str]:
        """List all active session IDs."""
        if app_user_id is None:
            return sorted(self.states.keys())
        return sorted(
            session_id
            for session_id, state in self.states.items()
            if state.metadata.get("user_id") == app_user_id
        )


# Global state manager instance
state_manager = StateManager()
