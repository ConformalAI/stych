"""
Pydantic schemas for API requests and responses.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from helpers.agent_registry import api_task_types


class ActionOption(BaseModel):
    """Selectable action option shown in chat UI."""

    label: str
    value: str


class ActionPrompt(BaseModel):
    """Action prompt payload for interactive chat responses."""

    prompt_id: str
    kind: str
    question: str
    options: List[ActionOption] = Field(default_factory=list)
    allow_open_text: bool = True
    context: Optional[Dict[str, Any]] = None


class UserActionRequest(BaseModel):
    """User action object returned from UI button interaction."""

    prompt_id: str
    kind: str
    value: str
    label: Optional[str] = None
    text: Optional[str] = None


class UploadResponse(BaseModel):
    """Response for file upload."""

    file_id: str
    session_id: str
    filename: str
    size_mb: float
    rows: int
    columns: int
    column_names: List[str]
    summary: Dict[str, Any]
    message: str = "File uploaded successfully"


class QueryRequest(BaseModel):
    """Request for agent query."""

    session_id: str
    message: str = Field(..., min_length=1, max_length=10000)
    user_action: Optional[UserActionRequest] = None
    # Accepted values derived from the declarative agent registry.
    task_type: Optional[str] = Field(
        default="auto", pattern="^(" + "|".join(api_task_types()) + ")$"
    )
    stream: bool = False
    # Per-session model override: takes precedence over the user's DB preferred_model.
    # Only accepted if no messages have been sent yet (enforced by the route handler).
    model_id: Optional[str] = None
    # Deterministic dataset target for task_type="eda" (skill-style initial EDA).
    target_file_id: Optional[str] = None
    # When True, long-term memory (Hindsight) is fully disabled for this session —
    # no retention and no recall. Stored on the session row so it persists across reloads.
    is_anonymous: bool = False
    # List of MCP integration server_ids enabled for this query (e.g. ["google_sheets", "opendata"]).
    # When None or empty, all connected integrations are available (legacy behavior).
    enabled_integrations: Optional[List[str]] = None
    # Stable browser-generated key. Detached-run creation uses this to make a
    # retried POST return the original run instead of starting duplicate work.
    client_request_id: Optional[str] = Field(default=None, max_length=100)


class QueryResponse(BaseModel):
    """Response for agent query."""

    session_id: str
    response: str
    task_type: str
    agents_used: Optional[List[str]] = None
    status: str = "completed"
    action_prompt: Optional[ActionPrompt] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class StreamChunk(BaseModel):
    """Streaming response chunk."""

    content: str
    done: bool = False


class AnalysisResult(BaseModel):
    """Response for data analysis."""

    statistics: Dict[str, Any]
    correlations: Dict[str, Any]
    missing_values: Dict[str, Any]
    insights: str
    file_id: str


class CleaningRequest(BaseModel):
    """Request for data cleaning."""

    session_id: str
    file_id: str
    operations: Optional[List[Dict[str, Any]]] = None
    auto_clean: bool = True


class CleaningResponse(BaseModel):
    """Response for data cleaning."""

    original_shape: tuple
    cleaned_shape: tuple
    operations_applied: List[Dict[str, Any]]
    code: str
    recommendations: str
    file_id: str


class SessionInfo(BaseModel):
    """Session information."""

    session_id: str
    created_at: datetime
    message_count: int
    has_data: bool
    dataset_info: Optional[Dict[str, Any]] = None
    # Every dataset in the session (primary + additional), so the client can
    # restore the full multi-file list after a refresh. dataset_info remains the
    # primary dataset for back-compat.
    datasets: List[Dict[str, Any]] = Field(default_factory=list)
    is_anonymous: bool = False
    persist_warning: bool = False


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    uptime_seconds: float
    agents_ready: bool
    a2a_enabled: bool
    app_db_ok: bool = True
    app_db_schema_version: Optional[str] = None
    supertokens_ok: bool = True
    hindsight_ok: bool = True  # long-term memory server; None = not configured


# ----- Memory settings -----


class MemoryStatusResponse(BaseModel):
    """Status of the long-term memory system for the current user."""

    enabled: bool
    """True when the Hindsight server is configured on this deployment."""
    user_enabled: bool = True
    """False when the user has opted out of long-term memory in Settings."""
    hindsight_ok: bool
    """False when the Hindsight server is down or not configured."""
    bank_exists: bool
    """False when the user has no retained turns yet."""
    document_count: int = 0
    """Number of conversation turns retained (documents in the bank)."""
    fact_count: int = 0
    """Number of extracted world/observation facts stored."""
    experience_count: int = 0
    """Number of raw retained experience chunks."""
    world_count: int = 0
    """Number of synthesised world facts."""
    observation_count: int = 0
    """Number of synthesised observations."""
    pending_operations: int = 0
    """Background extraction jobs still running."""


class MemoryDeleteResponse(BaseModel):
    """Result of a bank-deletion request."""

    deleted: bool
    message: str


class MemoryPreferenceRequest(BaseModel):
    """Update the current user's long-term memory preference."""

    enabled: bool


