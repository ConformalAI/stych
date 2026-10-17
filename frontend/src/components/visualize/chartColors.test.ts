import { describe, expect, it } from 'vitest';
import { makeColorConfig } from './chartColors';
import { emptySpec, type VizData, type VizSpec } from './types';

const heatmapData: VizData = {
    columns: [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'segment', kind: 'dimension', type: 'nominal' },
        { name: 'color', field: 'revenue', kind: 'measure', type: 'quantitative' },
    ],
    rows: [['East', 'Consumer', 10]],
    meta: { row_count: 1, truncated: false, engine: 'pandas' },
};

describe('color mode state', () => {
    it('preserves a custom gradient when switching to single color and back', () => {
        const continuous: VizSpec = {
            ...emptySpec(),
            mark: { type: 'heatmap' },
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'segment', type: 'nominal' },
                color: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            },
            config: {
                ...emptySpec().config,
                color: {
                    mode: 'continuous',
                    binding: { channel: 'color', field: 'revenue' },
                    palette: ['#FFFFFF', '#2563EB', '#111827'],
                },
            },
        };

        const single = makeColorConfig(continuous, 'single', heatmapData);
        expect(single).toMatchObject({
            mode: 'single',
            value: '#FFFFFF',
            palette: ['#FFFFFF', '#2563EB', '#111827'],
        });

        const switchedBack = makeColorConfig(
            {
                ...continuous,
                config: { ...continuous.config, color: single },
            },
            'continuous',
            heatmapData
        );
        expect(switchedBack?.palette).toEqual(['#FFFFFF', '#2563EB', '#111827']);
    });

    it('preserves categorical assignments while temporarily using one color', () => {
        const categorical: VizSpec = {
            ...emptySpec(),
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            },
            config: {
                ...emptySpec().config,
                color: {
                    mode: 'categorical',
                    binding: { channel: 'x', field: 'region' },
                    palette: ['#2563EB', '#0FB981'],
                    assignments: [{ value: 'East', color: '#DC2626' }],
                },
            },
        };
        const data: VizData = {
            columns: [
                { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
            ],
            rows: [
                ['East', 10],
                ['West', 20],
            ],
            meta: { row_count: 2, truncated: false, engine: 'pandas' },
        };

        const single = makeColorConfig(categorical, 'single', data);
        const switchedBack = makeColorConfig(
            {
                ...categorical,
                config: { ...categorical.config, color: single },
            },
            'categorical',
            data
        );

        expect(switchedBack?.assignments).toEqual([{ value: 'East', color: '#DC2626' }]);
        expect(switchedBack?.palette).toEqual(['#2563EB', '#0FB981']);
    });
});
