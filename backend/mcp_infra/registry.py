"""
Declarative MCP server registry.

Adding a new integration = one entry in MCP_REGISTRY.
No changes to the agent, orchestrator, or DB schema are needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional


@dataclass
class MCPServerDef:
    """Definition of a single MCP server that Stych can connect to."""

    server_id: str
    """Stable machine-readable identifier (e.g. 'google_sheets')."""

    name: str
    """Human-readable display name."""

    description: str
    """Short sentence describing what this integration does."""

    transport: Literal["http_sse", "http", "stdio"]
    """How to connect to the MCP server."""

    http_url: Optional[str] = None
    """Base URL for http_sse transport (e.g. 'https://mcp.tryopendata.ai/sse')."""

    stdio_cmd: Optional[List[str]] = None
    """Command + args for stdio transport (e.g. ['npx', '-y', '@modelcontextprotocol/server-gdrive'])."""

    required_credentials: List[str] = field(default_factory=list)
    """Credential keys the user must supply before the server can be used."""

    optional_credentials: List[str] = field(default_factory=list)
    """Credential keys that are accepted but not required."""

    auth_style: Optional[str] = None
    """
    How MCPClientManager injects credentials into the subprocess environment.
    "gsheets_home"    — mcp-google-sheets-full: override HOME with a per-user temp dir
                        containing gdrive-credentials.json + gdrive-token.json.
    "workspace_token" — @dguido/google-workspace-mcp: write a token JSON to a temp file
                        and set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                        GOOGLE_WORKSPACE_MCP_TOKEN_PATH env vars.
    None              — no special credential injection (e.g. HTTP-based servers).
    """

    workspace_services: Optional[str] = None
    """
    Comma-separated list of services to pass as GOOGLE_WORKSPACE_SERVICES to
    @dguido/google-workspace-mcp. Limits which tools are exposed to the agent.
    E.g. "docs,drive" — only show Docs and Drive tools, hide Sheets/Slides/Calendar/Gmail.
    Only used when auth_style == "workspace_token".
    """


MCP_REGISTRY: Dict[str, MCPServerDef] = {
    "google_sheets": MCPServerDef(
        server_id="google_sheets",
        name="Google Sheets",
        description=(
            "Read and write Google Sheets spreadsheets via your Google account. "
            "Ideal for exporting analysis results or pulling reference data from Sheets."
        ),
        transport="stdio",
        stdio_cmd=["npx", "-y", "mcp-google-sheets-full@0.2.0"],
        required_credentials=[],  # OAuth flow — no manual credentials needed
        auth_style="gsheets_home",
    ),
    "google_drive": MCPServerDef(
        server_id="google_drive",
        name="Google Drive",
        description=("Browse, search, upload, and manage files in your Google Drive."),
        transport="stdio",
        stdio_cmd=["npx", "-y", "@dguido/google-workspace-mcp@3.4.4"],
        required_credentials=[],  # OAuth flow — no manual credentials needed
        auth_style="workspace_token",
        workspace_services="drive",
    ),
    "google_docs": MCPServerDef(
        server_id="google_docs",
        name="Google Docs",
        description=("Create, read, and edit Google Docs documents."),
        transport="stdio",
        stdio_cmd=["npx", "-y", "@dguido/google-workspace-mcp@3.4.4"],
        required_credentials=[],  # OAuth flow — no manual credentials needed
        auth_style="workspace_token",
        workspace_services="docs,drive",
    ),
    "google_slides": MCPServerDef(
        server_id="google_slides",
        name="Google Slides",
        description=("Create and edit Google Slides presentations."),
        transport="stdio",
        stdio_cmd=["npx", "-y", "@dguido/google-workspace-mcp@3.4.4"],
        required_credentials=[],  # OAuth flow — no manual credentials needed
        auth_style="workspace_token",
        workspace_services="slides,drive",
    ),
    "opendata": MCPServerDef(
        server_id="opendata",
        name="TryOpenData",
        description=(
            "Search and retrieve public datasets from tryopendata.ai. "
            "Useful for enriching analyses with open government and research data."
        ),
        transport="http",
        http_url="https://mcp.tryopendata.ai/mcp",
        required_credentials=["api_key"],
        optional_credentials=[],
    ),
    "notion": MCPServerDef(
        server_id="notion",
        name="Notion",
        description=(
            "Read and write Notion pages, databases, and blocks. "
            "Export analysis results directly into your Notion workspace."
        ),
        transport="stdio",
        stdio_cmd=["npx", "-y", "@notionhq/notion-mcp-server@2.5.1"],
        required_credentials=["api_key"],
        optional_credentials=[],
        auth_style="notion_token",
    ),
    "yahoo_finance": MCPServerDef(
        server_id="yahoo_finance",
        name="Yahoo Finance",
        description=(
            "Fetch live and historical stock prices, financials, and market data "
            "to enrich your analyses with real-world market context."
        ),
        transport="stdio",
        stdio_cmd=["npx", "-y", "yahoo-finance-mcp-server@1.2.4"],
        required_credentials=[],
        optional_credentials=[],
        auth_style=None,
    ),
}