class MemoryPreferenceResponse(BaseModel):
    """Current user's long-term memory preference."""

    enabled: bool


class MemoryEntryItem(BaseModel):
    """A single memory entry returned from the user's bank."""

    id: str
    content: str
    type: str = "experience"
    timestamp: str | None = None
    tags: list[str] = []
    document_id: str | None = None
    metadata: dict = {}


class MemoryEntriesResponse(BaseModel):
    """Paginated list of memory entries."""

    items: list[MemoryEntryItem]
    total: int
    limit: int
    offset: int


class MemoryTimeseriesBucket(BaseModel):
    """A single time bucket for the memory activity chart."""

    time: str
    world: int = 0
    experience: int = 0
    observation: int = 0


class MemoryTimeseriesResponse(BaseModel):
    """Timeseries of memory activity over time."""

    buckets: list[MemoryTimeseriesBucket]


class MemoryEntryDeleteResponse(BaseModel):
    """Result of deleting a single memory entry."""

    deleted: bool
    message: str


class MemoryGraphNode(BaseModel):
    """A node in the memory knowledge graph."""

    id: str
    label: str
    text: str
    fact_type: str = "experience"
    tags: list[str] = []
    date: str | None = None


class MemoryGraphEdge(BaseModel):
    """An edge in the memory knowledge graph."""

    source: str
    target: str
    link_type: str = "semantic"
    weight: float = 1.0


class MemoryGraphResponse(BaseModel):
    """Knowledge graph of memory units and their relationships."""

    nodes: list[MemoryGraphNode]
    edges: list[MemoryGraphEdge]
    total_units: int = 0


# ----- Data source connectors -----


class PostgresConnectionParams(BaseModel):
    """PostgreSQL connection (optional; omit to use env default)."""

    connection_url: Optional[str] = None
    host: Optional[str] = None
    port: int = 5432
    database: str = "postgres"
    user: Optional[str] = None
    password: Optional[str] = None
    sslmode: Optional[str] = None


class PostgresLoadRequest(BaseModel):
    """Load data from PostgreSQL into session."""

    session_id: Optional[str] = None
    connection: Optional[PostgresConnectionParams] = None
    schema: str = "public"
    table: Optional[str] = None
    custom_query: Optional[str] = None
    max_rows: int = 0
    connection_id: Optional[str] = None


class PostgresTablesRequest(BaseModel):
    """List tables in a PostgreSQL schema."""

    connection: Optional[PostgresConnectionParams] = None
    schema: str = "public"


class PostgresTablesResponse(BaseModel):
    """List of schema.table."""

    schema: str
    tables: List[Dict[str, str]]


class PostgresTestRequest(BaseModel):
    """Test PostgreSQL connection (omit connection to use config default)."""

    connection: Optional[PostgresConnectionParams] = None


class PostgresTestResponse(BaseModel):
    """Result of connection test."""

    success: bool
    message: str


class PostgresStatusResponse(BaseModel):
    """Whether default Postgres connection is configured."""

    default_connection_available: bool


