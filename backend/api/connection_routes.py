"""User-level saved datasource connections and pinned tables."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from agents.state import state_manager
from api.connection_attach import attach_pin
from api.connection_dispatch import (
    default_connection_name,
    hint_for,
    list_namespaces,
    list_remote,
    test_and_normalize,
)
from api.connection_schemas import (
    AttachTableRequest,
    ConnectionDeleteResponse,
    ConnectionUsageResponse,
    ConnectorCatalogEntry,
    ConnectorCatalogResponse,
    CreateConnectionRequest,
    NamespaceDiscoveryResponse,
    PatchConnectionRequest,
    PinTableRequest,
    RemoteTablesResponse,
    SavedConnection,
    SavedConnectionList,
    SavedTable,
)
from api.dependencies import get_config, get_current_user
from api.source_lifecycle import collect_connection_usage, retire_connection_attachments
from database.connection import get_db
from database.connection_store import (
    CONNECTOR_IDS,
    connection_to_dict,
    create_connection,
    delete_connection,
    get_connection,
    get_pinned_table,
    list_connections,
    load_credentials,
    merge_connection_update,
    pin_table,
    record_test_result,
    table_to_dict,
    touch_last_used,
    unpin_table,
    update_connection,
)
from datasources.contracts import Capability
from datasources.registry import get_manifest
from helpers.storage import storage_manager

router = APIRouter(prefix="/api/connections", tags=["connections"])


def _owned(db: Session, user: str, connection_id: str):
    row = get_connection(db, user, connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return row


def _credentials(row):
    try:
        return load_credentials(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=SavedConnectionList)
async def list_saved_connections(
    connector_id: str | None = None,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if connector_id and connector_id not in CONNECTOR_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown connector '{connector_id}'")
    rows = list_connections(db, user, connector_id)
    return SavedConnectionList(connections=[SavedConnection(**connection_to_dict(r)) for r in rows])


@router.post("", response_model=SavedConnection)
async def create_saved_connection(
    request: CreateConnectionRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if request.connector_id not in CONNECTOR_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown connector '{request.connector_id}'")
    try:
        credentials = await test_and_normalize(
            request.connector_id, request.connection, config=config, user=user, db=db
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    hint = hint_for(credentials)
    name = (request.name or "").strip() or default_connection_name(request.connector_id, hint)
    row = create_connection(
        db,
        app_user_id=user,
        connector_id=request.connector_id,
        name=name,
        credentials=credentials,
        hint=hint,
        description=request.description,
        tags=request.tags,
    )
    return SavedConnection(**connection_to_dict(row))


# Vendor facet for the connectors UI. The manifest carries category/maturity but
# not the operator that runs the service, which is what users actually filter by
# ("show me our GCP connectors").
_VENDORS = {
    "postgres": "postgresql",
    "sqlserver": "microsoft",
    "mongodb": "mongodb",
    "snowflake": "snowflake",
    "redshift": "aws",
    "bigquery": "gcp",
    "databricks": "databricks",
}

# Per-connector vocabulary. Previously hardcoded per call site in the frontend.
_VOCAB = {
    "postgres": ("Schema", "public", "table"),
    "sqlserver": ("Schema", "dbo", "table"),
    "mongodb": ("Database", "admin", "collection"),
    "snowflake": ("Schema", "PUBLIC", "table"),
    "redshift": ("Schema", "public", "table"),
    "bigquery": ("Dataset", "", "table"),
    "databricks": ("Schema", "default", "table"),
}


@router.get("/catalog", response_model=ConnectorCatalogResponse)
async def get_connector_catalog(
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The connector *types* this deployment ships, with per-user configured counts.

    Serving this lets the UI drop its hardcoded provider list and pick up new
    connectors (and the server-side per-connector kill switch) automatically.
    """
    configured: dict[str, int] = {}
    for row in list_connections(db, user):
        configured[row.connector_id] = configured.get(row.connector_id, 0) + 1

    datasource_config = (config or {}).get("datasources", {}) or {}
    entries: list[ConnectorCatalogEntry] = []
    for connector_id in sorted(CONNECTOR_IDS):
        try:
            manifest = get_manifest(connector_id)
        except KeyError:
            continue
        schema_label, default_schema, relation_noun = _VOCAB.get(
            connector_id, ("Schema", "", "table")
        )
        entries.append(
            ConnectorCatalogEntry(
                id=connector_id,
                label=manifest.display_name,
                category=manifest.category,
                vendor=_VENDORS.get(connector_id, "other"),
                maturity=manifest.maturity.value,
                summary=manifest.user_summary,
                enabled=bool((datasource_config.get(connector_id) or {}).get("enabled", True)),
                schema_label=schema_label,
                default_schema=default_schema,
                relation_noun=relation_noun,
                configured_count=configured.get(connector_id, 0),
                same_source_join=(
                    {
                        "strategies": [
                            item.model_dump(mode="json")
                            for item in manifest.capabilities[
                                Capability.SAME_SOURCE_JOIN
                            ].join.strategies
                        ],
                        "join_types": list(
                            manifest.capabilities[Capability.SAME_SOURCE_JOIN].join.join_types
                        ),
                        "caveats": list(manifest.capabilities[Capability.SAME_SOURCE_JOIN].caveats),
                    }
                    if manifest.capabilities[Capability.SAME_SOURCE_JOIN].join
                    else None
                ),
            )
        )
    return ConnectorCatalogResponse(connectors=entries)


