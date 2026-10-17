"""
Metadata-backed registry of named working copies for a SQL DataContext.

A working copy is a full table in the ``stych_temp`` schema of the user's own
database. The registry lives in ``DataContext.metadata`` (persisted by the
session snapshot mechanism) and tracks every copy by name with its lineage:

    metadata["working_copies"] = [
        {"name": "working", "table": "stych_<sess>_<uuid>",
         "parent": "source", "created_at": "<iso>"},
        ...
    ]
    metadata["active_copy"] = "working"

The legacy ``temp_schema``/``temp_table`` keys are kept as a mirror of the
*active* copy so every existing reader (adapters, preview, diff, {table}
resolution) works unchanged; the registry is authoritative.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .runner import ROW_ID_COLUMN, TEMP_SCHEMA

COPIES_KEY = "working_copies"
ACTIVE_KEY = "active_copy"
# Sentinel parent meaning "branched from the user's source table".
SOURCE_PARENT = "source"
# Each copy is a full clone in the client database — cap runaway branching.
MAX_COPIES = 10

ROLE_KEY = "role"
WORKING_ROLE = "working"
BASE_ROLE = "base"
# Reserved name for the pinned snapshot taken when the source is first copied.
PINNED_BASE_NAME = "base"

__all__ = ["ROW_ID_COLUMN", "is_internal_column", "visible_columns"]


def is_internal_column(name: Any) -> bool:
    """True for columns Stych stamps onto working copies, not the user's data."""
    return str(name) == ROW_ID_COLUMN


def visible_columns(names: Any) -> list:
    """Drop Stych-internal columns from a user-facing column list.

    The stamped row id is how copies stay aligned for diffing; it is not the
    customer's data, so it must not appear in the grid, in a diff, in the viz
    field catalog, or in an exported file.
    """
    return [n for n in names if not is_internal_column(n)]


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")


def scratch_schema(meta: Dict[str, Any]) -> str:
    """Physical schema that holds working copies (Postgres/Databricks scratch)."""
    return str(meta.get("scratch_schema") or meta.get("temp_schema") or TEMP_SCHEMA)


def bind_scratch_schema(meta: Dict[str, Any], schema: str) -> bool:
    """Reconcile registry metadata with the runner's physical namespace.

    Warehouse identifiers can be case-sensitive.  In particular Snowflake's
    runner creates ``STYCH_TEMP`` while the connector-neutral fallback is
    ``stych_temp``.  Readers outside SQLAgent use the mirrored metadata, so the
    runner is authoritative whenever it is available.  Returns whether the
    metadata changed so callers can persist the repair.
    """
    physical = str(schema or "").strip()
    if not physical:
        raise WorkingCopyError("The scratch schema is unavailable.")
    changed = meta.get("scratch_schema") != physical
    meta["scratch_schema"] = physical
    if active_copy(meta) is not None and meta.get("temp_schema") != physical:
        meta["temp_schema"] = physical
        changed = True
    return changed


class WorkingCopyError(Exception):
    """Raised on invalid registry operations (bad name, duplicate, limit)."""


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise WorkingCopyError(
            f"Invalid working-copy name {name!r}: use 1-40 letters, digits, '-' or '_' "
            "(must start with a letter or digit)."
        )
    if name == SOURCE_PARENT:
        raise WorkingCopyError("'source' is reserved for the original table.")
    if name == PINNED_BASE_NAME:
        raise WorkingCopyError(
            f"{PINNED_BASE_NAME!r} is reserved for the pinned snapshot of the source."
        )
    return name


