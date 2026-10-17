import { describe, expect, it } from 'vitest';
import { emptySpec, type SavedViz, type VizSpec } from './types';
import {
    filterSavedVisualizations,
    intermediateFieldForSpec,
    isQueryable,
    nextNewChartTitle,
} from './useVizSpecTypes';

const dimension = { field: 'region', type: 'nominal' as const };
const measure = {
    field: 'revenue',
    type: 'quantitative' as const,
    aggregate: 'sum' as const,
};
const rawNumber = { field: 'revenue', type: 'quantitative' as const };

function spec(mark: VizSpec['mark']['type'], encoding: VizSpec['encoding']): VizSpec {
    return { ...emptySpec(), mark: { type: mark }, encoding };
}

describe('isQueryable', () => {
    it('waits for both scatter axes instead of sending a guaranteed-invalid request', () => {
        expect(isQueryable(spec('scatter', { x: rawNumber }))).toBe(false);
        expect(isQueryable(spec('scatter', { x: rawNumber, y: rawNumber }))).toBe(true);
    });

    it('waits for the complete heatmap shape', () => {
        expect(isQueryable(spec('heatmap', { x: dimension, y: dimension }))).toBe(false);
        expect(isQueryable(spec('heatmap', { x: dimension, y: dimension, color: measure }))).toBe(
            true
        );
    });

    it('queries dimension-only charts that the backend completes with count', () => {
        expect(isQueryable(spec('bar', { x: dimension }))).toBe(true);
        expect(isQueryable(spec('line', { x: dimension }))).toBe(true);
        expect(isQueryable(spec('pie', { x: dimension }))).toBe(true);
        expect(isQueryable(spec('bar', { x: dimension, y: measure }))).toBe(true);
        expect(isQueryable(spec('line', { x: dimension, y: measure }))).toBe(true);
        expect(isQueryable(spec('pie', { x: dimension, y: measure }))).toBe(true);
    });

    it('keeps histogram count as a derived rendering detail', () => {
        expect(
            isQueryable(
                spec('histogram', {
                    x: { ...rawNumber, bin: { maxbins: 10 } },
                })
            )
        ).toBe(true);
    });

    it('does not send measure-only cartesian drafts', () => {
        expect(isQueryable(spec('bar', { y: measure }))).toBe(false);
        expect(isQueryable(spec('line', { y: measure }))).toBe(false);
    });

    it('allows the horizontal bar layout', () => {
        expect(isQueryable(spec('bar', { x: measure, y: dimension }))).toBe(true);
    });
});

describe('nextNewChartTitle', () => {
    it('uses a stable integer after saved and unsaved default names', () => {
        const named = (title: string) => ({ title }) as SavedViz;
        expect(nextNewChartTitle([])).toBe('New Chart 1');
        expect(nextNewChartTitle([named('Revenue')])).toBe('New Chart 2');
        expect(nextNewChartTitle([named('New Chart 4'), named('Custom')], 'New Chart 5')).toBe(
            'New Chart 6'
        );
    });
});

describe('visualization source scoping', () => {
    it('keeps only charts created for the active dataset', () => {
        const chart = (id: string, dataset_ref: string | null) =>
            ({ ...({ id } as SavedViz), dataset_ref }) as SavedViz;
        expect(
            filterSavedVisualizations(
                [chart('csv-chart', 'csv-1'), chart('postgres-chart', 'pg-1')],
                'pg-1'
            ).map((row) => row.id)
        ).toEqual(['postgres-chart']);
    });
});

describe('intermediateFieldForSpec', () => {
    it('does not replace a rendered dimension-only pie with the intermediate table', () => {
        const pie = spec('pie', { x: dimension });
        const canonicalPie = {
            ...pie,
            encoding: {
                ...pie.encoding,
                y: { field: null, type: 'quantitative' as const, aggregate: 'count' as const },
            },
        };
        const catalog = {
            source: 'dataset-1',
            fingerprint: 'f',
            built_at: '',
            engine: 'pandas' as const,
            row_count: 1,
            fields: [{ name: 'region', role: 'dimension' as const, type: 'nominal' as const }],
        };

        expect(intermediateFieldForSpec(pie, catalog, canonicalPie)).toBeNull();
        expect(intermediateFieldForSpec(pie, catalog, null)?.name).toBe('region');
    });
});
