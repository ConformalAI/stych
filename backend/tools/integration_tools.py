"""
Tool schemas (OpenAI function-calling format) and system prompt for the IntegrationAgent.
"""

from __future__ import annotations

_INTEGRATION_TOOLS = [
    # ── Workspace tools (read data produced by analysis/cleaning agents) ───
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": (
                "List all files saved in the analysis workspace for this session. "
                "Call this first when the user wants to export data — the analysis or "
                "cleaning agent will have saved the result as a CSV or text file here."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": (
                "Read the contents of a workspace file (CSV, JSON, text). "
                "Use this to load the data you want to export to an external service. "
                "Returns up to 20,000 characters per call; pass offset to page "
                "through larger files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filename or relative path within the workspace.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from (default 0).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    # ── TryOpenData bulk fetch (bypasses LLM context entirely) ─────────────
    {
        "type": "function",
        "function": {
            "name": "fetch_opendata_to_workspace",
            "description": (
                "Download a TryOpenData dataset directly to the workspace as a CSV file. "
                "The actual data rows NEVER pass through the LLM context. "
                "Use this instead of call_mcp_tool(query_sql) whenever you want to save data "
                "for subsequent analysis. "
                "IMPORTANT: queries without a LIMIT clause are automatically checked first — "
                "if the dataset exceeds 100,000 rows the tool returns an error and nothing is "
                "downloaded. For large datasets add a LIMIT clause or advise the user to upload "
                "a pre-filtered file. "
                "Step 1: use search_datasets / get_dataset_info to find the dataset and schema. "
                "Step 2: call this tool with the SQL and a sensible filename. "
                "Step 3: tell the user the file is ready and how many rows were saved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "SQL SELECT query against TryOpenData. "
                            'Use the table format: SELECT col1, col2 FROM "provider/dataset" WHERE ... '
                            "Omit LIMIT/OFFSET for full downloads (pagination is automatic). "
                            "Add LIMIT only when you deliberately want a small sample."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Output filename for the workspace CSV, e.g. 'worldbank_gdp.csv'. "
                            "Will overwrite any existing file with the same name."
                        ),
                    },
                },
                "required": ["sql", "filename"],
            },
        },
    },
    # ── Google Sheets direct CSV export (bypasses LLM context entirely) ────
    {
        "type": "function",
        "function": {
            "name": "write_csv_to_spreadsheet",
            "description": (
                "Write a workspace CSV file directly to a Google Sheets spreadsheet. "
                "Reads the file server-side — the actual data rows never pass through "
                "the LLM context, so arbitrarily large CSVs are handled correctly. "
                "If no spreadsheet_id is given, creates a new spreadsheet automatically. "
                "ALWAYS call this instead of read_workspace_file + write_values when "
                "exporting workspace data to Google Sheets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the CSV file in the workspace (e.g. 'iris_flowers.csv').",
                    },
                    "spreadsheet_id": {
                        "type": "string",
                        "description": (
                            "Existing Google Sheets spreadsheet ID to write into. "
                            "Omit to create a new spreadsheet automatically."
                        ),
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "Sheet tab name to write to (default: 'Sheet1').",
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Title for the new spreadsheet when creating one. "
                            "Ignored if spreadsheet_id is provided."
                        ),
                    },
                },
                "required": ["filename"],
            },
        },
    },
    # ── Google Sheets direct import to workspace (bypasses LLM context) ────
    {
        "type": "function",
        "function": {
            "name": "read_spreadsheet_to_workspace",
            "description": (
                "Pull a Google Sheet INTO the session workspace as a CSV file. "
                "Reads the sheet server-side — the actual data rows never pass through "
                "the LLM context, so arbitrarily large sheets are handled correctly. "
                "ALWAYS use this instead of call_mcp_tool(get_values) when the user wants "
                "to save, import, or analyze a Google Sheet. Set as_active_dataset=true when "
                "the user wants to analyze/chat with the sheet (makes it the active dataset); "
                "leave it false to just drop the CSV alongside any existing dataset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spreadsheet_url_or_id": {
                        "type": "string",
                        "description": (
                            "Google Sheets share URL or the bare spreadsheet ID "
                            "(e.g. from 'https://docs.google.com/spreadsheets/d/SHEET_ID/edit')."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": "Output CSV filename in the workspace (e.g. 'q3_sales.csv').",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": (
                            "Tab name to read (e.g. 'Sheet1'). Omit to read the first tab."
                        ),
                    },
                    "range": {
                        "type": "string",
                        "description": (
                            "Optional A1 range (e.g. 'Sheet1!A1:F500'). Omit to read the whole tab."
                        ),
                    },
                    "as_active_dataset": {
                        "type": "boolean",
                        "description": (
                            "When true, the pulled sheet becomes the session's active dataset "
                            "(canonical data.parquet) so the analysis/cleaning agents can use it. "
                            "Default false — just saves a CSV file."
                        ),
                    },
                },
                "required": ["spreadsheet_url_or_id", "filename"],
            },
        },
    },
    # ── MCP discovery + execution tools ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": (
                "List all available MCP servers and whether the user has connected them. "
                "Always call this before calling any MCP tool so you know what integrations "
                "are available and which are connected."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mcp_tools",
            "description": (
                "Fetch the tool schemas (name, description, input schema) exposed by a specific "
                "MCP server. Call this before calling any tool on a server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "The server_id from list_mcp_servers (e.g. 'google_sheets').",
                    }
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_mcp_tool",
            "description": (
                "Call a specific tool on an MCP server. "
                "Always call list_mcp_tools first to discover the correct tool names and parameters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "The server_id of the MCP server.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "The tool name exactly as returned by list_mcp_tools.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool.",
                    },
                },
                "required": ["server_id", "tool_name", "arguments"],
            },
        },
    },
]