def list_copies(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    copies = meta.get(COPIES_KEY)
    return list(copies) if isinstance(copies, list) else []


def get_copy(meta: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for copy in list_copies(meta):
        if copy.get("name") == name:
            return copy
    return None


def active_copy(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = meta.get(ACTIVE_KEY)
    return get_copy(meta, name) if name else None


def active_source_variant(meta: Dict[str, Any]) -> str:
    """Canonical preview-source variant for the active SQL dataset.

    Named copies have stable identities in the Data tab.  ``temp`` remains the
    compatibility alias only for sessions created before the copy registry.
    """
    active = active_copy(meta)
    if active is not None:
        return f"copy:{active['name']}"
    if meta.get("temp_schema") and meta.get("temp_table"):
        return "temp"
    return "base"


def default_name(meta: Dict[str, Any]) -> str:
    """'working' for the first copy, then 'copy-2', 'copy-3', …"""
    taken = {c.get("name") for c in list_copies(meta)}
    if "working" not in taken:
        return "working"
    n = 2
    while f"copy-{n}" in taken:
        n += 1
    return f"copy-{n}"


def _mirror_legacy_keys(meta: Dict[str, Any]) -> None:
    """Keep temp_schema/temp_table pointing at the active copy (or absent)."""
    active = active_copy(meta)
    if active:
        meta["temp_schema"] = scratch_schema(meta)
        meta["temp_table"] = active["table"]
    else:
        meta.pop("temp_schema", None)
        meta.pop("temp_table", None)


def pinned_base(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The immutable snapshot of the source, if one was taken."""
    entry = get_copy(meta, PINNED_BASE_NAME)
    return entry if entry and entry.get(ROLE_KEY) == BASE_ROLE else None


def is_base(entry: Optional[Dict[str, Any]]) -> bool:
    return bool(entry) and entry.get(ROLE_KEY) == BASE_ROLE


def register_copy(
    meta: Dict[str, Any],
    *,
    name: str,
    table: str,
    parent: str = SOURCE_PARENT,
    role: str = WORKING_ROLE,
) -> Dict[str, Any]:
    """Add a copy to the registry. Working copies become active; the pinned
    base never does — it is a reference point, not a place to write."""
    if role == BASE_ROLE:
        if name != PINNED_BASE_NAME:
            raise WorkingCopyError(f"The pinned base must be named {PINNED_BASE_NAME!r}.")
    else:
        name = validate_name(name)
    copies = list_copies(meta)
    if any(c.get("name") == name for c in copies):
        raise WorkingCopyError(f"A working copy named {name!r} already exists.")
    if len(copies) >= MAX_COPIES:
        raise WorkingCopyError(
            f"Working-copy limit reached ({MAX_COPIES}). Drop one with drop_working_copy first."
        )
    entry = {
        "name": name,
        "table": table,
        "parent": parent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        ROLE_KEY: role,
    }
    copies.append(entry)
    meta[COPIES_KEY] = copies
    if role != BASE_ROLE:
        meta[ACTIVE_KEY] = name
    _mirror_legacy_keys(meta)
    return entry


def set_active(meta: Dict[str, Any], name: str) -> Dict[str, Any]:
    entry = get_copy(meta, name)
    if entry is None:
        raise WorkingCopyError(f"No working copy named {name!r}.")
    if is_base(entry):
        raise WorkingCopyError(
            f"{name!r} is the pinned snapshot of the source and is read-only. "
            "Branch a copy from it instead."
        )
    meta[ACTIVE_KEY] = name
    _mirror_legacy_keys(meta)
    return entry


def remove_copy(meta: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Remove a copy from the registry. If it was active, the most recently
    created remaining copy becomes active (or none). Returns the removed entry."""
    entry = get_copy(meta, name)
    if entry is None:
        raise WorkingCopyError(f"No working copy named {name!r}.")
    copies = [c for c in list_copies(meta) if c.get("name") != name]
    meta[COPIES_KEY] = copies
    if meta.get(ACTIVE_KEY) == name:
        writable = [c for c in copies if not is_base(c)]
        meta[ACTIVE_KEY] = writable[-1]["name"] if writable else None
        if meta[ACTIVE_KEY] is None:
            meta.pop(ACTIVE_KEY, None)
    _mirror_legacy_keys(meta)
    return entry


def adopt_legacy(meta: Dict[str, Any]) -> None:
    """Migrate a pre-registry session: temp_schema/temp_table set but no
    registry → adopt the existing table as the copy named 'working'."""
    if list_copies(meta):
        return
    table = meta.get("temp_table")
    if meta.get("temp_schema") and table:
        meta[COPIES_KEY] = [
            {
                "name": "working",
                "table": table,
                "parent": SOURCE_PARENT,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        meta[ACTIVE_KEY] = "working"
