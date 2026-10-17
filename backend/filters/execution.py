"""Shared pandas and DuckDB FilterSpec execution helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from datasources import adapters as table_adapters
from execution.duckdb import DuckDBKernelError
from filters import annotate_pandas, mongo_service, service, sql_service
from filters.predicate_pandas import UnknownColumn
from filters.spec import FilterSpec


def build_pandas_filter_page(
    df,
    cache_part,
    spec: FilterSpec,
    *,
    session_id: str,
    user: str,
    page: int,
    page_size: int,
    matches_only: Optional[bool],
    enabled_rules: Optional[List[str]],
    sort_by: Optional[str],
    sort_dir: str,
    search: Optional[str],
    filters: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    row_keys, identity_label, identity_warning = service.resolve_row_identity(df, spec.identity)
    workspace = None
    runner = None
    if any(step.kind == "pandas" for step in spec.annotate):
        from workspace.manager import WorkspaceManager  # noqa: PLC0415
        from workspace.runner import WorkspaceSandboxRunner  # noqa: PLC0415

        workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
        runner = WorkspaceSandboxRunner()
    cache_key = (
        (session_id, *cache_part, spec.annotate_fingerprint())
        if cache_part is not None and spec.annotate
        else None
    )
    ann = annotate_pandas.get_cached_annotations(
        cache_key, df, spec, row_keys=row_keys, workspace=workspace, runner=runner
    )
    if workspace is not None:
        (workspace / annotate_pandas.INPUT_PARQUET).unlink(missing_ok=True)
    try:
        return service.build_filter_page(
            df,
            spec,
            ann,
            identity_label=identity_label,
            identity_warning=identity_warning,
            page=page,
            page_size=page_size,
            matches_only=matches_only,
            enabled_rules=enabled_rules,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=filters,
        )
    except UnknownColumn as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Select predicate references unknown column {exc.column!r}.",
        )
    except service.AnnotationCollision as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def build_duckdb_filter_page(
    session,
    ctx,
    spec: FilterSpec,
    *,
    session_id: str,
    user: str,
    page: int,
    page_size: int,
    matches_only: Optional[bool],
    enabled_rules: Optional[List[str]],
    sort_by: Optional[str],
    sort_dir: str,
    search: Optional[str],
    filters: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    from datasources.adapters.duckdb import DuckDBTableAdapter  # noqa: PLC0415
    from execution.session_plane import plane_to_pandas, resolve_data_plane  # noqa: PLC0415

    # The "dataset" route may target the loaded preview of a Postgres
    # context; this helper is specifically the snapshot path.
    plane = resolve_data_plane(session, ctx, snapshot_postgres=True)
    adapter = DuckDBTableAdapter(plane)
    # Annotation materialization remains Python-based, and keyless snapshot
    # filters use the pandas positional identity semantics. Both still read the
    # canonical DuckDB snapshot rather than the connector's preview object.
    try:
        key = sql_service.detect_key_single(adapter, spec.identity)
        fallback = bool(spec.annotate or key is None)
        if fallback:
            frame = plane_to_pandas(plane)
            result = build_pandas_filter_page(
                frame,
                ("duckdb", plane.snapshot_token),
                spec,
                session_id=session_id,
                user=user,
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
        else:
            result = sql_service.build_sql_filter_page(
                adapter,
                spec,
                session_id=session_id,
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
    except UnknownColumn as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Select predicate references unknown column {exc.column!r}.",
        )
    except service.AnnotationCollision as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (table_adapters.AdapterError, DuckDBKernelError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result.update(
        {
            "engine": "hybrid" if fallback else "duckdb",
            "scope": plane.scope.value,
            "freshness": plane.freshness.value,
            "source_row_limit": plane.row_limit,
            "source_truncated": plane.truncated,
            "schema_complete": plane.schema_complete,
        }
    )
    result["summary"].update(
        {
            "engine": result["engine"],
            "scope": result["scope"],
            "freshness": result["freshness"],
            "source_row_limit": result["source_row_limit"],
            "source_truncated": result["source_truncated"],
            "schema_complete": result["schema_complete"],
        }
    )
    return result


def build_mongo_filter_page(
    ctx,
    spec: FilterSpec,
    variant: str,
    *,
    session_id: str,
    page: int,
    page_size: int,
    matches_only: Optional[bool],
    enabled_rules: Optional[List[str]],
    sort_by: Optional[str],
    sort_dir: str,
    search: Optional[str],
    filters: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Serve a filter page from a MongoDB collection via aggregation.

    The scratch working copy and the source collection differ only in which
    namespace is read; the source is never written to either way.
    """
    from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415
    from agents.mongo_agent.pipeline_guard import PipelineGuardError  # noqa: PLC0415
    from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

    meta = ctx.metadata or {}
    if variant == "temp":
        database, collection = meta.get("temp_schema"), meta.get("temp_table")
        if not database or not collection:
            raise HTTPException(
                status_code=404,
                detail="No working copy exists yet — it is created when an agent first writes.",
            )
    elif variant == "base":
        database, collection = meta.get("database"), meta.get("collection")
    elif variant.startswith("copy:"):
        # Mongo working copies are registered in the same metadata registry as
        # SQL copies, but all physical collections live in the runner's
        # Stych-owned scratch database.  Resolve the registry entry rather
        # than treating the user-controlled suffix as a collection name.
        from agents.sql_agent import working_copies as wc  # noqa: PLC0415

        name = variant[len("copy:") :].strip()
        if not name:
            raise HTTPException(status_code=400, detail="A working-copy name is required.")
        entry = wc.get_copy(meta, name)
        if entry is None or not isinstance(entry.get("table"), str):
            raise HTTPException(status_code=404, detail=f"No working copy named '{name}' exists.")
        database, collection = None, entry["table"]
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source variant '{variant}'.")

    try:
        runner = MongoScratchRunner.from_meta(meta)
        if variant.startswith("copy:"):
            # Named copies are scratch-only.  This check is important for
            # restored/tampered metadata because run_pipeline permits reads
            # from the connected source database.
            database = runner.temp_schema
            from agents.mongo_agent.pipeline_guard import assert_scratch_collection  # noqa: PLC0415

            collection = assert_scratch_collection(str(collection), scratch_prefix="stych_")
        result = mongo_service.build_mongo_filter_page(
            runner,
            spec,
            database=str(database),
            collection=str(collection),
            session_id=session_id,
            temp_generation=int(meta.get("temp_table_generation") or 0),
            page=page,
            page_size=page_size,
            matches_only=matches_only,
            enabled_rules=enabled_rules,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=filters,
        )
    except UnknownColumn as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Select predicate references unknown column {exc.column!r}.",
        )
    except (MongoRunnerError, PipelineGuardError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["engine"] = "mongodb"
    return result
