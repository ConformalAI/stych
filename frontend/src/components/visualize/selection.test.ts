import { describe, expect, it } from 'vitest';
import {
    applySelectionStyle,
    buildSelectionPayload,
    rowsFromEventParams,
    selectionSummary,
    toggleRows,
} from './selection';
import type { VizData, VizSpec } from './types';

function spec(partial: Partial<VizSpec>): VizSpec {
    return {
        schema_version: 1,
        data: { source: 'active' },
        transform: [],
        mark: { type: 'bar' },
        encoding: {},
        config: {},
        ...partial,
    };
}

function data(columns: VizData['columns'], rows: VizData['rows'], meta = {}): VizData {
    return {
        columns,
        rows,
        meta: { row_count: rows.length, truncated: false, engine: 'pandas', ...meta },
    };
}

const BAR_SPEC = spec({
    encoding: {
        x: { field: 'region', type: 'nominal' },
        y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
    },
});

const BAR_DATA = data(
    [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
    ],
    [
        ['EU', 300],
        ['NA', 150],
        ['APAC', 50],
    ]
);

describe('toggleRows', () => {
    it('replaces on plain click and toggles off on re-click', () => {
        const one = toggleRows(new Set(), [1], false);
        expect([...one]).toEqual([1]);
        expect([...toggleRows(one, [2], false)]).toEqual([2]);
        expect(toggleRows(one, [1], false).size).toBe(0); // re-click clears
    });

    it('accumulates and removes with additive clicks', () => {
        const two = toggleRows(new Set([0]), [1], true);
        expect([...two].sort()).toEqual([0, 1]);
        expect([...toggleRows(two, [1], true)]).toEqual([0]);
    });
});

describe('buildSelectionPayload', () => {
    it('maps nominal x selection to channel values', () => {
        const payload = buildSelectionPayload(BAR_SPEC, BAR_DATA, new Set([0, 2]));
        expect(payload).toEqual({
            clauses: [
                { items: [{ channel: 'x', values: ['EU'] }] },
                { items: [{ channel: 'x', values: ['APAC'] }] },
            ],
        });
    });

    it('sends timeUnit bucket starts as values (backend expands buckets)', () => {
        const s = spec({
            mark: { type: 'line' },
            encoding: {
                x: { field: 'order_date', type: 'temporal', timeUnit: 'month' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            },
        });
        const d = data(
            [
                { name: 'x', field: 'order_date', kind: 'dimension', type: 'temporal' },
                { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
            ],
            [
                ['2024-01-01T00:00:00', 10],
                ['2024-02-01T00:00:00', 20],
            ]
        );
        const payload = buildSelectionPayload(s, d, new Set([1]));
        expect(payload).toEqual({
            clauses: [{ items: [{ channel: 'x', values: ['2024-02-01T00:00:00'] }] }],
        });
    });

    it('maps binned x to a start..start+width range', () => {
        const s = spec({
            mark: { type: 'histogram' },
            encoding: {
                x: { field: 'revenue', type: 'quantitative', bin: { maxbins: 5 } },
                y: { field: null, type: 'quantitative', aggregate: 'count' },
            },
        });
        const d = data(
            [
                { name: 'x', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                { name: 'y', field: null, kind: 'measure', type: 'quantitative' },
            ],
            [
                [0, 3],
                [50, 4],
                [100, 2],
            ],
            { bins: { x: { width: 50 } } }
        );
        const payload = buildSelectionPayload(s, d, new Set([0, 1]));
        expect(payload).toEqual({
            clauses: [
                { items: [{ channel: 'x', range: { min: 0, max: 50 } }] },
                { items: [{ channel: 'x', range: { min: 50, max: 100 } }] },
            ],
        });
    });

    it('maps raw numeric axes (scatter) to min/max ranges per axis', () => {
        const s = spec({
            mark: { type: 'scatter' },
            encoding: {
                x: { field: 'qty', type: 'quantitative' },
                y: { field: 'revenue', type: 'quantitative' },
            },
        });
        const d = data(
            [
                { name: 'x', field: 'qty', kind: 'dimension', type: 'quantitative' },
                { name: 'y', field: 'revenue', kind: 'dimension', type: 'quantitative' },
            ],
            [
                [1, 100],
                [5, 20],
                [9, 60],
            ]
        );
        const payload = buildSelectionPayload(s, d, new Set([0, 2]));
        expect(payload).toEqual({
            clauses: [
                {
                    items: [
                        { channel: 'x', range: { min: 1, max: 9 } },
                        { channel: 'y', range: { min: 60, max: 100 } },
                    ],
                },
            ],
        });
    });

    it('keeps selected stacked-bar tuples correlated', () => {
        const s = spec({
            mark: { type: 'bar', stacked: true },
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                color: { field: 'channel', type: 'nominal' },
            },
        });
        const d = data(
            [
                { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                { name: 'color', field: 'channel', kind: 'dimension', type: 'nominal' },
                { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
            ],
            [
                ['EU', 'web', 100],
                ['EU', 'store', 80],
                ['NA', 'web', 70],
                ['NA', 'store', 60],
            ]
        );
        expect(buildSelectionPayload(s, d, new Set([0, 3]))).toEqual({
            clauses: [
                {
                    items: [
                        { channel: 'x', values: ['EU'] },
                        { channel: 'color', values: ['web'] },
                    ],
                },
                {
                    items: [
                        { channel: 'x', values: ['NA'] },
                        { channel: 'color', values: ['store'] },
                    ],
                },
            ],
        });
    });

    it('returns null for an empty selection', () => {
        expect(buildSelectionPayload(BAR_SPEC, BAR_DATA, new Set())).toBeNull();
    });
});

describe('selectionSummary', () => {
    it('reports share of additive measures', () => {
        const s = selectionSummary(BAR_SPEC, BAR_DATA, new Set([0]));
        expect(s.count).toBe(1);
        expect(s.total).toBe(3);
        expect(s.chips).toEqual(['300 · 60% of sum(revenue)']);
    });

    it('reports averages for non-additive measures', () => {
        const meanSpec = spec({
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'mean' },
            },
        });
        const s = selectionSummary(meanSpec, BAR_DATA, new Set([0, 1]));
        expect(s.chips).toEqual(['avg mean(revenue): 225']);
    });
});

describe('applySelectionStyle', () => {
    it('dims unselected datums, leaves selected at full opacity', () => {
        const option = {
            series: [
                {
                    type: 'bar',
                    data: [
                        { value: 1, __row: 0 },
                        { value: 2, __row: 1 },
                    ],
                },
            ],
        };
        const styled = applySelectionStyle(option, new Set([1]));
        expect(styled.series[0].data[0].itemStyle.opacity).toBeLessThan(1);
        expect(styled.series[0].data[1].itemStyle.opacity).toBe(1);
        // pure: the input option is untouched
        expect(option.series[0].data[0]).toEqual({ value: 1, __row: 0 });
    });

    it('is a no-op for an empty selection', () => {
        const option = { series: [{ data: [{ value: 1, __row: 0 }] }] };
        expect(applySelectionStyle(option, new Set())).toBe(option);
    });
});

describe('rowsFromEventParams', () => {
    it('extracts the datum row index', () => {
        expect(rowsFromEventParams({ data: { __row: 4 } })).toEqual([4]);
        expect(rowsFromEventParams({ data: { value: 3 } })).toEqual([]);
        expect(rowsFromEventParams(undefined)).toEqual([]);
    });
});
