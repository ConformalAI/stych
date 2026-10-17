"""
Data preview endpoints: list previewable sources (workspace files + in-memory
datasets) and serve paginated/filtered/sorted pages of tabular data or capped
raw text. Pipeline logic lives in ``api/preview_service.py``.
"""

from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query

from agents.state import state_manager
from api import diff_service, mongo_diff_service, preview_service, sql_diff_service
from api.cleaning_export_routes import _resolve_workspace_path
from api.dependencies import get_current_user
from api.schemas import (
    PreviewDiffPage,
    PreviewSource,
    PreviewSourcesResponse,
    PreviewTablePage,
    PreviewTextPage,
)
from api.source_lifecycle import raise_source_removed
from datasources import adapters as table_adapters
from datasources.feature_resolver import capability_summary
from datasources.registry import SOURCE_WORKING_COPY_IDS
from workspace.manager import WorkspaceManager, dataset_stem, list_workspace_artifacts

router = APIRouter()

# Module-level indirection so tests can inject a fake adapter resolver.
_resolve_sql_adapter = table_adapters.resolve_adapter

# Prefer the lossless parquet when a dataset has leftover csv/xlsx copies of the same stem.
_TABLE_SUFFIX_RANK = {"parquet": 0, "tsv": 1, "xlsx": 2, "csv": 3}


def _adapter_http_error(exc: table_adapters.AdapterError) -> HTTPException:
    if isinstance(exc, table_adapters.AdapterUnavailable):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, table_adapters.TempTableUnavailable):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, table_adapters.AdapterTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _sql_source_entries(ctx, active_stem: Optional[str], index: int) -> list[PreviewSource]:
    """Preview sources for a DB-backed context: the user's base table, plus every
    named ``stych_temp`` working copy (Postgres only), the active one flagged.

    ``rows`` is left unset on purpose — counting a remote table belongs to the
    page fetch (short timeout + cache), not to a listing that must stay fast.
    """
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    meta = ctx.metadata or {}
    origin = meta.get("source")
    df = ctx.dataframe
    copies = wc.list_copies(meta) if origin in SOURCE_WORKING_COPY_IDS else []
    has_temp = (
        origin in SOURCE_WORKING_COPY_IDS
        and bool(meta.get("temp_schema"))
        and bool(meta.get("temp_table"))
    )
    identity = dataset_stem(ctx.filename)
    base_active = not (copies or has_temp) and (
        (bool(active_stem) and identity == active_stem) or (active_stem is None and index == 0)
    )
    entries = [
        PreviewSource(
            id=f"{ctx.file_id}::base",
            kind="sql",
            name=ctx.filename,
            file_id=ctx.file_id,
            renderer="table",
            columns=len(df.columns) if df is not None else None,
            origin=origin,
            role="base",
            supports_stats=(origin == "postgres"),
            capability_summary=capability_summary(origin),
            is_active=base_active,
        )
    ]
    table_label = meta.get("table") or "query"
    if copies:
        active_name = meta.get(wc.ACTIVE_KEY)
        for copy in copies:
            pinned = wc.is_base(copy)
            entries.append(
                PreviewSource(
                    id=f"{ctx.file_id}::copy:{copy['name']}",
                    kind="sql",
                    name=(
                        f"{table_label} · pinned snapshot"
                        if pinned
                        else f"{table_label} · {copy['name']}"
                    ),
                    file_id=ctx.file_id,
                    renderer="table",
                    origin=origin,
                    role="base" if pinned else "working",
                    supports_stats=True,
                    capability_summary=(
                        "Pinned · Read-only snapshot taken when the table was connected"
                        if pinned
                        else capability_summary(origin, working_copy=True)
                    ),
                    is_active=(copy["name"] == active_name),
                )
            )
    elif has_temp:
        # Pre-registry session (legacy keys only): keep the single ::temp entry.
        entries.append(
            PreviewSource(
                id=f"{ctx.file_id}::temp",
                kind="sql",
                name=f"{table_label} (working copy)",
                file_id=ctx.file_id,
                renderer="table",
                origin=origin,
                role="working",
                supports_stats=True,
                capability_summary=capability_summary(origin, working_copy=True),
                is_active=True,
            )
        )
    return entries


