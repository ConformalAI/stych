import axios from 'axios';
import { getAuthHeaders } from './authHeaders';
import type { TaskType } from '../generated/contracts/taskTypes';

/**
 * Axios instance for API calls.
 *
 * In development, requests to /api/* are proxied by Vite to the backend
 * (see vite.config.ts). This means cookies work on the same origin.
 *
 * In production, set VITE_API_BASE_URL to the backend URL.
 * withCredentials ensures cookies are sent cross-origin.
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

export const api = axios.create({
    baseURL: API_BASE_URL,
    timeout: 30000,
    withCredentials: true, // Send cookies with every request
    headers: {
        'Content-Type': 'application/json',
    },
});

api.interceptors.request.use(async (config) => {
    const authHeaders = await getAuthHeaders();
    if (authHeaders['st-access-token']) {
        config.headers = config.headers ?? {};
        config.headers['st-access-token'] = authHeaders['st-access-token'];
    }
    return config;
});

// Response interceptor for auth errors.
// Guard prevents multiple parallel 401s from each triggering a redirect.
let _redirecting = false;
api.interceptors.response.use(
    (response) => response,
    (error) => {
        if (error.response?.status === 401 && !_redirecting) {
            const path = window.location.pathname;
            // Stay on the OAuth callback so it can recover an already-created
            // session instead of a hard redirect racing the sign-in exchange.
            if (path !== '/login' && path !== '/' && path !== '/oauth/callback/google') {
                _redirecting = true;
                window.location.href = '/login';
            }
        }
        return Promise.reject(error);
    }
);

export interface UploadResponse {
    file_id: string;
    session_id: string;
    filename: string;
    size_mb: number;
    rows: number;
    columns: number;
    column_names: string[];
    summary: Record<string, any>;
}

export interface QueryRequest {
    session_id: string;
    message: string;
    task_type?: TaskType;
    user_action?: {
        prompt_id: string;
        kind: string;
        value: string;
        label?: string;
        text?: string;
    };
    model_id?: string | null;
    is_anonymous?: boolean;
    enabled_integrations?: string[] | null;
    client_request_id?: string;
    target_file_id?: string;
}

export interface AgentRun {
    run_id: string;
    session_id: string;
    status:
        | 'queued'
        | 'running'
        | 'completed'
        | 'failed'
        | 'cancelled'
        | 'timed_out'
        | 'limit_reached';
    created_at: string;
    started_at?: string | null;
    completed_at?: string | null;
}

export interface SessionInfo {
    session_id: string;
    created_at: string;
    message_count: number;
    has_data: boolean;
    dataset_info?: Record<string, any>;
    datasets?: Record<string, any>[];
}

// API Functions
export const uploadFile = async (file: File, sessionId?: string): Promise<UploadResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    if (sessionId) {
        formData.append('session_id', sessionId);
    }

    const response = await api.post('/api/upload', formData, {
        // The axios instance sets a default Content-Type of application/json. In
        // axios 1.x, FormData + a JSON content-type makes transformRequest
        // serialize the form to JSON (formDataToJSON) instead of sending
        // multipart — the backend then sees no `file` field and returns 422.
        // Clearing the header lets the browser set multipart/form-data with the
        // correct boundary.
        // Use a generous timeout since files can be up to 50 MB on slow connections.
        headers: { 'Content-Type': undefined },
        timeout: 10 * 60 * 1000,
    });

    return response.data;
};

export const createAgentRun = async (request: QueryRequest): Promise<AgentRun> => {
    const response = await api.post('/api/query/runs', request);
    return response.data;
};

export const getLatestAgentRun = async (sessionId: string): Promise<AgentRun | null> => {
    const response = await api.get(`/api/query/runs/session/${sessionId}`);
    return response.data?.run ?? null;
};

export const cancelAgentRun = async (runId: string): Promise<AgentRun> => {
    const response = await api.post(`/api/query/runs/${runId}/cancel`);
    return response.data;
};

export const agentRunStreamUrl = (runId: string): string =>
    `${API_BASE_URL || ''}/api/query/runs/${runId}/stream`;

export const getHistory = async (sessionId: string, limit = 50) => {
    const response = await api.get(`/api/history/${sessionId}`, {
        params: { limit },
    });
    return response.data;
};

export const getSessionInfo = async (sessionId: string): Promise<SessionInfo> => {
    const response = await api.get(`/api/session/${sessionId}`);
    return response.data;
};

export const deleteSession = async (sessionId: string) => {
    try {
        const response = await api.delete(`/api/session/${sessionId}`);
        return response.data;
    } catch (error) {
        // A never-persisted client UUID (or a row already gone) used to 404
        // and the sidebar treated that as failure, so empty sessions could not
        // be retired. Treat not-found as already deleted.
        if (axios.isAxiosError(error) && error.response?.status === 404) {
            return { message: 'Session deleted successfully', session_id: sessionId };
        }
        throw error;
    }
};

export const archiveWorkspace = async (sessionId: string): Promise<void> => {
    await api.post(`/api/session/${sessionId}/archive-workspace`);
};

export const checkHealth = async () => {
    const response = await api.get('/api/health');
    return response.data;
};

export type ExportFormat = 'csv' | 'xlsx' | 'json' | 'parquet';
export type ExportOption =
    | ExportFormat
    | 'cleaning_script'
    | 'cleaning_sql'
    | `workspace:${string}`;

export interface CleaningWorkspaceFile {
    name: string;
    path?: string;
    size_bytes: string;
    modified_at: string;
    suffix?: string;
    // "true"/"false" string flag — whether this file is the session's working dataset.
    is_active_dataset?: string;
}

// ----- SQL Server data source -----

export interface SqlServerConnectionParams {
    connection_url?: string;
    host?: string;
    port?: number;
    database?: string;
    user?: string;
    password?: string;
}

export interface SqlServerTestResponse {
    success: boolean;
    message: string;
}

export interface SqlServerTablesResponse {
    schema: string;
    tables: Array<{ schema: string; table: string }>;
}

export const testSqlServerConnection = async (
    connection?: SqlServerConnectionParams | null
): Promise<SqlServerTestResponse> => {
    const response = await api.post('/api/datasources/sqlserver/test', {
        connection: connection ?? undefined,
    });
    return response.data;
};

export const getSqlServerTables = async (
    schema: string = 'dbo',
    connection?: SqlServerConnectionParams | null
): Promise<SqlServerTablesResponse> => {
    const response = await api.post('/api/datasources/sqlserver/tables', {
        schema,
        connection: connection ?? undefined,
    });
    return response.data;
};

export const loadSqlServerTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection?: SqlServerConnectionParams | null
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/sqlserver/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection: connection ?? undefined,
    });
    return response.data;
};

// ----- Cloud warehouse data sources -----

export interface SnowflakeConnectionParams {
    account: string;
    user: string;
    private_key: string;
    warehouse: string;
    database: string;
    role?: string;
    private_key_passphrase?: string;
}

export interface RedshiftConnectionParams {
    host: string;
    port: number;
    database: string;
    user: string;
    access_key_id: string;
    secret_access_key: string;
    session_token?: string;
    region?: string;
    cluster_identifier?: string;
}

export interface BigQueryConnectionParams {
    auth_mode?: 'oauth';
    service_account_json?: string;
    project_id?: string;
}

export interface DatabricksConnectionParams {
    server_hostname: string;
    http_path: string;
    access_token: string;
    catalog: string;
    scratch_catalog?: string;
    scratch_schema?: string;
}

export interface WarehouseTestResponse {
    success: boolean;
    message: string;
}

export interface WarehouseTablesResponse {
    schema: string;
    tables: Array<{ schema: string; table: string }>;
}

export const testSnowflakeConnection = async (
    connection: SnowflakeConnectionParams
): Promise<WarehouseTestResponse> => {
    const response = await api.post('/api/datasources/snowflake/test', { connection });
    return response.data;
};

export const getSnowflakeTables = async (
    schema: string,
    connection: SnowflakeConnectionParams
): Promise<WarehouseTablesResponse> => {
    const response = await api.post('/api/datasources/snowflake/tables', {
        schema,
        connection,
    });
    return response.data;
};

export const loadSnowflakeTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection: SnowflakeConnectionParams
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/snowflake/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection,
    });
    return response.data;
};

export const testRedshiftConnection = async (
    connection: RedshiftConnectionParams
): Promise<WarehouseTestResponse> => {
    const response = await api.post('/api/datasources/redshift/test', { connection });
    return response.data;
};

export const getRedshiftTables = async (
    schema: string,
    connection: RedshiftConnectionParams
): Promise<WarehouseTablesResponse> => {
    const response = await api.post('/api/datasources/redshift/tables', {
        schema,
        connection,
    });
    return response.data;
};

export const loadRedshiftTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection: RedshiftConnectionParams
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/redshift/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection,
    });
    return response.data;
};

export const BIGQUERY_OAUTH_SERVER_ID = 'google_bigquery';

export const getBigQueryOAuthStatus = async (): Promise<{ connected: boolean }> => {
    const response = await api.get('/api/datasources/bigquery/oauth/status');
    return response.data;
};

export const getBigQueryProjects = async (
    connection: BigQueryConnectionParams
): Promise<{ projects: string[] }> => {
    const response = await api.post('/api/datasources/bigquery/projects', { connection });
    return response.data;
};

export const getBigQueryDatasets = async (
    connection: BigQueryConnectionParams
): Promise<{ datasets: string[] }> => {
    const response = await api.post('/api/datasources/bigquery/datasets', { connection });
    return response.data;
};

export const testBigQueryConnection = async (
    connection: BigQueryConnectionParams
): Promise<WarehouseTestResponse> => {
    const response = await api.post('/api/datasources/bigquery/test', { connection });
    return response.data;
};

export const getBigQueryTables = async (
    schema: string,
    connection: BigQueryConnectionParams
): Promise<WarehouseTablesResponse> => {
    const response = await api.post('/api/datasources/bigquery/tables', {
        schema,
        connection,
    });
    return response.data;
};

export const loadBigQueryTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection: BigQueryConnectionParams
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/bigquery/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection,
    });
    return response.data;
};

export const testDatabricksConnection = async (
    connection: DatabricksConnectionParams
): Promise<WarehouseTestResponse> => {
    const response = await api.post('/api/datasources/databricks/test', { connection });
    return response.data;
};

export const getDatabricksTables = async (
    schema: string,
    connection: DatabricksConnectionParams
): Promise<WarehouseTablesResponse> => {
    const response = await api.post('/api/datasources/databricks/tables', {
        schema,
        connection,
    });
    return response.data;
};

export const loadDatabricksTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection: DatabricksConnectionParams
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/databricks/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection,
    });
    return response.data;
};

// ----- MongoDB data source -----

export interface MongoConnectionParams {
    connection_url?: string;
    host?: string;
    port?: number;
    database?: string;
    user?: string;
    password?: string;
}

export interface MongoTestResponse {
    success: boolean;
    message: string;
}

export interface MongoCollectionsResponse {
    database: string;
    collections: string[];
}

export const testMongoConnection = async (
    connection?: MongoConnectionParams | null,
    database?: string
): Promise<MongoTestResponse> => {
    const response = await api.post('/api/datasources/mongodb/test', {
        connection: connection ?? undefined,
        database: database ?? 'admin',
    });
    return response.data;
};

export const getMongoCollections = async (
    database: string = 'admin',
    connection?: MongoConnectionParams | null
): Promise<MongoCollectionsResponse> => {
    const response = await api.post('/api/datasources/mongodb/collections', {
        database,
        connection: connection ?? undefined,
    });
    return response.data;
};

export const loadMongoCollection = async (
    database: string,
    collection: string,
    sessionId: string | null,
    connection?: MongoConnectionParams | null
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/mongodb/load', {
        database,
        collection,
        session_id: sessionId ?? undefined,
        connection: connection ?? undefined,
    });
    return response.data;
};

// ----- PostgreSQL data source (external DB as source, like CSV) -----

export interface PostgresConnectionParams {
    connection_url?: string;
    host?: string;
    port?: number;
    database?: string;
    user?: string;
    password?: string;
    sslmode?: string;
}

export interface PostgresStatusResponse {
    default_connection_available: boolean;
}

export interface PostgresTestResponse {
    success: boolean;
    message: string;
}

export interface PostgresTablesResponse {
    schema: string;
    tables: Array<{ schema: string; table: string }>;
}

export interface DatasourceLoadResponse {
    session_id: string;
    source_id: string;
    source_label: string;
    source_type: string;
    rows: number;
    columns: number;
    column_names: string[];
    summary: Record<string, unknown>;
    message: string;
}

export type ConnectorId =
    | 'postgres'
    | 'sqlserver'
    | 'mongodb'
    | 'snowflake'
    | 'redshift'
    | 'bigquery'
    | 'databricks';

export interface SavedTable {
    id: string;
    schema_name?: string | null;
    table_name?: string | null;
    custom_query?: string | null;
    display_name: string;
    created_at?: string | null;
}

export interface SavedConnection {
    id: string;
    connector_id: ConnectorId;
    name: string;
    description?: string | null;
    tags: string[];
    hint: Record<string, unknown>;
    last_tested_at?: string | null;
    /** Outcome of the most recent test: 'ok' | 'error' | null (never tested). */
    last_test_status?: 'ok' | 'error' | null;
    last_test_error?: string | null;
    last_used_at?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
    tables: SavedTable[];
}