@router.get("/{connection_id}", response_model=SavedConnection)
async def get_saved_connection(
    connection_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return SavedConnection(**connection_to_dict(_owned(db, user, connection_id)))


@router.patch("/{connection_id}", response_model=SavedConnection)
async def patch_saved_connection(
    connection_id: str,
    request: PatchConnectionRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    credentials = None
    hint = None
    if request.connection is not None:
        merged = merge_connection_update(_credentials(row), request.connection)
        try:
            credentials = await test_and_normalize(
                row.connector_id, merged, config=config, user=user, db=db
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        hint = hint_for(credentials)
    row = update_connection(
        db,
        row,
        name=request.name,
        credentials=credentials,
        hint=hint,
        description=request.description,
        tags=request.tags,
    )
    return SavedConnection(**connection_to_dict(row))


@router.post("/{connection_id}/test")
async def test_saved_connection(
    connection_id: str,
    request: PatchConnectionRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    merged = merge_connection_update(_credentials(row), request.connection or {})
    try:
        normalized = await test_and_normalize(
            row.connector_id, merged, config=config, user=user, db=db
        )
    except ValidationError as exc:
        record_test_result(db, row, ok=False, error="Invalid connection parameters")
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except HTTPException as exc:
        # A failed test is a recorded outcome, not just a rejected request — the
        # connectors list shows this as an unhealthy connection.
        record_test_result(db, row, ok=False, error=str(exc.detail))
        raise
    # Rebuild redacted endpoint metadata from the successfully normalized
    # credentials. This also repairs hints saved by older versions that let a
    # component default overwrite the actual connection-URL port.
    update_connection(db, row, hint=hint_for(normalized))
    record_test_result(db, row, ok=True)
    return {"success": True, "message": "Connection successful"}


@router.delete("/{connection_id}", response_model=ConnectionDeleteResponse)
async def delete_saved_connection(
    connection_id: str,
    confirm: bool = Query(False),
    etag: str | None = Query(None),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    usage = collect_connection_usage(db, user, connection_id)
    if usage.attachments:
        if not confirm:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "confirm_required",
                    "message": "Confirm deletion to detach this connection from every chat that uses it.",
                    **usage.to_payload(),
                },
            )
        if etag != usage.etag:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_usage",
                    "message": "This connection's usage changed. Refresh and confirm again.",
                    **usage.to_payload(),
                },
            )
    detached = retire_connection_attachments(db, user, usage.attachments)
    delete_connection(db, row)
    for att in detached:
        session = state_manager.get_session(att.session_id, app_user_id=user)
        if session is None:
            continue
        try:
            if session.get_all_data_contexts():
                await state_manager.snapshot_data_async(session, raise_on_error=True)
            else:
                storage_manager.delete_session_snapshots(att.session_id)
            state_manager.mark_snapshot_clean(att.session_id)
        except Exception:
            state_manager.mark_dirty(att.session_id)
    return ConnectionDeleteResponse(
        ok=True,
        detached=[
            {
                "session_id": att.session_id,
                "file_id": att.file_id,
                "filename": att.filename,
            }
            for att in detached
        ],
    )


@router.get("/{connection_id}/usage", response_model=ConnectionUsageResponse)
async def get_saved_connection_usage(
    connection_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned(db, user, connection_id)
    return ConnectionUsageResponse(**collect_connection_usage(db, user, connection_id).to_payload())


@router.get("/{connection_id}/remote-tables", response_model=RemoteTablesResponse)
async def list_remote_tables(
    connection_id: str,
    schema: str,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    credentials = _credentials(row)
    schema_name, tables, collections = await list_remote(
        row.connector_id, credentials, schema, config=config, user=user, db=db
    )
    return RemoteTablesResponse(schema=schema_name, tables=tables, collections=collections)


@router.get("/{connection_id}/namespaces", response_model=NamespaceDiscoveryResponse)
async def list_remote_namespaces(
    connection_id: str,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    default, namespaces, complete, warning = await list_namespaces(
        row.connector_id,
        _credentials(row),
        config=config,
        user=user,
        db=db,
    )
    return NamespaceDiscoveryResponse(
        default_namespace=default,
        namespaces=namespaces,
        complete=complete,
        warning=warning,
    )


@router.get("/{connection_id}/tables", response_model=list[SavedTable])
async def list_pinned_tables(
    connection_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    return [SavedTable(**table_to_dict(t)) for t in row.tables]


@router.post("/{connection_id}/tables", response_model=SavedTable)
async def pin_saved_table(
    connection_id: str,
    request: PinTableRequest,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    display = (
        request.display_name
        or request.table_name
        or ("query" if request.custom_query else None)
        or "table"
    )
    try:
        pin = pin_table(
            db,
            row,
            schema_name=request.schema_name,
            table_name=request.table_name,
            custom_query=request.custom_query,
            display_name=display,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SavedTable(**table_to_dict(pin))


@router.delete("/{connection_id}/tables/{table_id}")
async def unpin_saved_table(
    connection_id: str,
    table_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    pin = get_pinned_table(db, row, table_id)
    if pin is None:
        raise HTTPException(status_code=404, detail="Pinned table not found")
    unpin_table(db, pin)
    return {"ok": True}


@router.post("/{connection_id}/tables/{table_id}/attach")
async def attach_saved_table(
    connection_id: str,
    table_id: str,
    request: AttachTableRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    pin = get_pinned_table(db, row, table_id)
    if pin is None:
        raise HTTPException(status_code=404, detail="Pinned table not found")
    result = await attach_pin(
        connector_id=row.connector_id,
        credentials=_credentials(row),
        schema=pin.schema_name,
        table=pin.table_name,
        query=pin.custom_query,
        session_id=request.session_id,
        connection_id=row.id,
        config=config,
        user=user,
        db=db,
    )
    touch_last_used(db, row)
    return result


@router.post("/{connection_id}/attach")
async def attach_connection_table(
    connection_id: str,
    request: AttachTableRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, connection_id)
    result = await attach_pin(
        connector_id=row.connector_id,
        credentials=_credentials(row),
        schema=request.schema_name,
        table=request.table_name,
        query=request.custom_query,
        session_id=request.session_id,
        connection_id=row.id,
        config=config,
        user=user,
        db=db,
    )
    touch_last_used(db, row)
    return result
