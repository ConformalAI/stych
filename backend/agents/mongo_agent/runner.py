"""Scratch-collection runner for MongoDB.

Working copies live in a Stych-owned database (default ``stych_temp``) inside
the customer's own cluster, created with ``$out``. The source collection is
never modified.

Two things differ from every SQL runner here, one in MongoDB's favour and one
against it:

- **Row identity is free.** Every document carries ``_id``: unique, immutable,
  and guaranteed by the database. ``$out`` preserves it, so a working copy is
  aligned with its source by construction — no stamped row id, no pinned
  snapshot, no key detection. This is the cleanest identity story of any
  connector here.
- **Schema is never complete.** Collections are schemaless, so the field set is
  inferred from a sample, and a field appearing only in an unsampled document is
  invisible. That is a property of the data model, not something a runner fixes.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from agents.mongo_agent.pipeline_guard import (
    PipelineGuardError,
    assert_scratch_collection,
    validate_document_expression,
    validate_pipeline,
)

logger = logging.getLogger(__name__)

DEFAULT_SCRATCH_DB = "stych_temp"
#: Every collection Stych creates carries this prefix, so a write cannot land on
#: a customer collection even inside the scratch database.
SCRATCH_PREFIX = "stych_"
#: Marker collection that forces the scratch database into existence. It is
#: Stych-owned but is not a working copy, so listings exclude it.
KEEPALIVE = "stych_keepalive"
#: MongoDB's own identity field, used directly as the row key.
ROW_KEY = "_id"

_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class MongoRunnerError(Exception):
    """Raised when a scratch-collection operation fails."""


def scratch_database(config: Mapping[str, Any]) -> str:
    name = str(config.get("scratch_database") or DEFAULT_SCRATCH_DB).strip()
    if not _DB_NAME_RE.match(name):
        raise MongoRunnerError(
            f"Invalid MongoDB scratch database {name!r}: use letters, digits, and underscores."
        )
    return name


def collection_name(session_id: str) -> str:
    """A scratch collection name that records which session owns it."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", session_id)[:32]
    return f"{SCRATCH_PREFIX}{safe}_{uuid.uuid4().hex[:8]}"


