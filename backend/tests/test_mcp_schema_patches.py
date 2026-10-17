"""Tests for MCP tool schema patches and list_mcp_tools formatting."""

from mcp_infra.schema_patches import TOOL_SCHEMA_PATCHES, apply_tool_schema_patches
from tools.mcp_tools import _format_tool_params, handle_list_mcp_tools


def test_drive_search_patch_requires_non_empty_query():
    tools = [
        {
            "name": "search",
            "description": "Search files and folders in Drive.",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "searchType": {
                        "type": "string",
                        "enum": ["fulltext", "name", "name_exact"],
                    },
                },
            },
        }
    ]

    patched = apply_tool_schema_patches("google_drive", tools)[0]

    assert patched["stych_enrich_params"] is True
    assert "never pass an empty string" in patched["description"]
    query_desc = patched["inputSchema"]["properties"]["query"]["description"]
    assert "Required non-empty" in query_desc
    assert "Use '*'" in query_desc
    assert "list_spreadsheets" in query_desc


def test_google_sheets_list_and_search_patches():
    tools = [
        {
            "name": "list_spreadsheets",
            "description": "List Google Sheets files in the user's Drive.",
            "inputSchema": {
                "properties": {
                    "page_size": {"type": "number", "description": "Max results."},
                },
            },
        },
        {
            "name": "search_spreadsheets",
            "description": "Search spreadsheets in the user's Drive.",
            "inputSchema": {
                "properties": {
                    "name_contains": {"type": "string", "description": "Substring."},
                    "full_text": {"type": "string", "description": "Full text."},
                },
            },
        },
    ]

    patched = {t["name"]: t for t in apply_tool_schema_patches("google_sheets", tools)}

    assert "no query parameter required" in patched["list_spreadsheets"]["description"]
    assert patched["list_spreadsheets"]["stych_enrich_params"] is True
    search = patched["search_spreadsheets"]
    assert "list_spreadsheets" in search["description"]
    assert (
        "at least one is required"
        in search["inputSchema"]["properties"]["name_contains"]["description"]
    )


def test_google_slides_gets_search_and_create_patches():
    tools = [
        {"name": "search", "description": "Search", "inputSchema": {"properties": {"query": {}}}},
        {
            "name": "create_google_slides",
            "description": "Create slides",
            "inputSchema": {"properties": {"name": {"type": "string", "description": "Name"}}},
        },
    ]

    patched = {t["name"]: t for t in apply_tool_schema_patches("google_slides", tools)}

    assert "non-empty" in patched["search"]["description"]
    assert (
        "not title"
        in patched["create_google_slides"]["inputSchema"]["properties"]["name"]["description"]
    )


def test_opendata_search_uses_queries_param_name():
    tools = [
        {
            "name": "search_datasets",
            "description": "Search datasets",
            "inputSchema": {
                "required": ["queries"],
                "properties": {"queries": {"type": "string", "description": "Search"}},
            },
        }
    ]

    patched = apply_tool_schema_patches("opendata", tools)[0]
    desc = patched["inputSchema"]["properties"]["queries"]["description"]
    assert "queries (not query)" in desc


def test_patch_skipped_for_unregistered_server():
    tools = [{"name": "search", "description": "orig", "inputSchema": {}}]
    assert apply_tool_schema_patches("notion", tools) == tools


def test_all_registered_servers_have_patches():
    assert set(TOOL_SCHEMA_PATCHES) == {
        "google_drive",
        "google_docs",
        "google_slides",
        "google_sheets",
        "opendata",
    }


def test_format_tool_params_shows_descriptions_when_enriched():
    schema = {
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": "Plain text only — not mimeType filters.",
            },
            "searchType": {
                "type": "string",
                "enum": ["fulltext", "name"],
                "description": "Controls how query is interpreted.",
            },
        },
    }

    plain = _format_tool_params(schema)
    enriched = _format_tool_params(schema, enrich_param_descriptions=True)

    assert "Plain text only" not in plain
    assert "Plain text only" in enriched
    assert "Controls how query" in enriched


async def test_list_mcp_tools_renders_patched_search_tool():
    class FakeClient:
        async def list_tools(self, server_def, creds):
            return apply_tool_schema_patches(
                server_def.server_id,
                [
                    {
                        "name": "search",
                        "description": "Search files and folders in Drive.",
                        "inputSchema": {
                            "type": "object",
                            "required": ["query"],
                            "properties": {
                                "query": {"type": "string", "description": "Search query"},
                                "searchType": {
                                    "type": "string",
                                    "enum": ["fulltext", "name", "name_exact"],
                                },
                            },
                        },
                    }
                ],
            )

    from mcp_infra.registry import MCP_REGISTRY

    out = await handle_list_mcp_tools(
        {"server_id": "google_drive"},
        {"google_drive": {"access_token": "x", "refresh_token": "y"}},
        FakeClient(),
    )

    assert "search(" in out
    assert "never pass an empty string" in out
    assert "Required non-empty" in out
    assert MCP_REGISTRY["google_drive"].name in out


async def test_list_mcp_tools_renders_google_sheets_list_tool():
    class FakeClient:
        async def list_tools(self, server_def, creds):
            return apply_tool_schema_patches(
                server_def.server_id,
                [
                    {
                        "name": "list_spreadsheets",
                        "description": "List Google Sheets files in the user's Drive.",
                        "inputSchema": {
                            "properties": {
                                "page_size": {"type": "number", "description": "Max results."},
                            },
                        },
                    }
                ],
            )

    out = await handle_list_mcp_tools(
        {"server_id": "google_sheets"},
        {"google_sheets": {"access_token": "x", "refresh_token": "y"}},
        FakeClient(),
    )

    assert "list_spreadsheets(" in out
    assert "no query parameter required" in out