/** A connector *type* the platform ships, as served by /api/connections/catalog. */
export interface ConnectorCatalogEntry {
    id: ConnectorId;
    label: string;
    category: string;
    vendor: string;
    maturity: 'production' | 'beta' | 'experimental';
    summary: string;
    enabled: boolean;
    schema_label: string;
    default_schema: string;
    relation_noun: string;
    configured_count: number;
    same_source_join?: {
        strategies: Array<{
            strategy: string;
            mode: 'native' | 'emulated';
            scope: 'full' | 'bounded' | 'sample';
            freshness: 'live' | 'snapshot';
            executor: 'source' | 'pandas' | 'duckdb' | 'hybrid';
            egress: 'none' | 'preview_only' | 'full';
            result_mode: 'source_scratch' | 'duckdb_snapshot' | 'pandas_materialized';
            caveats: string[];
        }>;
        join_types: Array<'INNER' | 'LEFT' | 'RIGHT' | 'FULL OUTER'>;
        caveats: string[];
    } | null;
}

export const getConnectorCatalog = async (): Promise<ConnectorCatalogEntry[]> => {
    const response = await api.get('/api/connections/catalog');
    return response.data?.connectors ?? [];
};

export const listSavedConnections = async (
    connectorId?: ConnectorId
): Promise<SavedConnection[]> => {
    const response = await api.get('/api/connections', {
        params: connectorId ? { connector_id: connectorId } : undefined,
    });
    return response.data?.connections ?? [];
};

