import { describe, expect, it } from 'vitest';
import {
    isMarkEncodingCompatible,
    recommendedMarkEncoding,
    transitionMark,
} from './markCapabilities';
import { emptySpec, type VizSpec } from './types';

function stackedBar(): VizSpec {
    return {
        ...emptySpec(),
        mark: { type: 'bar', stacked: true },
        encoding: {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            color: { field: 'segment', type: 'nominal' },
            size: null,
            tooltip: [],
        },
    };
}

describe('transitionMark', () => {
    it('changes chart options without changing field placement', () => {
        const original = stackedBar();
        const result = transitionMark(original, 'histogram');

        expect(result.mark).toMatchObject({ type: 'histogram', stacked: false });
        expect(result.encoding).toEqual(original.encoding);
        expect(result.encoding).toBe(original.encoding);
    });

    it('clears options that the next chart type cannot use', () => {
        const result = transitionMark(stackedBar(), 'line');
        expect(result.mark).toMatchObject({ type: 'line', stacked: false });
        expect(result.encoding).toEqual(stackedBar().encoding);
    });

    it('clears an intrinsic bar color binding when changing to a line', () => {
        const chart = stackedBar();
        chart.config = {
            ...chart.config,
            color: {
                mode: 'categorical',
                binding: { channel: 'x', field: 'region' },
                assignments: [{ value: 'East', color: '#DC2626' }],
            },
        };

        expect(transitionMark(chart, 'line').config?.color).toBeNull();
    });
});

describe('recommendedMarkEncoding', () => {
    it('offers a valid histogram arrangement without changing the current spec', () => {
        const original = transitionMark(stackedBar(), 'histogram');
        const result = recommendedMarkEncoding(original);
        if (!result) throw new Error('Expected a histogram recommendation');

        expect(result.encoding.x).toMatchObject({ field: 'revenue', bin: { maxbins: 10 } });
        expect(result.encoding.y).toMatchObject({ aggregate: 'count', field: null });
        expect(original.encoding.x?.field).toBe('region');
        expect(isMarkEncodingCompatible(result)).toBe(true);
    });

    it('offers aggregated numeric axes for a scatter chart', () => {
        const base = stackedBar();
        base.encoding.x = { field: 'cost', type: 'quantitative', aggregate: 'mean' };
        const result = recommendedMarkEncoding(transitionMark(base, 'scatter'));
        if (!result) throw new Error('Expected a scatter recommendation');

        expect(result.encoding.x?.aggregate).toBe('mean');
        expect(result.encoding.y?.aggregate).toBe('sum');
        expect(isMarkEncodingCompatible(result)).toBe(true);
    });

    it('returns no recommendation when the available fields cannot build the chart', () => {
        const chart = stackedBar();
        chart.encoding.color = null;
        const base = transitionMark(chart, 'heatmap');

        expect(recommendedMarkEncoding(base)).toBeNull();
    });

    it('does not recommend the same numeric field for both pie roles', () => {
        const chart = transitionMark(stackedBar(), 'pie');
        chart.encoding.x = { field: 'cost', type: 'quantitative' };
        chart.encoding.y = { field: 'revenue', type: 'quantitative' };
        chart.encoding.color = null;
        const result = recommendedMarkEncoding(chart);
        if (!result) throw new Error('Expected a pie recommendation');

        expect(result.encoding.x?.field).toBe('cost');
        expect(result.encoding.y?.field).toBe('revenue');
    });
});

describe('isMarkEncodingCompatible', () => {
    it('reports the unchanged fields as incompatible after a chart-type change', () => {
        expect(isMarkEncodingCompatible(transitionMark(stackedBar(), 'histogram'))).toBe(false);
    });

    it('accepts the existing compatible layout', () => {
        expect(isMarkEncodingCompatible(stackedBar())).toBe(true);
    });

    it('includes unsupported optional fields in the compatibility result', () => {
        const chart = stackedBar();
        chart.encoding.size = { field: 'profit', type: 'quantitative' };

        expect(isMarkEncodingCompatible(chart)).toBe(false);
        const recommendation = recommendedMarkEncoding(chart);
        if (!recommendation) throw new Error('Expected a bar recommendation');
        expect(isMarkEncodingCompatible(recommendation)).toBe(true);
    });

    it('allows a histogram count to remain implicit but rejects a stale y field', () => {
        const chart = transitionMark(stackedBar(), 'histogram');
        chart.encoding.x = {
            field: 'revenue',
            type: 'quantitative',
            bin: { maxbins: 10 },
        };
        chart.encoding.y = null;
        expect(isMarkEncodingCompatible(chart)).toBe(true);

        chart.encoding.y = { field: 'cost', type: 'quantitative', aggregate: 'sum' };
        expect(isMarkEncodingCompatible(chart)).toBe(false);
    });

    it('accepts raw or aggregated scatter axes but rejects a mixed mode', () => {
        const chart = transitionMark(stackedBar(), 'scatter');
        chart.encoding.color = { field: 'segment', type: 'nominal' };
        expect(isMarkEncodingCompatible(chart)).toBe(false);

        chart.encoding.x = { field: 'cost', type: 'quantitative' };
        chart.encoding.y = { field: 'revenue', type: 'quantitative' };
        expect(isMarkEncodingCompatible(chart)).toBe(true);

        chart.encoding.x.aggregate = 'sum';
        expect(isMarkEncodingCompatible(chart)).toBe(false);

        chart.encoding.y.aggregate = 'mean';
        expect(isMarkEncodingCompatible(chart)).toBe(true);
    });
});
