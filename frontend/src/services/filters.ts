/**
 * Saved-filter API client: CRUD over /api/filters plus the apply endpoint that
 * feeds the preview grid. Mirrors the preview/diff conventions in api.ts
 * (JSON-stringified `filters`/`rules` query params).
 */

import { api, type PreviewSource } from './api';

/** Persisted FilterSpec — treated as opaque JSON by the UI (the agent authors it). */
export type FilterSpecJson = Record<string, any>;

export interface SavedFilter {
    id: string;
    session_id: string;
    name: string;
    dataset_ref: string;
    spec: FilterSpecJson;
    version: number;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface FilterRule {
    id: string;
    severity: 'danger' | 'warning' | 'accent' | 'info' | 'success';
    label?: string | null;
    target: 'row' | 'cells';
}

export interface FilterCellMeta {
    k: FilterRule['severity'];
    l?: string;
    t?: string;
}

export interface FilterRowMeta {
    g?: number;
    s?: FilterRule['severity'];
    l?: string;
    t?: string;
    m: boolean;
}

export interface FilterAnnotationStatus {
    step_id: string;
    status: 'ok' | 'error' | 'skipped';
    rows: number;
    coverage: number;
    error?: string | null;
    cached?: boolean;
}

export interface FilterSummary {
    matched_rows: number;
    total_rows: number;
    approx: boolean;
    rule_counts: Record<string, number>;
    group_count?: number | null;
    identity_resolved: string;
    degraded?: string | null;
    warning?: string | null;
    annotations: FilterAnnotationStatus[];
    group_by?: string | null;
    engine?: 'pandas' | 'duckdb' | 'hybrid' | null;
    scope?: 'full' | 'sample' | 'bounded' | null;
    freshness?: 'live' | 'snapshot' | null;
    source_row_limit?: number | null;
    source_truncated?: boolean | null;
    schema_complete?: boolean | null;
}

export interface FilterPageResponse {
    kind: 'filter';
    columns: Array<{ name: string; dtype: string; kind: string; origin?: string }>;
    rows: Array<Array<string | number | boolean | null>>;
    cell_meta: Array<Array<FilterCellMeta | null>>;
    row_meta: Array<FilterRowMeta | null>;
    total_rows: number;
    filtered_rows: number;
    page: number;
    total_pages: number;
    summary: FilterSummary;
    engine?: 'pandas' | 'duckdb' | 'hybrid' | null;
    scope?: 'full' | 'sample' | 'bounded' | null;
    freshness?: 'live' | 'snapshot' | null;
    source_row_limit?: number | null;
    source_truncated?: boolean | null;
    schema_complete?: boolean | null;
}

export interface FilterApplyParams {
    filter_id: string;
    source: 'file' | 'dataset' | 'sql';
    path?: string;
    file_id?: string;
    source_id?: string;
    page?: number;
    page_size?: number;
    matches_only?: boolean;
    rules?: string[];
    sort_by?: string;
    sort_dir?: 'asc' | 'desc';
    search?: string;
    filters?: Record<string, string>;
}

export const listFilters = async (
    sessionId: string,
    datasetRef?: string
): Promise<SavedFilter[]> => {
    const response = await api.get(`/api/filters/sessions/${sessionId}`, {
        params: { dataset_ref: datasetRef || undefined },
    });
    return response.data.filters;
};

export const getFilter = async (filterId: string): Promise<SavedFilter> => {
    const response = await api.get(`/api/filters/${filterId}`);
    return response.data;
};

export const updateFilter = async (
    filterId: string,
    spec: FilterSpecJson,
    name?: string
): Promise<SavedFilter> => {
    const response = await api.put(`/api/filters/${filterId}`, { spec, name });
    return response.data;
};

export const deleteFilter = async (filterId: string): Promise<void> => {
    await api.delete(`/api/filters/${filterId}`);
};

export const getFilterPage = async (
    sessionId: string,
    params: FilterApplyParams,
    signal?: AbortSignal
): Promise<FilterPageResponse> => {
    const { filters, rules, ...rest } = params;
    const response = await api.get(`/api/filters/sessions/${sessionId}/apply`, {
        params: {
            ...rest,
            rules: rules ? JSON.stringify(rules) : undefined,
            filters:
                filters && Object.keys(filters).length > 0 ? JSON.stringify(filters) : undefined,
        },
        signal,
    });
    return response.data;
};

/** Source→apply params, mirroring how diffProvider maps PreviewSource kinds. */
export const sourceApplyParams = (
    source: PreviewSource
): Pick<FilterApplyParams, 'source' | 'path' | 'file_id' | 'source_id'> => ({
    source: source.kind,
    path: source.kind === 'file' ? source.id : undefined,
    file_id: source.kind === 'dataset' ? source.id : undefined,
    source_id: source.kind === 'sql' ? source.id : undefined,
});