export const createSavedConnection = async (body: {
    connector_id: ConnectorId;
    name?: string;
    description?: string;
    tags?: string[];
    connection?: Record<string, unknown> | object;
}): Promise<SavedConnection> => {
    const response = await api.post('/api/connections', body);
    return response.data;
};

export const patchSavedConnection = async (
    connectionId: string,
    body: {
        name?: string;
        description?: string;
        tags?: string[];
        connection?: Record<string, unknown> | object;
    }
): Promise<SavedConnection> => {
    const response = await api.patch(`/api/connections/${connectionId}`, body);
    return response.data;
};

export const testSavedConnection = async (
    connectionId: string,
    connection?: Record<string, unknown> | object
): Promise<{ success: boolean; message: string }> => {
    const response = await api.post(`/api/connections/${connectionId}/test`, {
        connection: connection ?? {},
    });
    return response.data;
};

export const deleteSavedConnection = async (
    connectionId: string,
    opts?: { confirm?: boolean; etag?: string }
): Promise<{
    ok: boolean;
    detached: Array<{ session_id: string; file_id: string; filename: string }>;
}> => {
    const response = await api.delete(`/api/connections/${connectionId}`, {
        params: {
            confirm: opts?.confirm ? true : undefined,
            etag: opts?.etag,
        },
    });
    return response.data;
};

