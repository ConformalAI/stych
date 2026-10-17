/**
 * Data-provider contract for the preview grid.
 *
 * SpreadsheetGrid renders exclusively from these shapes and never talks to the
 * API directly, so alternative providers (e.g. a future DiffProvider that
 * composes two sources and fills `cellMeta`) can feed the same grid unchanged.
 */

export interface PreviewQuery {
    offset: number;
    limit: number;
    sortBy?: string;
    sortDir?: 'asc' | 'desc';
    search?: string;
    filters?: Record<string, string>;
}

export interface PreviewColumn {
    name: string;
    dtype: string;
    kind: 'number' | 'string' | 'date' | 'bool' | 'other';
    /** Diff views only: how this column changed between base and target. */
    status?: 'common' | 'added' | 'renamed';
    /** Diff views only: short annotation, e.g. "standardized", "renamed from x". */
    note?: string | null;
}

export type CellValue = string | number | boolean | null;

export type DiffKind = 'added' | 'removed' | 'changed' | 'imputed' | 'transformed';

export type FilterSeverity = 'danger' | 'warning' | 'accent' | 'info' | 'success';

/** Per-cell annotations. Unused by the API provider; diff/filter providers fill it. */
export interface CellMeta {
    diff?: DiffKind;
    /** Filter views only: highlight severity (takes precedence over `diff`). */
    severity?: FilterSeverity;
    /** Short in-cell phrase, e.g. "mean imputed". */
    label?: string;
    tooltip?: string;
}

/** Per-row annotations (filter views): group banding + row highlight. */
export interface RowMeta {
    /** Per-result group ordinal — rows sharing it band together (duplicate pairs). */
    group?: number | null;
    severity?: FilterSeverity | null;
    label?: string | null;
    tooltip?: string | null;
    /** Whether the row matched the filter's select predicate. */
    matched?: boolean;
}

export interface PreviewPage {
    columns: PreviewColumn[];
    rows: CellValue[][];
    /** Aligned to `rows`; null/absent = no annotation. */
    cellMeta?: (CellMeta | null)[][];
    /** Aligned to `rows`; filter views only. */
    rowMeta?: (RowMeta | null)[];
    /** Rows in the source before any filter/search. */
    totalRows: number;
    /** Rows after filter/search — drives paging. */
    filteredRows: number;
    /** True when the source was capped on load (huge files). */
    truncated?: boolean;
    /** True when totalRows/filteredRows are estimates (exact count timed out). */
    approx?: boolean;
}

export interface ColumnStats {
    nulls: number;
    unique?: number | null;
    min?: CellValue;
    max?: CellValue;
    mean?: number | null;
}

export interface PreviewFetchResult extends PreviewPage {
    stats?: Record<string, ColumnStats>;
}

export interface PreviewCapabilities {
    sort: boolean;
    filter: boolean;
    search: boolean;
    stats: boolean;
}

export interface PreviewDataProvider {
    /** Identity — consumers reset paging/filters when it changes. */
    key: string;
    /** Which grid controls this provider supports; defaults to all. */
    capabilities?: Partial<PreviewCapabilities>;
    fetchPage(
        query: PreviewQuery,
        opts?: { includeStats?: boolean; signal?: AbortSignal }
    ): Promise<PreviewFetchResult>;
}