class MongoScratchRunner:
    """Clone source collections into a scratch database and run pipelines there."""

    #: A pipeline is one server-side operation, so a copy either lands or does not.
    atomic_writes = True
    engine_label = "MongoDB"

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        url = str(cfg.get("connection_url") or "").strip()
        if not url or url == "[REDACTED]" or "://" not in url:
            raise MongoRunnerError(
                "The MongoDB connection for this source is unavailable — "
                "please reconnect the collection."
            )
        database = str(cfg.get("database") or "").strip()
        if not database:
            raise MongoRunnerError(
                "MongoDB database is required for the scratch working copy — reconnect."
            )
        self.connection_config = cfg
        self.connection_url = url
        self.database = database
        self._scratch_db = scratch_database(cfg)

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> "MongoScratchRunner":
        cfg: Dict[str, Any] = {
            "connection_url": meta.get("connection_url"),
            "database": meta.get("database"),
        }
        if meta.get("scratch_database"):
            cfg["scratch_database"] = meta["scratch_database"]
        return cls(cfg)

    # -- identity -----------------------------------------------------------

    @property
    def temp_schema(self) -> str:
        """The scratch database. Named for the shared vocabulary, not for Mongo."""
        return self._scratch_db

    @property
    def credential_key(self) -> str:
        return f"{self.connection_url}|{self.database}|{self._scratch_db}"

    def quote_relation(self, schema: str, table: str) -> str:
        """A namespace reference, for messages and logs."""
        return f"{schema}.{table}"

    # -- connection ---------------------------------------------------------

    def _client(self):
        from datasources.adapters.mongodb import _get_shared_client  # noqa: PLC0415

        return _get_shared_client(self.connection_url)

    def _scratch(self, name: str):
        assert_scratch_collection(name, scratch_prefix=SCRATCH_PREFIX)
        return self._client()[self._scratch_db][name]

    def ensure_temp_schema(self) -> None:
        """Make sure the scratch database exists.

        A replica set or standalone creates it on first write, but a sharded
        cluster requires the target database to already exist — so it is created
        explicitly rather than relying on the lazy path.
        """
        try:
            marker = self._client()[self._scratch_db][KEEPALIVE]
            marker.update_one({"_id": "keepalive"}, {"$set": {"_id": "keepalive"}}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(
                f"Cannot create the scratch database {self._scratch_db!r}. "
                f"Grant write access to it, or set a different scratch database. ({exc})"
            ) from exc

    # -- copies -------------------------------------------------------------

    def create_temp_table_from_source(
        self,
        session_id: str,
        source_schema: str,
        source_table: Optional[str],
        custom_query: Optional[str] = None,
        max_rows: int = 0,
        table_name: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Clone a source collection into scratch. Returns ``(scratch_db, name)``.

        ``$out`` preserves ``_id``, so the copy aligns with the source
        document-for-document without stamping anything.
        """
        del max_rows, custom_query  # full-collection clone
        if not source_table:
            raise MongoRunnerError("A source collection is required")
        name = table_name or collection_name(session_id)
        assert_scratch_collection(name, scratch_prefix=SCRATCH_PREFIX)
        self.ensure_temp_schema()
        self.run_pipeline(
            source_table,
            [{"$out": {"db": self._scratch_db, "coll": name}}],
            database=source_schema or self.database,
        )
        logger.info("Created MongoDB scratch collection %s.%s", self._scratch_db, name)
        return (self._scratch_db, name)

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None:
        assert_scratch_collection(new_table, scratch_prefix=SCRATCH_PREFIX)
        assert_scratch_collection(source_table, scratch_prefix=SCRATCH_PREFIX)
        self.run_pipeline(
            source_table,
            [{"$out": {"db": self._scratch_db, "coll": new_table}}],
            database=self._scratch_db,
        )

    def drop_temp_table(self, table: str) -> None:
        try:
            self._scratch(table).drop()
        except PipelineGuardError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        try:
            names = self._client()[self._scratch_db].list_collection_names()
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc
        return [name for name in names if name.startswith(prefix) and name != KEEPALIVE]

    # -- introspection ------------------------------------------------------

    def table_exists(self, schema: str, table: str) -> bool:
        try:
            return table in self._client()[schema].list_collection_names()
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def get_row_count(self, schema: str, table: str) -> int:
        try:
            return int(self._client()[schema][table].count_documents({}))
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def get_table_columns(
        self, schema: str, table: str, sample: int = 1_000
    ) -> List[Dict[str, Any]]:
        """Fields seen in a sample of documents.

        Inferred, never complete: a field present only in unsampled documents is
        invisible. Every entry carries ``sampled`` so a caller cannot mistake
        this for a declared schema.
        """
        from datasources.mongodb import flatten_documents  # noqa: PLC0415

        try:
            docs = list(self._client()[schema][table].find(limit=int(sample)))
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc
        frame = flatten_documents(docs)
        return [
            {
                "column_name": str(column),
                "data_type": str(frame[column].dtype),
                "is_nullable": "YES",
                "sampled": True,
            }
            for column in frame.columns
        ]

    # -- execution ----------------------------------------------------------

    def run_pipeline(
        self,
        collection: str,
        pipeline: Sequence[Mapping[str, Any]],
        *,
        database: Optional[str] = None,
        timeout_ms: int = 60_000,
        allow_other_collections: bool = True,
        allow_writes: bool = True,
    ) -> List[Dict[str, Any]]:
        """Run an aggregation after checking it against the pipeline guard."""
        running_db = database or self.database
        validate_pipeline(
            pipeline,
            scratch_db=self._scratch_db,
            scratch_prefix=SCRATCH_PREFIX,
            running_db=running_db,
            allow_other_collections=allow_other_collections,
            allow_writes=allow_writes,
        )
        try:
            cursor = self._client()[running_db][collection].aggregate(
                list(pipeline), maxTimeMS=int(timeout_ms)
            )
            return list(cursor)
        except PipelineGuardError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def update_many(self, collection: str, filter_doc: Mapping[str, Any], update: Any) -> int:
        assert_scratch_collection(collection, scratch_prefix=SCRATCH_PREFIX)
        validate_document_expression(filter_doc)
        validate_document_expression(update)
        if not isinstance(update, (Mapping, list, tuple)) or not update:
            raise MongoRunnerError("MongoDB update must be a non-empty document or pipeline")
        try:
            result = self._scratch(collection).update_many(dict(filter_doc), update)
            return int(result.modified_count)
        except PipelineGuardError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def delete_many(self, collection: str, filter_doc: Mapping[str, Any]) -> int:
        assert_scratch_collection(collection, scratch_prefix=SCRATCH_PREFIX)
        validate_document_expression(filter_doc)
        try:
            result = self._scratch(collection).delete_many(dict(filter_doc))
            return int(result.deleted_count)
        except PipelineGuardError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc

    def deduplicate(self, collection: str, fields: Sequence[str]) -> int:
        """Keep the lowest-_id document for each field tuple inside scratch."""
        assert_scratch_collection(collection, scratch_prefix=SCRATCH_PREFIX)
        names = [str(field).strip() for field in fields if str(field).strip()]
        if not names:
            raise MongoRunnerError("At least one deduplication field is required")
        before = self.get_row_count(self._scratch_db, collection)
        replacement = collection_name("dedup")
        group_id = {name: f"${name}" for name in names}
        self.run_pipeline(
            collection,
            [
                {"$sort": {"_id": 1}},
                {"$group": {"_id": group_id, "doc": {"$first": "$$ROOT"}}},
                {"$replaceWith": "$doc"},
                {"$out": {"db": self._scratch_db, "coll": replacement}},
            ],
            database=self._scratch_db,
        )
        try:
            self._scratch(replacement).rename(collection, dropTarget=True)
        except Exception as exc:  # noqa: BLE001
            raise MongoRunnerError(str(exc)) from exc
        return before - self.get_row_count(self._scratch_db, collection)

    def run_pipeline_frame(
        self,
        collection: str,
        pipeline: Sequence[Mapping[str, Any]],
        *,
        database: Optional[str] = None,
    ) -> pd.DataFrame:
        """Aggregation results as a flattened frame, for callers expecting rows."""
        from datasources.mongodb import flatten_documents  # noqa: PLC0415

        return flatten_documents(self.run_pipeline(collection, pipeline, database=database))
