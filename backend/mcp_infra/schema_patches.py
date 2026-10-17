"""
Targeted overrides for MCP tool schemas before they reach the integration agent.

Third-party MCP servers sometimes expose misleading parameter descriptions
(e.g. Drive search accepting a "query" that is not raw Drive API q syntax).
Patch here instead of bloating the integration agent system prompt.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

# google_docs / google_drive / google_slides all use @dguido/google-workspace-mcp.
_WORKSPACE_SERVER_IDS = frozenset({"google_docs", "google_drive", "google_slides"})

_MIME_FILTER_HINT = (
    "Filter results by mimeType (spreadsheet: application/vnd.google-apps.spreadsheet, "
    "doc: application/vnd.google-apps.document, slides: "
    "application/vnd.google-apps.presentation)."
)

_WORKSPACE_SEARCH_PATCH: Dict[str, Any] = {
    "description": (
        "Search Drive files. query is required and must be non-empty — use '*' to list "
        "broadly; never pass an empty string. Not raw Drive API q syntax."
    ),
    "stych_enrich_params": True,
    "inputSchema": {
        "properties": {
            "query": {
                "description": (
                    "Required non-empty string. Use '*' to list many files (empty string "
                    "is rejected). Plain text only — default searchType wraps as fullText "
                    f"contains '…'. Not Drive q syntax. {_MIME_FILTER_HINT} "
                    "For all spreadsheets, prefer google_sheets list_spreadsheets instead."
                ),
            },
            "searchType": {
                "description": (
                    "fulltext (default): search file content. name: filename contains. "
                    "name_exact: exact filename. No mimeType filter mode — filter the "
                    "returned files list instead."
                ),
            },
        },
    },
}

_WORKSPACE_LIST_FOLDER_PATCH: Dict[str, Any] = {
    "description": (
        "List files in a Drive folder without a search query. Use to browse a folder "
        "when you do not have a search term — omit folderId for root."
    ),
}

_WORKSPACE_CREATE_NAME_PATCH: Dict[str, Any] = {
    "stych_enrich_params": True,
    "inputSchema": {
        "properties": {
            "name": {
                "description": "Required title (parameter name is name, not title).",
            },
        },
    },
}

_GOOGLE_SHEETS_PATCHES: Dict[str, Dict[str, Any]] = {
    "list_spreadsheets": {
        "description": (
            "List all Google Sheets in Drive (most recent first). Prefer this over "
            "google_drive search when listing spreadsheets — no query parameter required."
        ),
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "page_size": {
                    "description": "Max results (1-1000, default 50). Increase to list more.",
                },
            },
        },
    },
    "search_spreadsheets": {
        "description": (
            "Search spreadsheets by title substring and/or content. Cannot list all "
            "sheets — use list_spreadsheets for that."
        ),
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "name_contains": {
                    "description": (
                        "Non-empty substring of spreadsheet title. Provide this and/or "
                        "full_text — at least one is required."
                    ),
                },
                "full_text": {
                    "description": (
                        "Non-empty full-text search term. Provide this and/or "
                        "name_contains — at least one is required."
                    ),
                },
            },
        },
    },
}

_OPENDATA_PATCHES: Dict[str, Dict[str, Any]] = {
    "search_datasets": {
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "queries": {
                    "description": (
                        "Required. Search term(s) — parameter name is queries (not query). "
                        "String or array of strings."
                    ),
                },
            },
        },
    },
    "query_sql": {
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "sql": {
                    "description": (
                        "Required non-empty SQL (SELECT … FROM provider.dataset …). "
                        "Use fetch_opendata_to_workspace for large exports to the workspace."
                    ),
                },
            },
        },
    },
    "get_dataset_info": {
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "provider": {
                    "description": "Required provider slug from search_datasets results.",
                },
                "dataset": {
                    "description": "Required dataset slug from search_datasets results.",
                },
            },
        },
    },
    "query_dataset": {
        "stych_enrich_params": True,
        "inputSchema": {
            "properties": {
                "provider": {
                    "description": "Required provider slug from search_datasets results.",
                },
                "dataset": {
                    "description": "Required dataset slug from search_datasets results.",
                },
            },
        },
    },
}


def _workspace_patches() -> Dict[str, Dict[str, Any]]:
    return {
        "search": deepcopy(_WORKSPACE_SEARCH_PATCH),
        "list_folder": deepcopy(_WORKSPACE_LIST_FOLDER_PATCH),
    }


def _google_docs_patches() -> Dict[str, Dict[str, Any]]:
    patches = _workspace_patches()
    patches["create_google_doc"] = deepcopy(_WORKSPACE_CREATE_NAME_PATCH)
    return patches


def _google_slides_patches() -> Dict[str, Dict[str, Any]]:
    patches = _workspace_patches()
    patches["create_google_slides"] = deepcopy(_WORKSPACE_CREATE_NAME_PATCH)
    return patches


# server_id → tool_name → patch dict (deep-merged into the MCP tool schema)
TOOL_SCHEMA_PATCHES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "google_drive": _workspace_patches(),
    "google_docs": _google_docs_patches(),
    "google_slides": _google_slides_patches(),
    "google_sheets": deepcopy(_GOOGLE_SHEETS_PATCHES),
    "opendata": deepcopy(_OPENDATA_PATCHES),
}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if key == "inputSchema" and isinstance(value, dict):
            schema = out.setdefault("inputSchema", {})
            if not isinstance(schema, dict):
                schema = {}
                out["inputSchema"] = schema
            for sk, sv in value.items():
                if sk == "properties" and isinstance(sv, dict):
                    props = schema.setdefault("properties", {})
                    if not isinstance(props, dict):
                        props = {}
                        schema["properties"] = props
                    for pk, pv in sv.items():
                        if isinstance(pv, dict) and isinstance(props.get(pk), dict):
                            props[pk] = {**props[pk], **pv}
                        else:
                            props[pk] = deepcopy(pv)
                else:
                    if isinstance(schema.get(sk), dict) and isinstance(sv, dict):
                        schema[sk] = {**schema[sk], **sv}
                    else:
                        schema[sk] = deepcopy(sv)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **deepcopy(value)}
        else:
            out[key] = deepcopy(value)
    return out


def apply_tool_schema_patches(server_id: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return tools with any registered schema patches applied."""
    patches = TOOL_SCHEMA_PATCHES.get(server_id)
    if not patches:
        return tools
    return [
        _deep_merge(tool, patches[tool["name"]]) if tool.get("name") in patches else tool
        for tool in tools
    ]