export interface ConnectionUsageSession {
    session_id: string;
    title?: string | null;
    tables: string[];
    viz_count: number;
    filter_count: number;
}

export interface ConnectionUsage {
    etag: string;
    sessions: ConnectionUsageSession[];
    table_count: number;
    viz_count: number;
    filter_count: number;
}

export const getConnectionUsage = async (connectionId: string): Promise<ConnectionUsage> => {
    const response = await api.get(`/api/connections/${connectionId}/usage`);
    return response.data;
};

export const listRemoteTables = async (
    connectionId: string,
    schema: string
): Promise<{
    schema: string;
    tables: Array<{ schema: string; table: string }>;
    collections: string[];
}> => {
    const response = await api.get(`/api/connections/${connectionId}/remote-tables`, {
        params: { schema },
    });
    return response.data;
};

export interface NamespaceDiscoveryResponse {
    default_namespace: string;
    namespaces: string[];
    complete: boolean;
    warning?: string | null;
}

export const listRemoteNamespaces = async (
    connectionId: string
): Promise<NamespaceDiscoveryResponse> => {
    const response = await api.get(`/api/connections/${connectionId}/namespaces`);
    return response.data;
};

export const pinSavedTable = async (
    connectionId: string,
    body: {
        schema_name?: string;
        table_name?: string;
        custom_query?: string;
        display_name?: string;
    }
): Promise<SavedTable> => {
    const response = await api.post(`/api/connections/${connectionId}/tables`, body);
    return response.data;
};

export const unpinSavedTable = async (connectionId: string, tableId: string): Promise<void> => {
    await api.delete(`/api/connections/${connectionId}/tables/${tableId}`);
};

export const attachSavedTable = async (
    connectionId: string,
    tableId: string,
    sessionId: string | null
): Promise<DatasourceLoadResponse> => {
    const response = await api.post(`/api/connections/${connectionId}/tables/${tableId}/attach`, {
        session_id: sessionId ?? undefined,
    });
    return response.data;
};

export const attachConnectionTable = async (
    connectionId: string,
    body: {
        sessionId: string | null;
        schema_name?: string;
        table_name?: string;
        custom_query?: string;
    }
): Promise<DatasourceLoadResponse> => {
    const response = await api.post(`/api/connections/${connectionId}/attach`, {
        session_id: body.sessionId ?? undefined,
        schema_name: body.schema_name,
        table_name: body.table_name,
        custom_query: body.custom_query,
    });
    return response.data;
};

export const getPostgresStatus = async (): Promise<PostgresStatusResponse> => {
    const response = await api.get('/api/datasources/postgres/status');
    return response.data;
};