class DatasourceLoadResponse(BaseModel):
    """Response for datasource load (same shape as upload for UI)."""

    session_id: str
    source_id: str
    source_label: str
    source_type: str  # Registered connector id (postgres, snowflake, redshift, etc.)
    rows: int
    columns: int
    column_names: List[str]
    summary: Dict[str, Any]
    message: str = "Data loaded successfully"


# ----- SQL Server -----


class SqlServerConnectionParams(BaseModel):
    """SQL Server connection parameters."""

    connection_url: Optional[str] = None
    host: Optional[str] = None
    port: int = 1433
    database: str = "master"
    user: Optional[str] = None
    password: Optional[str] = None


class SqlServerLoadRequest(BaseModel):
    """Load data from SQL Server into session."""

    session_id: Optional[str] = None
    connection: Optional[SqlServerConnectionParams] = None
    schema: str = "dbo"
    table: Optional[str] = None
    custom_query: Optional[str] = None
    max_rows: int = 0
    connection_id: Optional[str] = None


class SqlServerTablesRequest(BaseModel):
    """List tables in a SQL Server schema."""

    connection: Optional[SqlServerConnectionParams] = None
    schema: str = "dbo"


class SqlServerTablesResponse(BaseModel):
    """List of schema.table."""

    schema: str
    tables: List[Dict[str, str]]


class SqlServerTestRequest(BaseModel):
    """Test SQL Server connection."""

    connection: Optional[SqlServerConnectionParams] = None


class SqlServerTestResponse(BaseModel):
    """Result of SQL Server connection test."""

    success: bool
    message: str


class SqlServerStatusResponse(BaseModel):
    """Whether SQL Server datasource is enabled."""

    enabled: bool


# ----- MongoDB -----


class MongoConnectionParams(BaseModel):
    """MongoDB connection parameters."""

    connection_url: Optional[str] = None
    host: Optional[str] = None
    port: int = 27017
    database: str = "admin"
    user: Optional[str] = None
    password: Optional[str] = None


class MongoLoadRequest(BaseModel):
    """Load data from MongoDB into session."""

    session_id: Optional[str] = None
    connection: Optional[MongoConnectionParams] = None
    database: str = "admin"
    collection: Optional[str] = None
    query: Optional[Dict[str, Any]] = None
    max_rows: int = 0
    connection_id: Optional[str] = None


class MongoCollectionsRequest(BaseModel):
    """List collections in a MongoDB database."""

    connection: Optional[MongoConnectionParams] = None
    database: str = "admin"


class MongoCollectionsResponse(BaseModel):
    """List of collection names."""

    database: str
    collections: List[str]


class MongoTestRequest(BaseModel):
    """Test MongoDB connection."""

    connection: Optional[MongoConnectionParams] = None
    database: str = "admin"


class MongoTestResponse(BaseModel):
    """Result of MongoDB connection test."""

    success: bool
    message: str


class MongoStatusResponse(BaseModel):
    """Whether MongoDB datasource is enabled."""

    enabled: bool


# ----- MCP Integrations -----


class IntegrationStatus(BaseModel):
    """Status of a single MCP integration for the current user."""

    server_id: str
    name: str
    description: str
    connected: bool
    required_credentials: List[str]
    optional_credentials: List[str] = Field(default_factory=list)
    # Non-sensitive user preferences surfaced from the credential store.
    # e.g. {"default_spreadsheet_url": "https://docs.google.com/spreadsheets/d/..."}
    settings: Dict[str, str] = Field(default_factory=dict)


class IntegrationCredentialRequest(BaseModel):
    """Payload for saving credentials for a specific MCP server."""

    credentials: Dict[str, str] = Field(default_factory=dict)


class IntegrationStatusResponse(BaseModel):
    """Single-integration result returned after save/delete."""

    server_id: str
    connected: bool


# ----- Data Preview -----


