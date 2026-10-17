/**
 * Chart-selection layer for "Ask this chart".
 *
 * Selection state is a set of tidy-table row indices (the VizData contract),
 * fed by chart events via the `__row` indices compileSpec embeds in every
 * datum. Pure functions only:
 *  - toggleRows: click/shift-click accumulation
 *  - buildSelectionPayload: rows → the backend Selection wire format
 *    (spec-vocabulary values/ranges, exact and replayable — never pixels)
 *  - selectionSummary: instant deterministic stats, no server round-trip
 *  - applySelectionStyle: dims unselected marks on a compiled ECharts option
 */

import { formatNumber } from './compileSpec';
import {
    SHELF_CHANNELS,
    type ChannelSelectionPayload,
    type SelectionPayload,
    type ShelfChannel,
    type VizData,
    type VizSpec,
} from './types';

export function toggleRows(
    current: ReadonlySet<number>,
    rows: number[],
    additive: boolean
): Set<number> {
    if (!additive) {
        // Plain click on an already (and only) selected mark clears it.
        const same =
            rows.length > 0 && current.size === rows.length && rows.every((r) => current.has(r));
        return same ? new Set() : new Set(rows);
    }
    const next = new Set(current);
    const allIn = rows.every((r) => next.has(r));
    for (const r of rows) {
        if (allIn) next.delete(r);
        else next.add(r);
    }
    return next;
}

// ---------------------------------------------------------------------------
// Payload
// ---------------------------------------------------------------------------

function dimColumns(spec: VizSpec, data: VizData) {
    return data.columns
        .map((col, idx) => ({ col, idx }))
        .filter(
            ({ col }) =>
                col.kind === 'dimension' &&
                (SHELF_CHANNELS as readonly string[]).includes(col.name) &&
                Boolean(spec.encoding[col.name as ShelfChannel]?.field)
        );
}

/**
 * Selected rows → exact OR-of-AND dimension clauses. Keeping each row's
 * dimension tuple avoids turning, for example, two heatmap cells into all
 * combinations of their x and y values.
 */
export function buildSelectionPayload(
    spec: VizSpec,
    data: VizData,
    selected: ReadonlySet<number>
): SelectionPayload | null {
    if (selected.size === 0) return null;
    const dims = dimColumns(spec, data);

    // A scatter brush is a geometric rectangle, so the two axis ranges are an
    // exact conjunction. Keeping color out deliberately selects every color
    // inside that rectangle rather than creating color-specific tuples.
    if (spec.mark.type === 'scatter') {
        const items = dims
            .filter(({ col }) => col.name === 'x' || col.name === 'y')
            .flatMap(({ col, idx }) => {
                if (col.type !== 'quantitative') return [];
                const nums = [...selected]
                    .map((ri) => Number(data.rows[ri]?.[idx]))
                    .filter(Number.isFinite);
                if (nums.length === 0) return [];
                return [
                    {
                        channel: col.name as ShelfChannel,
                        range: { min: Math.min(...nums), max: Math.max(...nums) },
                    },
                ];
            });
        return items.length > 0 ? { clauses: [{ items }] } : null;
    }

    const clauses: SelectionPayload['clauses'] = [];
    const seen = new Set<string>();
    for (const ri of selected) {
        const row = data.rows[ri];
        if (!row) continue;
        const items: ChannelSelectionPayload[] = [];
        for (const { col, idx } of dims) {
            const channel = col.name as ShelfChannel;
            const ch = spec.encoding[channel]!;
            const value = row[idx];
            if (value == null) continue;
            if (ch.bin) {
                const width = data.meta.bins?.[channel]?.width ?? 0;
                const start = Number(value);
                if (!Number.isFinite(start) || !Number.isFinite(width) || width <= 0) continue;
                items.push({ channel, range: { min: start, max: start + width } });
            } else {
                // A one-value item is exact for nominal, temporal, and raw
                // quantitative dimensions. Time buckets expand server-side.
                items.push({ channel, values: [value] });
            }
        }
        if (items.length === 0) continue;
        const key = JSON.stringify(items);
        if (!seen.has(key)) {
            seen.add(key);
            clauses.push({ items });
        }
    }
    return clauses.length > 0 ? { clauses } : null;
}

// ---------------------------------------------------------------------------
// Instant stats (client-side, deterministic)
// ---------------------------------------------------------------------------

export interface SelectionSummary {
    count: number;
    total: number;
    chips: string[];
}

function measureLabel(spec: VizSpec, channel: string, field: string | null): string {
    const ch = spec.encoding[channel as ShelfChannel];
    if (ch?.aggregate === 'count' || (!field && ch?.aggregate)) return 'count';
    if (ch?.aggregate) return `${ch.aggregate}(${field ?? ''})`;
    return field ?? channel;
}

export function selectionSummary(
    spec: VizSpec,
    data: VizData,
    selected: ReadonlySet<number>
): SelectionSummary {
    const chips: string[] = [];
    for (const [ci, col] of data.columns.entries()) {
        if (col.kind !== 'measure') continue;
        let selSum = 0;
        let totSum = 0;
        let selN = 0;
        data.rows.forEach((row, ri) => {
            const v = row[ci];
            if (typeof v !== 'number') return;
            totSum += v;
            if (selected.has(ri)) {
                selSum += v;
                selN += 1;
            }
        });
        if (selN === 0) continue;
        const label = measureLabel(spec, col.name, col.field);
        const agg = spec.encoding[col.name as ShelfChannel]?.aggregate;
        if ((agg === 'sum' || agg === 'count') && totSum !== 0) {
            chips.push(
                `${formatNumber(selSum)} · ${Math.round((selSum / totSum) * 100)}% of ${label}`
            );
        } else {
            chips.push(`avg ${label}: ${formatNumber(selSum / selN)}`);
        }
    }
    return { count: selected.size, total: data.rows.length, chips };
}

// ---------------------------------------------------------------------------
// Highlight styling on a compiled option
// ---------------------------------------------------------------------------

const DIM_OPACITY = 0.22;

/** Dim unselected marks. Pure: returns a new option; no-op when nothing selected. */
export function applySelectionStyle(
    option: Record<string, any>,
    selected: ReadonlySet<number>
): Record<string, any> {
    if (selected.size === 0) return option;
    const seriesList = Array.isArray(option.series) ? option.series : [option.series];
    const styled = seriesList.map((s: any) => {
        if (!s || !Array.isArray(s.data)) return s;
        return {
            ...s,
            data: s.data.map((d: any) => {
                if (d == null || typeof d !== 'object' || d.__row == null) return d;
                const on = selected.has(d.__row);
                return {
                    ...d,
                    itemStyle: { ...(d.itemStyle ?? {}), opacity: on ? 1 : DIM_OPACITY },
                };
            }),
        };
    });
    return { ...option, series: Array.isArray(option.series) ? styled : styled[0] };
}

/** Rows behind an ECharts event params object (datum carries __row). */
export function rowsFromEventParams(params: any): number[] {
    const row = params?.data?.__row;
    return typeof row === 'number' ? [row] : [];
}