export const testPostgresConnection = async (
    connection?: PostgresConnectionParams | null
): Promise<PostgresTestResponse> => {
    const response = await api.post('/api/datasources/postgres/test', {
        connection: connection ?? undefined,
    });
    return response.data;
};

export const getPostgresTables = async (
    schema: string = 'public',
    connection?: PostgresConnectionParams | null
): Promise<PostgresTablesResponse> => {
    const response = await api.post('/api/datasources/postgres/tables', {
        schema,
        connection: connection ?? undefined,
    });
    return response.data;
};

export const loadPostgresTable = async (
    schema: string,
    table: string,
    sessionId: string | null,
    connection?: PostgresConnectionParams | null
): Promise<DatasourceLoadResponse> => {
    const response = await api.post('/api/datasources/postgres/load', {
        schema,
        table,
        session_id: sessionId ?? undefined,
        connection: connection ?? undefined,
    });
    return response.data;
};

/**
 * Check whether a blob response is actually a JSON error body.
 * When responseType:'blob' is used, error responses still arrive as blobs.
 * Returns the error detail string if it's a JSON error, or null otherwise.
 */
async function _checkBlobForError(blob: Blob, contentType: string): Promise<string | null> {
    if (!contentType.includes('application/json')) return null;
    try {
        const text = await blob.text();
        const json = JSON.parse(text);
        return json.detail ?? json.message ?? 'Export failed';
    } catch {
        return null;
    }
}