_SYSTEM_PROMPT = """\
You are the integration agent for Stych, a data-analysis platform. You connect
analysis results to external services (Google Sheets, Docs, Slides, Drive,
TryOpenData, and others) through MCP servers.

Workflow:
1. Call list_mcp_servers to see what's connected. If the server the task needs is
   not_connected or connected_missing_credentials, stop and tell the user to fix it
   in Settings → Integrations.
2. Call list_mcp_tools(server_id) to get that server's exact tool names and
   parameter schemas. Then call_mcp_tool with arguments that match the schema
   exactly — use the parameter names the schema gives you (e.g. some tools name a
   title field "name", others "title"). Never assume a parameter name; the schema
   from list_mcp_tools is the source of truth.
3. When the task is done or cannot proceed, reply in plain text with no tool call.
   Always include the resulting document / sheet / file URL so the user can open it.

Working with the user's data:
- The analysis and cleaning agents save results as CSV files in the session
  workspace. Use list_workspace_files and read_workspace_file to inspect them.
- Never create an empty sheet or doc — populate it with the actual data.

Native shortcuts (prefer these for bulk data — they bypass the LLM context entirely):
- write_csv_to_spreadsheet(filename, ...): export a workspace CSV straight to Google
  Sheets server-side. Use it INSTEAD of reading the CSV and calling Sheets tools
  yourself — it handles any file size. Don't read the file first.
- fetch_opendata_to_workspace(sql, filename): download a TryOpenData query straight
  to a workspace CSV. Use it INSTEAD of call_mcp_tool(query_sql) for any data you
  want to keep. Queries over 100,000 rows are rejected — add a LIMIT or ask the user
  to upload a pre-filtered file. Use search_datasets / get_dataset_info first to find
  the dataset and its schema, and tell the user the filename and row count after.
- read_spreadsheet_to_workspace(spreadsheet_url_or_id, filename, ...): pull a Google
  Sheet INTO a workspace CSV server-side. Use it INSTEAD of call_mcp_tool(get_values)
  whenever the user wants to save, import, or analyze a Sheet. Pass as_active_dataset=true
  when they want to analyze/chat with it; tell the user the filename and row count after.

Service notes:
- Google Sheets / Docs / Slides / Drive: when the user provides a spreadsheet URL
  or ID, extract and use it directly (e.g. from
  "https://docs.google.com/spreadsheets/d/SHEET_ID/edit" extract "SHEET_ID").
  When no URL or ID is given, search for the item or create a new one.
  For reading data from a provided Google Sheets URL: call list_mcp_tools(google_sheets)
  first, then call_mcp_tool with the spreadsheet ID to fetch rows/data.
  For long Docs (over ~8,000 characters), create the doc first, then append the
  rest in chunks.

Be concise and action-oriented. Make reasonable assumptions instead of asking
clarifying questions.
"""
