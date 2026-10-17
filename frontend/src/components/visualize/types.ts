/**
 * Wire-contract types for the visualize subsystem.
 *
 * SOURCE OF TRUTH: backend Pydantic models (viz/spec.py, viz/catalog.py) flow
 * through scripts/export_contract_schemas.py -> `npm run codegen` into
 * src/generated/contracts/. This module re-exports them under the stable names
 * the UI uses, tightening optionality where the wire format always carries the
 * field (Pydantic defaults are omitted from JSON-Schema `required`, but
 * model_dump always emits them). Only UI-side types live here by hand.
 */

import type * as Gen from '../../generated/contracts/vizSpecSchema';
import type {
    Aggregate,
    ColorChannel,
    ColorMode,
    FieldType,
    MarkType,
    SortDir,
    TexturePattern,
    TimeUnit,
} from '../../generated/contracts/vizLiterals';

export type {
    Aggregate,
    ColorChannel,
    ColorMode,
    FieldType,
    MarkType,
    Predicate,
    SortDir,
    TexturePattern,
    TimeUnit,
} from '../../generated/contracts/vizLiterals';

export type { FieldCatalog, FieldInfo } from '../../generated/contracts/fieldCatalog';
import type { SpecError as SpecErrorItem } from '../../generated/contracts/specError';

// -- Spec skeleton (required-ness tightened to the real wire shape) ----------

export interface VizSpec extends Gen.VizSpec {
    schema_version?: 1;
    title?: string | null;
    data?: DataRef;
    transform?: Transform[];
    mark: MarkDef;
    encoding: Encoding;
    config?: VizConfig;
}

export interface DataRef extends Gen.DataRef {
    source: string;
}

export interface FilterTransform extends Gen.FilterTransform {
    op: 'filter';
}
export interface FilterAnyTransform extends Gen.FilterAnyTransform {
    op: 'filter_any';
}
export interface CalculateTransform extends Gen.CalculateTransform {
    op: 'calculate';
    expr: string;
    as: string;
}
/** Filters run BEFORE aggregation; calculated fields are projected first. */
export type Transform = FilterTransform | FilterAnyTransform | CalculateTransform;

export function isFilter(t: Transform): t is FilterTransform {
    return t.op === 'filter';
}

export function isFilterAny(t: Transform): t is FilterAnyTransform {
    return t.op === 'filter_any';
}

export function isCalculate(t: Transform): t is CalculateTransform {
    return t.op === 'calculate';
}

export interface MarkDef extends Gen.MarkDef {
    type: MarkType;
}
export interface Encoding extends Gen.Encoding {
    x?: ChannelDef | null;
    y?: ChannelDef | null;
    color?: ChannelDef | null;
    size?: ChannelDef | null;
    tooltip?: ChannelDef[];
    row?: ChannelDef | null;
    column?: ChannelDef | null;
}
export interface ChannelDef extends Gen.ChannelDef {
    field: string | null;
    type: FieldType;
    aggregate?: Aggregate | null;
    timeUnit?: TimeUnit | null;
    bin?: BinDef | null;
    sort?: Sort | null;
    topK?: TopKDef | null;
}
export interface BinDef extends Gen.BinDef {
    maxbins: number;
}
/** Sort a channel BY another channel's value (Vega-Lite sort-by-encoding),
 *  e.g. bars ordered by their measure: { encoding: 'y', order: 'desc' }. */
export interface SortByEncoding extends Gen.SortByEncoding {
    encoding: SortChannel;
    order: SortDir;
}
export type Sort = SortDir | SortByEncoding;
export interface TopKDef extends Gen.TopKDef {
    n: number;
    others: boolean;
}

export interface VizConfig extends Gen.VizConfig {
    color?: ColorConfig | null;
    /** Legacy v1 palette. New editor writes use `color`. */
    palette?: string[] | null;
    texture?: TextureConfig;
    legend?: boolean;
    grid?: boolean;
    labels?: boolean;
    labelFontSize?: number | null;
    labelColor?: string | null;
    numberFormat?: string | null;
}
export interface ColorConfig extends Gen.ColorConfig {
    mode: ColorMode;
    value?: string | null;
    palette?: string[] | null;
    binding?: ColorBinding | null;
    assignments?: ColorAssignment[];
}
export interface ColorBinding extends Gen.ColorBinding {
    channel: ColorChannel;
    field: string | null;
}
export interface ColorAssignment extends Gen.ColorAssignment {
    value: ColorValue;
    color: string;
}
export interface TextureConfig extends Gen.TextureConfig {
    enabled: boolean;
    pattern: TexturePattern;
}