@router.get("/preview/sources/{session_id}", response_model=PreviewSourcesResponse)
async def list_preview_sources(
    session_id: str, user: str = Depends(get_current_user)
) -> PreviewSourcesResponse:
    """
    List everything previewable in a session. Workspace files are the canonical
    sources (served from their true on-disk bytes); an in-memory dataset is only
    surfaced when it has no backing file yet (unsaved uploads, connector/SQL
    previews). This keeps the mutable working frame — which ``set_active_dataset``
    can silently repoint to a *different* file — from masquerading as the original
    upload, which would make an original-vs-derived diff show no changes.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sources: list[PreviewSource] = []
    if not session.data_context:
        return PreviewSourcesResponse(sources=sources)

    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    active = session.metadata.get("active_dataset")
    active_stem = dataset_stem(active) if active else None

    # Map canonical dataset identity -> owning file_id so workspace-file sources
    # that mirror a dataset carry the dataset's file_id (drives full-dataset delete).
    all_contexts = list(session.get_all_data_contexts())
    stem_to_file_id = {dataset_stem(ctx.filename): ctx.file_id for ctx in all_contexts}

    # Group workspace table files by canonical dataset identity so a dataset's
    # mirror copies collapse to one entry; report/text artifacts stand alone.
    table_groups: dict[str, list[dict]] = {}
    text_arts: list[dict] = []
    for art in list_workspace_artifacts(workspace):
        suffix = art["suffix"]
        if suffix not in preview_service.PREVIEWABLE_SUFFIXES:
            continue
        if suffix in preview_service.TEXT_SUFFIXES:
            text_arts.append(art)
            continue
        table_groups.setdefault(dataset_stem(art["name"]), []).append(art)

    for identity, arts in sorted(table_groups.items()):
        # One entry per dataset identity. Prefer parquet so the picker name
        # matches the bytes on disk; leftover csv/xlsx of the same stem collapse.
        chosen = min(arts, key=lambda a: _TABLE_SUFFIX_RANK.get(a["suffix"], 9))
        sources.append(
            PreviewSource(
                id=chosen["path"],
                kind="file",
                name=chosen["name"],
                file_id=stem_to_file_id.get(identity),
                renderer="table",
                suffix=chosen["suffix"],
                size_bytes=int(chosen["size_bytes"]),
                modified_at=chosen["modified_at"],
                capability_summary=capability_summary("upload"),
                is_active=bool(active_stem) and identity == active_stem,
            )
        )

    # In-memory datasets only when NOT already represented by a workspace file.
    # A saved dataset is served from its file above, not the mutable frame.
    file_identities = set(table_groups)
    for index, ctx in enumerate(all_contexts):
        identity = dataset_stem(ctx.filename)
        # DB-backed contexts with live credentials are served by pushdown over
        # the full remote table, not from the 10K connect-time sample. Contexts
        # whose credentials were redacted (legacy snapshots) fall through to the
        # sample entry below so the UI still shows something.
        is_remote_source = bool(
            callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source()
        )
        if table_adapters.supports_pushdown(ctx.metadata) or is_remote_source:
            sources.extend(_sql_source_entries(ctx, active_stem, index))
            continue
        if identity in file_identities:
            continue
        df = ctx.dataframe
        origin = ctx.metadata.get("source", "upload")
        sources.append(
            PreviewSource(
                id=ctx.file_id,
                kind="dataset",
                name=ctx.filename,
                file_id=ctx.file_id,
                renderer="table",
                rows=len(df),
                columns=len(df.columns),
                origin=origin,
                capability_summary=capability_summary(
                    origin, sampled_fallback=(origin != "upload")
                ),
                is_active=(bool(active_stem) and identity == active_stem)
                or (active_stem is None and index == 0),
            )
        )

    for art in text_arts:
        sources.append(
            PreviewSource(
                id=art["path"],
                kind="file",
                name=art["name"],
                renderer="text",
                suffix=art["suffix"],
                size_bytes=int(art["size_bytes"]),
                modified_at=art["modified_at"],
                is_active=False,
            )
        )

    return PreviewSourcesResponse(sources=sources)


# Source-resolution helpers now live in preview_service (shared with the
# filters router); the old private names stay as aliases for existing callers.
_parse_filters = preview_service.parse_filters_param
_resolve_sql_source_id = preview_service.resolve_sql_source_id
_lossless_source = preview_service.lossless_source
_load_tabular_source = preview_service.load_tabular_source


@router.get("/preview/diff/{session_id}", response_model=PreviewDiffPage)
def get_preview_diff(
    session_id: str,
    base_source: str = Query(..., pattern="^(file|dataset|sql)$"),
    target_source: str = Query(..., pattern="^(file|dataset|sql)$"),
    base_path: Optional[str] = Query(None),
    base_file_id: Optional[str] = Query(None),
    target_path: Optional[str] = Query(None),
    target_file_id: Optional[str] = Query(None),
    base_source_id: Optional[str] = Query(None),
    target_source_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    changes_only: bool = Query(False),
    change_kind: Optional[str] = Query(
        None, pattern="^(added|removed|changed|imputed|transformed)$"
    ),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    search: Optional[str] = Query(None),
    filters: Optional[str] = Query(None, description='JSON object {"column": "substring"}'),
    user: str = Depends(get_current_user),
):
    """
    Compare two tabular sources (base -> target) and return one page of the
    target annotated with per-cell change classifications. Sync ``def`` so the
    pandas work runs in the FastAPI threadpool.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if (base_source == "sql") != (target_source == "sql"):
        raise HTTPException(
            status_code=400,
            detail="Cannot compare a database table with a file — pick two tables "
            "from the same database connection.",
        )
    if base_source == "sql":
        base_ctx, base_variant = _resolve_sql_source_id(session, base_source_id)
        target_ctx, target_variant = _resolve_sql_source_id(session, target_source_id)
        unsupported = [
            source
            for source in (
                (base_ctx.metadata or {}).get("source"),
                (target_ctx.metadata or {}).get("source"),
            )
            if source not in SOURCE_WORKING_COPY_IDS
        ]
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Diff is not supported for {unsupported[0]} sources. "
                    "Supported: " + ", ".join(sorted(SOURCE_WORKING_COPY_IDS)) + "."
                ),
            )
        if (base_ctx.metadata or {}).get("source") == "mongodb":
            # Documents are compared by aggregation; the SQL diff below has no
            # compilation target for a collection.
            return _mongo_diff(
                base_ctx,
                target_ctx,
                base_variant,
                target_variant,
                page=page,
                page_size=page_size,
                changes_only=changes_only,
                change_kind=change_kind,
                filters=_parse_filters(filters),
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        try:
            base_adapter = _resolve_sql_adapter(base_ctx, base_variant)
            target_adapter = _resolve_sql_adapter(target_ctx, target_variant)
            return sql_diff_service.build_sql_diff_page(
                base_adapter,
                target_adapter,
                session_id=session_id,
                page=page,
                page_size=page_size,
                changes_only=changes_only,
                change_kind=change_kind,
                filters=_parse_filters(filters),
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        except table_adapters.AdapterError as exc:
            raise _adapter_http_error(exc)

    base_df, base_key = _load_tabular_source(
        session, session_id, user, base_source, base_path, base_file_id
    )
    target_df, target_key = _load_tabular_source(
        session, session_id, user, target_source, target_path, target_file_id
    )

    cache_key = (
        (session_id, base_key, target_key)
        if base_key is not None and target_key is not None
        else None
    )
    result = diff_service.get_cached_diff(cache_key, base_df, target_df)
    return diff_service.build_diff_page(
        result,
        page=page,
        page_size=page_size,
        changes_only=changes_only,
        change_kind=change_kind,
        filters=_parse_filters(filters),
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.get(
    "/preview/page/{session_id}",
    response_model=Union[PreviewTablePage, PreviewTextPage],
)
def get_preview_page(
    session_id: str,
    source: str = Query(..., pattern="^(file|dataset|sql)$"),
    path: Optional[str] = Query(None, description="Workspace artifact relative path"),
    file_id: Optional[str] = Query(None, description="In-memory dataset file_id"),
    source_id: Optional[str] = Query(None, description='DB source id: "<file_id>::base|temp"'),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    search: Optional[str] = Query(None),
    filters: Optional[str] = Query(None, description='JSON object {"column": "substring"}'),
    include_stats: bool = Query(False),
    user: str = Depends(get_current_user),
):
    """
    One page of a preview. Sync ``def`` on purpose: DuckDB/pandas/DB work runs
    in the FastAPI threadpool instead of blocking the event loop.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    parsed_filters = _parse_filters(filters)

    if source == "sql":
        ctx, variant = _resolve_sql_source_id(session, source_id)
        try:
            adapter = _resolve_sql_adapter(ctx, variant)
            return preview_service.build_adapter_page(
                adapter,
                session_id=session_id,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=parsed_filters,
                include_stats=include_stats,
            )
        except table_adapters.AdapterError as exc:
            raise _adapter_http_error(exc)

    if source == "dataset":
        if not file_id:
            raise HTTPException(status_code=400, detail="Missing file_id.")
        ctx = next(
            (c for c in session.get_all_data_contexts() if c.file_id == file_id),
            None,
        )
        if ctx is None:
            raise_source_removed()
        df = ctx.dataframe
        stats = preview_service.compute_stats(df) if include_stats else None
        return preview_service.build_table_page(
            df,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=parsed_filters,
            stats=stats,
        )

    if not path:
        raise HTTPException(status_code=400, detail="Missing path.")
    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    target = _resolve_workspace_path(workspace, path)
    suffix = target.suffix.lower().lstrip(".")
    if suffix not in preview_service.PREVIEWABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="This file type cannot be previewed.")

    if suffix in preview_service.TEXT_SUFFIXES:
        return preview_service.read_text_file(target)

    target = _lossless_source(target)
    suffix = target.suffix.lower().lstrip(".")
    if suffix in preview_service.FILE_PUSHDOWN_SUFFIXES:
        try:
            return preview_service.build_file_page(
                session_id,
                target,
                workspace,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=parsed_filters,
                include_stats=include_stats,
            )
        except table_adapters.AdapterError as exc:
            raise _adapter_http_error(exc)

    entry = preview_service.load_table_file(target)
    if include_stats and entry["stats"] is None:
        entry["stats"] = preview_service.compute_stats(entry["df"])
    return preview_service.build_table_page(
        entry["df"],
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        search=search,
        filters=parsed_filters,
        stats=entry["stats"] if include_stats else None,
        truncated=entry["truncated"],
    )


def _mongo_diff(base_ctx, target_ctx, base_variant: str, target_variant: str, **kwargs):
    """Compare two MongoDB collections. Both sides must be the same connection:
    a cross-connection comparison has no shared identity to align on."""
    from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415
    from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

    base_meta = base_ctx.metadata or {}
    target_meta = target_ctx.metadata or {}
    if target_meta.get("source") != "mongodb":
        raise HTTPException(
            status_code=400, detail="Cannot compare a MongoDB collection with another source."
        )
    if base_meta.get("connection_url") != target_meta.get("connection_url"):
        raise HTTPException(
            status_code=400, detail="Cannot compare collections from different connections."
        )
    try:
        runner = MongoScratchRunner.from_meta(target_meta)
        scratch = runner.temp_schema
        base_db, base_collection = mongo_diff_service.resolve_diff_target(
            base_meta, base_variant, scratch
        )
        target_db, target_collection = mongo_diff_service.resolve_diff_target(
            target_meta, target_variant, scratch
        )
        if base_db != target_db:
            raise HTTPException(
                status_code=400,
                detail="Comparing versions requires both sides in the scratch database.",
            )
        return mongo_diff_service.build_mongo_diff_page(
            runner,
            database=base_db,
            base_collection=base_collection,
            target_collection=target_collection,
            **kwargs,
        )
    except mongo_diff_service.MongoDiffError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except MongoRunnerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