function _triggerDownload(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

export const exportData = async (
    sessionId: string,
    format: ExportFormat = 'csv'
): Promise<void> => {
    const response = await api.get(`/api/export/${sessionId}`, {
        params: { format },
        responseType: 'blob',
    });

    const contentType = response.headers['content-type'] ?? '';
    const errMsg = await _checkBlobForError(response.data, String(contentType));
    if (errMsg) throw new Error(errMsg);

    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const filename = filenameMatch?.[1] ?? `stych_export.${format}`;
    _triggerDownload(response.data, filename);
};

export const exportCleaningScript = async (sessionId: string): Promise<void> => {
    const response = await api.get(`/api/export/cleaning-script/${sessionId}`, {
        responseType: 'blob',
    });

    const contentType = response.headers['content-type'] ?? '';
    const errMsg = await _checkBlobForError(response.data, String(contentType));
    if (errMsg) throw new Error(errMsg);

    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const filename = filenameMatch?.[1] ?? 'stych_cleaning_script.zip';
    _triggerDownload(response.data, filename);
};

export const exportCleaningSql = async (sessionId: string): Promise<void> => {
    const response = await api.get(`/api/export/cleaning-sql/${sessionId}`, {
        responseType: 'blob',
    });

    const contentType = response.headers['content-type'] ?? '';
    const errMsg = await _checkBlobForError(response.data, String(contentType));
    if (errMsg) throw new Error(errMsg);

    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const filename = filenameMatch?.[1] ?? 'stych_cleaning_sql.zip';
    _triggerDownload(response.data, filename);
};

// ─── Model selection ─────────────────────────────────────────────────────────

export interface AvailableModel {
    id: string;
    provider: string;
    is_default: boolean;
}

export interface AvailableModelsResponse {
    active_provider: string;
    models: AvailableModel[];
    preferred_model: string | null;
}

export const getAvailableModels = async (): Promise<AvailableModelsResponse> => {
    const response = await api.get('/api/models');
    return response.data;
};

export const getUserModel = async (): Promise<{ preferred_model: string | null }> => {
    const response = await api.get('/api/settings/model');
    return response.data;
};

export const setUserModel = async (
    modelId: string | null
): Promise<{ preferred_model: string | null }> => {
    const response = await api.post('/api/settings/model', { model_id: modelId });
    return response.data;
};

export interface RouterHealthResponse {
    healthy: boolean;
    latency_ms: number;
    reply?: string | null;
}

export const checkRouterHealth = async (): Promise<RouterHealthResponse> => {
    const response = await api.get('/api/models/health');
    return response.data;
};

// ----- Long-term memory -----

export interface MemoryStatus {
    enabled: boolean;
    user_enabled: boolean;
    hindsight_ok: boolean;
    bank_exists: boolean;
    document_count: number;
    fact_count: number;
    experience_count: number;
    world_count: number;
    observation_count: number;
    pending_operations: number;
}

export const getMemoryStatus = async (): Promise<MemoryStatus> => {
    const response = await api.get('/api/memory/status');
    return response.data;
};

export const updateMemoryPreference = async (enabled: boolean): Promise<{ enabled: boolean }> => {
    const response = await api.patch('/api/memory/preferences', { enabled });
    return response.data;
};

export const deleteMemoryBank = async (): Promise<{ deleted: boolean; message: string }> => {
    const response = await api.delete('/api/memory/bank');
    return response.data;
};

export interface MemoryEntry {
    id: string;
    content: string;
    type: string;
    timestamp: string | null;
    tags: string[];
    document_id: string | null;
    metadata: Record<string, unknown>;
}

export interface MemoryEntriesResult {
    items: MemoryEntry[];
    total: number;
    limit: number;
    offset: number;
}

export interface MemoryTimeseriesBucket {
    time: string;
    world: number;
    experience: number;
    observation: number;
}

export const listMemoryEntries = async (params?: {
    search?: string;
    type?: string;
    limit?: number;
    offset?: number;
}): Promise<MemoryEntriesResult> => {
    const response = await api.get('/api/memory/entries', { params });
    return response.data;
};

export const deleteMemoryEntry = async (
    documentId: string
): Promise<{ deleted: boolean; message: string }> => {
    const response = await api.delete(`/api/memory/entries/${encodeURIComponent(documentId)}`);
    return response.data;
};

export const getMemoryTimeseries = async (
    period?: string
): Promise<{ buckets: MemoryTimeseriesBucket[] }> => {
    const response = await api.get('/api/memory/timeseries', { params: period ? { period } : {} });
    return response.data;
};

export interface MemoryGraphNode {
    id: string;
    label: string;
    text: string;
    fact_type: string;
    tags: string[];
    date: string | null;
}

export interface MemoryGraphEdge {
    source: string;
    target: string;
    link_type: string;
    weight: number;
}

export const getMemoryGraph = async (
    limit?: number
): Promise<{ nodes: MemoryGraphNode[]; edges: MemoryGraphEdge[]; total_units: number }> => {
    const response = await api.get('/api/memory/graph', { params: limit ? { limit } : {} });
    return response.data;
};

export const listCleaningWorkspaceFiles = async (
    sessionId: string
): Promise<CleaningWorkspaceFile[]> => {
    const response = await api.get(`/api/export/cleaning-files/${sessionId}`);
    return response.data?.files ?? [];
};

export const exportCleaningWorkspaceFile = async (
    sessionId: string,
    filename: string,
    path?: string,
    format?: 'csv' | 'parquet'
): Promise<void> => {
    const response = await api.get(`/api/export/cleaning-file/${sessionId}`, {
        params: { filename, path, ...(format ? { format } : {}) },
        responseType: 'blob',
    });

    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const resolvedName = filenameMatch?.[1] ?? filename;

    const url = URL.createObjectURL(new Blob([response.data]));
    const link = document.createElement('a');
    link.href = url;
    link.download = resolvedName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
};

export interface SourceExportInfo {
    available: boolean;
    reason?: string;
    scope?: 'full' | 'bounded' | 'sample';
    full_table?: boolean;
    rows?: number | null;
    rows_approximate?: boolean;
    row_cap?: number;
    exceeds_cap?: boolean;
}

export const getSourceExportInfo = async (
    sessionId: string,
    sourceId: string
): Promise<SourceExportInfo> => {
    const response = await api.get(`/api/export/source/${sessionId}/info`, {
        params: { source_id: sourceId },
    });
    return response.data;
};

/**
 * Download a connected table. The backend refuses an oversized export with 413
 * rather than silently truncating; pass truncate to accept the capped prefix.
 */
export const exportPreviewSource = async (
    sessionId: string,
    sourceId: string,
    format: 'csv' | 'parquet',
    truncate = false
): Promise<{ truncated: boolean }> => {
    const response = await api.get(`/api/export/source/${sessionId}`, {
        params: { source_id: sourceId, format, ...(truncate ? { truncate: true } : {}) },
        responseType: 'blob',
    });

    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const resolvedName = filenameMatch?.[1] ?? `export.${format}`;

    const url = URL.createObjectURL(new Blob([response.data]));
    const link = document.createElement('a');
    link.href = url;
    link.download = resolvedName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    return { truncated: response.headers['x-stych-export-truncated'] === 'true' };
};

// ---------------------------------------------------------------------------
// Data Preview
// ---------------------------------------------------------------------------

export interface PreviewSource {
    id: string;
    kind: 'file' | 'dataset' | 'sql';
    name: string;
    /** Owning dataset's file_id, when this source belongs to one (drives delete). */
    file_id?: string | null;
    renderer: 'table' | 'text';
    suffix?: string | null;
    size_bytes?: number | null;
    modified_at?: string | null;
    rows?: number | null;
    columns?: number | null;
    origin?: string | null; // "upload" | "postgres" | "sqlserver" | "mongodb" | cloud warehouses | ...
    /** sql sources only: "base" = the user's DB table, "working" = the agent's working copy. */
    role?: 'base' | 'working' | null;
    /** False for sql sources whose backend cannot compute column stats (sqlserver/mongodb). */
    supports_stats?: boolean;
    /** Brief disclosure of live/snapshot and full/sample behavior. */
    capability_summary?: string | null;
    is_active: boolean;
}

export interface PreviewApiColumn {
    name: string;
    dtype: string;
    kind: 'number' | 'string' | 'date' | 'bool' | 'other';
}

export interface PreviewApiColumnStats {
    nulls: number;
    unique?: number | null;
    min?: string | number | null;
    max?: string | number | null;
    mean?: number | null;
}

export interface PreviewTablePage {
    kind: 'table';
    columns: PreviewApiColumn[];
    rows: Array<Array<string | number | boolean | null>>;
    total_rows: number;
    filtered_rows: number;
    page: number;
    total_pages: number;
    truncated: boolean;
    /** True when total_rows/filtered_rows are estimates (exact COUNT timed out). */
    approx?: boolean;
    stats?: Record<string, PreviewApiColumnStats> | null;
}

export interface PreviewTextPage {
    kind: 'text';
    text: string;
    size_bytes: number;
    truncated: boolean;
}

export type PreviewPageResponse = PreviewTablePage | PreviewTextPage;

export interface PreviewPageParams {
    source: 'file' | 'dataset' | 'sql';
    path?: string;
    file_id?: string;
    source_id?: string;
    page?: number;
    page_size?: number;
    sort_by?: string;
    sort_dir?: 'asc' | 'desc';
    search?: string;
    filters?: Record<string, string>;
    include_stats?: boolean;
}

export const getPreviewSources = async (sessionId: string): Promise<PreviewSource[]> => {
    const response = await api.get(`/api/preview/sources/${sessionId}`);
    return response.data?.sources ?? [];
};

/** Delete a source from a session, removing it locally and from S3/prod. */
export const deleteSource = async (
    sessionId: string,
    source: { kind: 'file' | 'dataset' | 'sql'; id: string; file_id?: string | null }
): Promise<{ remaining_datasets: number }> => {
    const response = await api.post(`/api/session/${sessionId}/delete-source`, {
        kind: source.kind,
        id: source.id,
        file_id: source.file_id ?? null,
    });
    return response.data;
};

export interface PreviewDiffColumn {
    name: string;
    dtype: string;
    kind: 'number' | 'string' | 'date' | 'bool' | 'other';
    status: 'common' | 'added' | 'renamed';
    note?: string | null;
    changed_cells: number;
}

export interface PreviewDiffSummary {
    rows_added: number;
    rows_removed: number;
    rows_changed: number;
    rows_unchanged: number;
    cells_changed: number;
    columns_added: string[];
    columns_removed: string[];
    columns_renamed: Record<string, string>;
    similarity: number;
    alignment: string; // "key:<column>" | "positional"
    warning?: string | null;
}

export interface PreviewDiffCellMeta {
    k: 'added' | 'removed' | 'changed' | 'imputed' | 'transformed';
    l?: string;
    o?: string | number | boolean | null;
}

export interface PreviewDiffPage {
    kind: 'diff';
    columns: PreviewDiffColumn[];
    rows: Array<Array<string | number | boolean | null>>;
    cell_meta: Array<Array<PreviewDiffCellMeta | null>>;
    total_rows: number;
    filtered_rows: number;
    page: number;
    total_pages: number;
    summary: PreviewDiffSummary;
}

export interface PreviewDiffParams {
    base_source: 'file' | 'dataset' | 'sql';
    base_path?: string;
    base_file_id?: string;
    base_source_id?: string;
    target_source: 'file' | 'dataset' | 'sql';
    target_path?: string;
    target_file_id?: string;
    target_source_id?: string;
    page?: number;
    page_size?: number;
    changes_only?: boolean;
    change_kind?: 'added' | 'removed' | 'changed' | 'imputed' | 'transformed';
    sort_by?: string;
    sort_dir?: 'asc' | 'desc';
    search?: string;
    filters?: Record<string, string>;
}

export const getPreviewDiff = async (
    sessionId: string,
    params: PreviewDiffParams,
    signal?: AbortSignal
): Promise<PreviewDiffPage> => {
    const { filters, ...rest } = params;
    const response = await api.get(`/api/preview/diff/${sessionId}`, {
        params: {
            ...rest,
            filters:
                filters && Object.keys(filters).length > 0 ? JSON.stringify(filters) : undefined,
        },
        signal,
    });
    return response.data;
};

/** Download the whole session workspace as a zip archive. */
export const exportWorkspaceArchive = async (sessionId: string): Promise<void> => {
    const response = await api.get(`/api/export/cleaning-files-archive/${sessionId}`, {
        responseType: 'blob',
    });
    const url = URL.createObjectURL(new Blob([response.data]));
    const link = document.createElement('a');
    link.href = url;
    link.download = `workspace_${sessionId.slice(0, 8)}.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
};

export const getPreviewPage = async (
    sessionId: string,
    params: PreviewPageParams,
    signal?: AbortSignal
): Promise<PreviewPageResponse> => {
    const { filters, ...rest } = params;
    const response = await api.get(`/api/preview/page/${sessionId}`, {
        params: {
            ...rest,
            filters:
                filters && Object.keys(filters).length > 0 ? JSON.stringify(filters) : undefined,
        },
        signal,
    });
    return response.data;
};

// ---------------------------------------------------------------------------
// MCP Integrations
// ---------------------------------------------------------------------------

export interface IntegrationStatus {
    server_id: string;
    name: string;
    description: string;
    connected: boolean;
    required_credentials: string[];
    optional_credentials: string[];
    settings: Record<string, string>;
}

/**
 * Fetch all available MCP integrations and their connection status for the current user.
 */
export const getIntegrations = async (): Promise<IntegrationStatus[]> => {
    const response = await api.get('/api/integrations');
    return response.data;
};

/**
 * Save (or replace) credentials for an MCP integration.
 * Pass an empty object `{}` for integrations that require no credentials.
 */
export const saveIntegrationCredentials = async (
    serverId: string,
    credentials: Record<string, string>
): Promise<void> => {
    await api.put(`/api/integrations/${serverId}`, { credentials });
};

/**
 * Remove credentials for an MCP integration, marking it as disconnected.
 */
export const deleteIntegrationCredentials = async (serverId: string): Promise<void> => {
    await api.delete(`/api/integrations/${serverId}`);
};

/**
 * Begin a Google OAuth flow for the given server_id.
 * Returns the Google authorization URL to open in a popup window.
 */
export const startGoogleOAuth = async (serverId: string): Promise<string> => {
    const response = await api.get(`/api/integrations/${serverId}/oauth/start`);
    return response.data.auth_url as string;
};

// ----- Linking inspect / viz -----

export interface LinkingPreviewRow {
    _row_id: number;
    [key: string]: unknown;
}

export interface LinkingPreviewTable {
    name: string;
    join_key?: string | null;
    columns: string[];
    rows: LinkingPreviewRow[];
    row_count: number;
    truncated?: boolean;
}

export type LinkingVizKind = 'score_matrix' | 'coordinated' | 'bipartite';

export interface LinkingInspectContext {
    left: LinkingPreviewTable;
    right: LinkingPreviewTable;
    plan: {
        left: string;
        right: string;
        left_on: string;
        right_on: string;
        strategy: string;
        threshold: number;
        join_type: string;
    };
    available_strategies: string[];
    has_semantic?: boolean;
    has_lexical?: boolean;
    committed?: boolean;
    viz?: { kinds: LinkingVizKind[] };
}

export interface LinkingVizResponse {
    kind: LinkingVizKind;
    library: 'vega-lite' | 'vega';
    spec: Record<string, unknown>;
    n_edges: number;
    n_sources: number;
    n_candidates: number;
    strategy?: string | null;
    threshold?: number | null;
}

export interface LinkingInspectCandidate {
    candidate_row_id: number;
    score: number;
    strategy: string;
    value?: string | null;
    source_value?: string | null;
    row?: LinkingPreviewRow;
    passes_threshold?: boolean;
}

export interface LinkingInspectResponse {
    source_row_id: number;
    source_row?: LinkingPreviewRow;
    source_value?: unknown;
    join_key?: string;
    candidate_join_key?: string;
    strategy?: string;
    threshold?: number;
    candidates: LinkingInspectCandidate[];
    strategies?: string[];
}

export const inspectLinkingRow = async (
    sessionId: string,
    sourceRowId: number,
    options?: { strategy?: string; threshold?: number; top_n?: number }
): Promise<LinkingInspectResponse> => {
    const response = await api.post('/api/linking/inspect', {
        session_id: sessionId,
        source_row_id: sourceRowId,
        strategy: options?.strategy,
        threshold: options?.threshold,
        top_n: options?.top_n ?? 10,
    });
    return response.data;
};

export const getLinkingInspectContext = async (
    sessionId: string
): Promise<{ context: LinkingInspectContext | null }> => {
    const response = await api.get('/api/linking/inspect/context', {
        params: { session_id: sessionId },
    });
    return response.data;
};

export const fetchLinkingViz = async (
    sessionId: string,
    kind: LinkingVizKind,
    options?: { strategy?: string; threshold?: number; dark?: boolean }
): Promise<LinkingVizResponse> => {
    const response = await api.post('/api/linking/viz', {
        session_id: sessionId,
        kind,
        strategy: options?.strategy,
        threshold: options?.threshold,
        dark: options?.dark ?? false,
    });
    return response.data;
};
