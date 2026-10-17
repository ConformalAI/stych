import { describe, expect, it } from 'vitest';
import { rawScatterOverlapSummary } from './scatterOverlap';
import type { VizData, VizSpec } from './types';

const data: VizData = {
    columns: [
        { name: 'x', field: 'quantity', kind: 'dimension', type: 'quantitative' },
        { name: 'y', field: 'total_spent', kind: 'dimension', type: 'quantitative' },
    ],
    rows: [
        [1, 5],
        [1, 5],
        [2, 10],
    ],
    meta: { row_count: 3, truncated: false, engine: 'sql' },
};

function scatter(aggregate: boolean): VizSpec {
    return {
        mark: { type: 'scatter' },
        encoding: {
            x: {
                field: 'quantity',
                type: 'quantitative',
                aggregate: aggregate ? 'sum' : null,
            },
            y: {
                field: 'total_spent',
                type: 'quantitative',
                aggregate: aggregate ? 'sum' : null,
            },
        },
    };
}

describe('rawScatterOverlapSummary', () => {
    it('warns when raw rows share coordinates', () => {
        expect(rawScatterOverlapSummary(scatter(false), data)).toContain(
            '3 plotted rows occupy 2 positions'
        );
    });

    it('does not warn for aggregated scatter charts', () => {
        expect(rawScatterOverlapSummary(scatter(true), data)).toBeNull();
    });
});
