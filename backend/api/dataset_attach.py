"""Identity for a connector table already attached to a chat session."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import HTTPException


def _relation(meta: Mapping[str, Any]) -> str:
    query = str(meta.get("custom_query") or "").strip()
    if query:
        return query
    schema = str(meta.get("schema") or "")
    table = str(meta.get("table") or meta.get("collection") or "")
    if not schema and meta.get("collection"):
        schema = str(meta.get("database") or "")
    if not table:
        return ""
    return f"{schema}.{table}" if schema else table


def is_same_attached_relation(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    if str(existing.get("source") or "") != str(incoming.get("source") or ""):
        return False
    left, right = _relation(existing), _relation(incoming)
    if not left or left != right:
        return False
    left_id, right_id = existing.get("connection_id"), incoming.get("connection_id")
    if left_id and right_id:
        return left_id == right_id
    left_db, right_db = existing.get("database"), incoming.get("database")
    if left_db and right_db and left_db != right_db:
        return False
    left_url, right_url = existing.get("connection_url"), incoming.get("connection_url")
    if left_url and right_url and left_url != right_url:
        return False
    return True


def iter_session_contexts(session: Any) -> list[Any]:
    getter = getattr(session, "get_all_data_contexts", None)
    if callable(getter):
        return list(getter() or [])
    out: list[Any] = []
    primary = getattr(session, "data_context", None)
    if primary is not None:
        out.append(primary)
    out.extend(getattr(session, "additional_data_contexts", None) or [])
    out.extend(getattr(session, "contexts", None) or [])
    seen: set[int] = set()
    unique: list[Any] = []
    for ctx in out:
        ident = id(ctx)
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(ctx)
    return unique


def find_attached_relation(session: Any, incoming: Mapping[str, Any]) -> Optional[Any]:
    if not _relation(incoming):
        return None
    for ctx in iter_session_contexts(session):
        meta = getattr(ctx, "metadata", None) or {}
        if is_same_attached_relation(meta, incoming):
            return ctx
    return None


def reject_if_already_attached(session: Any, incoming: Mapping[str, Any], label: str) -> None:
    if find_attached_relation(session, incoming) is None:
        return
    raise HTTPException(status_code=409, detail=f"{label} is already in this chat.")