class PreviewSource(BaseModel):
    """A previewable data source: a workspace file, an in-memory dataset, or a
    live database table served by query pushdown."""

    id: str
    kind: str  # "file" | "dataset" | "sql"
    name: str
    # The uploaded/DB dataset this source belongs to, when one exists. Present
    # for dataset/sql sources and for workspace files that mirror a dataset;
    # None for standalone artifacts (reports, engineered outputs). Drives the
    # delete action so it can remove the whole dataset vs. a lone file.
    file_id: Optional[str] = None
    renderer: str  # "table" | "text"
    suffix: Optional[str] = None
    size_bytes: Optional[int] = None
    modified_at: Optional[str] = None
    rows: Optional[int] = None
    columns: Optional[int] = None
    origin: Optional[str] = None  # "upload" | "postgres" | "sqlserver" | "mongodb" | ...
    is_active: bool = False
    # sql sources only: "base" = the user's table, "working" = the stych_temp copy.
    role: Optional[str] = None
    supports_stats: bool = True
    # Deliberately brief user-facing disclosure. The exhaustive matrix is a
    # developer artifact generated from the connector registry.
    capability_summary: Optional[str] = None


class PreviewSourcesResponse(BaseModel):
    """All previewable sources for a session."""

    sources: List[PreviewSource] = Field(default_factory=list)


class DeleteSourceRequest(BaseModel):
    """Identify a source to delete from a session (see /session/{id}/delete-source).

    ``kind`` mirrors PreviewSource.kind. ``id`` is the file_id (dataset/sql) or
    the workspace-relative path (file). ``file_id`` is the owning dataset when a
    ``file`` source mirrors one, so the whole dataset is removed rather than a
    single mirror file.
    """

    kind: str  # "file" | "dataset" | "sql"
    id: str
    file_id: Optional[str] = None


class PreviewColumn(BaseModel):
    """Column descriptor for the preview grid."""

    name: str
    dtype: str
    kind: str  # "number" | "string" | "date" | "bool" | "other"


class PreviewColumnStats(BaseModel):
    """Per-column summary stats shown in the grid header popover."""

    nulls: int
    unique: Optional[int] = None
    min: Optional[Any] = None
    max: Optional[Any] = None
    mean: Optional[float] = None


class PreviewTablePage(BaseModel):
    """One page of a tabular preview after filter/search/sort."""

    kind: str = "table"
    columns: List[PreviewColumn]
    rows: List[List[Any]]
    total_rows: int
    filtered_rows: int
    page: int
    total_pages: int
    truncated: bool = False
    # True when total/filtered counts are estimates (exact count timed out).
    approx: bool = False
    stats: Optional[Dict[str, PreviewColumnStats]] = None


class PreviewTextPage(BaseModel):
    """Raw text preview for txt/md/json files, capped at a byte limit."""

    kind: str = "text"
    text: str
    size_bytes: int
    truncated: bool = False


class PreviewDiffColumn(BaseModel):
    """Column descriptor for the diff grid, including its change status."""

    name: str
    dtype: str
    kind: str  # "number" | "string" | "date" | "bool" | "other"
    status: str = "common"  # "common" | "added" | "renamed"
    note: Optional[str] = None  # e.g. "standardized", "renamed from x"
    changed_cells: int = 0


class PreviewDiffSummary(BaseModel):
    """Whole-diff summary shown above the grid."""

    rows_added: int
    rows_removed: int
    rows_changed: int
    rows_unchanged: int
    cells_changed: int
    columns_added: List[str] = Field(default_factory=list)
    columns_removed: List[str] = Field(default_factory=list)
    columns_renamed: Dict[str, str] = Field(default_factory=dict)
    similarity: float
    alignment: str  # "key:<column>" | "positional"
    warning: Optional[str] = None


class PreviewDiffPage(BaseModel):
    """One page of a base->target diff: target values + per-cell change meta."""

    kind: str = "diff"
    columns: List[PreviewDiffColumn]
    rows: List[List[Any]]
    # Aligned to rows; each cell is null or {"k": kind, "l": label, "o": old value}.
    cell_meta: List[List[Optional[Dict[str, Any]]]]
    total_rows: int
    filtered_rows: int
    page: int
    total_pages: int
    summary: PreviewDiffSummary
