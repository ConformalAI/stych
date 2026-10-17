import { describe, expect, it } from 'vitest';
import { applyFieldDrop, setScatterMeasureAggregation } from './dropRules';
import { emptySpec, type FieldCatalog, type FieldInfo, type VizSpec } from './types';

const REGION: FieldInfo = { name: 'region', type: 'nominal', role: 'dimension' };
const ORDER_DATE: FieldInfo = { name: 'order_date', type: 'temporal', role: 'dimension' };
const REVENUE: FieldInfo = {
    name: 'revenue',
    type: 'quantitative',
    role: 'measure',
    default_aggregate: 'sum',
};
const USER_ID: FieldInfo = {
    name: 'user_id',
    type: 'quantitative',
    role: 'dimension',
    is_id: true,
};
const CATALOG: FieldCatalog = {
    fingerprint: 'test',
    built_at: '2026-01-01T00:00:00Z',
    engine: 'pandas',
    row_count: 1,
    fields: [REGION, REVENUE, USER_ID],
};

function withMark(mark: VizSpec['mark']['type'], encoding: VizSpec['encoding'] = {}): VizSpec {
    return { ...emptySpec(), mark: { type: mark }, encoding };
}

describe('literal shelf placement', () => {
    it('puts a dimension only on the selected x shelf', () => {
        const out = applyFieldDrop(withMark('bar'), REGION, 'x');
        expect(out.encoding.x).toMatchObject({ field: 'region', type: 'nominal' });
        expect(out.encoding.y ?? null).toBeNull();
    });

    it('puts a measure only on the selected y shelf with its default aggregate', () => {
        const out = applyFieldDrop(withMark('bar'), REVENUE, 'y');
        expect(out.encoding.y).toMatchObject({ field: 'revenue', aggregate: 'sum' });
        expect(out.encoding.x ?? null).toBeNull();
    });

    it('does not move or replace the opposite shelf', () => {
        const start = withMark('bar', {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'quantity', type: 'quantitative', aggregate: 'sum' },
        });
        const out = applyFieldDrop(start, REVENUE, 'x');
        expect(out.encoding.x).toMatchObject({ field: 'revenue', aggregate: 'sum' });
        expect(out.encoding.y).toEqual(start.encoding.y);
    });

    it('clears per-value colors when their bound shelf gets a different field', () => {
        const start = withMark('bar', {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'quantity', type: 'quantitative', aggregate: 'sum' },
        });
        start.config = {
            ...start.config,
            color: {
                mode: 'categorical',
                binding: { channel: 'x', field: 'region' },
                assignments: [{ value: 'East', color: '#DC2626' }],
            },
        };

        expect(applyFieldDrop(start, ORDER_DATE, 'x').config?.color).toBeNull();
    });

    it('supports a horizontal bar by honoring a dimension dropped on y', () => {
        const start = withMark('bar', {
            x: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
        });
        const out = applyFieldDrop(start, REGION, 'y');
        expect(out.encoding.x).toEqual(start.encoding.x);
        expect(out.encoding.y).toMatchObject({ field: 'region', type: 'nominal' });
    });

    it('keeps a temporal field on y without creating x', () => {
        const out = applyFieldDrop(withMark('bar'), ORDER_DATE, 'y');
        expect(out.encoding.y).toMatchObject({ field: 'order_date' });
        expect(out.encoding.x ?? null).toBeNull();
    });

    it('keeps an ID-like numeric field as a dimension on its target shelf', () => {
        const out = applyFieldDrop(withMark('bar'), USER_ID, 'x');
        expect(out.encoding.x).toMatchObject({
            field: 'user_id',
            type: 'quantitative',
        });
        expect(out.encoding.x?.aggregate).toBeUndefined();
    });
});

describe('mark-local defaults', () => {
    it('aggregates scatter measures by default', () => {
        const x = applyFieldDrop(withMark('scatter'), REVENUE, 'x');
        const size = applyFieldDrop(withMark('scatter'), REVENUE, 'size');
        expect(x.encoding.x).toMatchObject({
            field: 'revenue',
            type: 'quantitative',
            aggregate: 'sum',
        });
        expect(size.encoding.size?.aggregate).toBe('sum');
    });

    it('toggles scatter measures between aggregate and raw modes', () => {
        const chart = withMark('scatter', {
            x: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            y: { field: 'revenue', type: 'quantitative', aggregate: 'mean' },
            color: { field: 'region', type: 'nominal' },
        });

        const raw = setScatterMeasureAggregation(chart, CATALOG, false);
        expect(raw.encoding.x?.aggregate).toBeNull();
        expect(raw.encoding.y?.aggregate).toBeNull();
        expect(raw.encoding.color).toEqual(chart.encoding.color);

        const aggregated = setScatterMeasureAggregation(raw, CATALOG, true);
        expect(aggregated.encoding.x?.aggregate).toBe('sum');
        expect(aggregated.encoding.y?.aggregate).toBe('sum');
    });

    it('bins a numeric field dropped on histogram x without creating y', () => {
        const out = applyFieldDrop(withMark('histogram'), REVENUE, 'x');
        expect(out.encoding.x).toMatchObject({ field: 'revenue', bin: { maxbins: 10 } });
        expect(out.encoding.y ?? null).toBeNull();
    });

    it('does not reroute a heatmap measure dropped on y to color', () => {
        const start = withMark('heatmap', {
            x: { field: 'region', type: 'nominal' },
            color: { field: 'quantity', type: 'quantitative', aggregate: 'sum' },
        });
        const out = applyFieldDrop(start, REVENUE, 'y');
        expect(out.encoding.y).toMatchObject({ field: 'revenue', type: 'quantitative' });
        expect(out.encoding.color).toEqual(start.encoding.color);
    });
});