// ---------------------------------------------------------------------------
// Editor shelves
// ---------------------------------------------------------------------------

/** Channel keys the editor exposes as shelves (order = shelf order). */
export const SHELF_CHANNELS = ['x', 'y', 'color', 'size'] as const;
export type ShelfChannel = (typeof SHELF_CHANNELS)[number];
export type SortChannel = (typeof SHELF_CHANNELS)[number];
export type ColorValue = string | number | boolean | null;

// ---------------------------------------------------------------------------
// Materialized data (tidy-table contract with backend/viz/plan.py)
// ---------------------------------------------------------------------------

export interface VizColumn {
    name: string; // channel key: x | y | color | size | tooltip_N
    field: string | null;
    kind: 'dimension' | 'measure';
    type: FieldType;
}

export interface VizData {
    columns: VizColumn[];
    rows: Array<Array<string | number | null>>;
    meta: {
        row_count: number;
        truncated: boolean;
        engine: 'pandas' | 'sql' | 'mongodb' | 'duckdb';
        bins?: Record<string, { width: number; min?: number; max?: number }>;
        scope?: 'full' | 'sample' | 'bounded';
        freshness?: 'live' | 'snapshot';
        source_truncated?: boolean;
        source_row_limit?: number | null;
    };
}

// ---------------------------------------------------------------------------
// Persisted visualization entities
// ---------------------------------------------------------------------------

export interface SavedViz {
    id: string;
    session_id: string;
    dataset_ref: string | null;
    title: string | null;
    spec: VizSpec;
    version: number;
    created_at: string | null;
    updated_at: string | null;
    /** False when the dataset this chart was built from has been removed. */
    source_available?: boolean;
}

export interface VizVersion {
    version: number;
    spec: VizSpec;
    author: 'user' | 'agent';
    created_at: string | null;
}

/** Structured validation problem from the viz API (backend viz.spec.SpecError). */
export type { SpecError as SpecErrorItem } from '../../generated/contracts/specError';

// ---------------------------------------------------------------------------
// "Ask this chart" (backend/viz/selection.py + inquiry.py wire formats)
// ---------------------------------------------------------------------------

export interface SelectionRangePayload {
    min: unknown;
    max: unknown;
    /** True when max is a bucket start (temporal buckets): match < max. */
    exclusiveMax?: boolean;
}

export interface ChannelSelectionPayload {
    channel: ShelfChannel;
    values?: unknown[];
    range?: SelectionRangePayload;
}

export interface SelectionClausePayload {
    items: ChannelSelectionPayload[];
}

export interface SelectionPayload {
    clauses: SelectionClausePayload[];
}

export type AskMode = 'explain' | 'compare' | 'drill' | 'custom';

export interface AskMeasureStat {
    channel: string;
    field: string | null;
    selected_sum?: number;
    selected_avg?: number;
    total_sum?: number;
    overall_avg?: number;
    selected_share?: number;
}

export interface AskDriverFinding {
    field: string;
    segment: string;
    selected_share: number;
    overall_share: number;
    lift: number | null;
}

export interface AskResponse {
    headline: string;
    insight: string;
    selection: string;
    stats: {
        selected_points: number;
        total_points: number;
        measures: AskMeasureStat[];
    };
    drivers: AskDriverFinding[];
    drill: { viz_id: string; title: string } | null;
}

/** Structured 422 payload from the viz API. */
export function specErrorsFromAxios(err: unknown): SpecErrorItem[] | null {
    const detail = (err as any)?.response?.data?.detail;
    if (detail && Array.isArray(detail.errors)) return detail.errors as SpecErrorItem[];
    return null;
}

export function emptySpec(): VizSpec {
    return {
        schema_version: 1,
        title: null,
        data: { source: 'active' },
        transform: [],
        mark: { type: 'bar', stacked: false, smooth: false, point: false },
        encoding: { x: null, y: null, color: null, size: null, tooltip: [] },
        config: {
            color: null,
            palette: null,
            texture: { enabled: false, pattern: 'diagonal' },
            legend: true,
            grid: true,
            labels: false,
            labelFontSize: null,
            labelColor: null,
            numberFormat: null,
        },
    };
}
