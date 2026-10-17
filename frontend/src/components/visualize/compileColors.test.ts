import { describe, expect, it } from 'vitest';
import { compileSpec, themeFromMode } from './compileSpec';
import type { MarkType, VizData, VizSpec } from './types';

const SOLID = '#2563EB';
const theme = themeFromMode(false);
type ColoredSeries = {
    itemStyle: { color: string };
    lineStyle?: { color: string };
    areaStyle?: { color: string };
};

function spec(mark: MarkType, encoding: VizSpec['encoding'], palette = [SOLID]): VizSpec {
    return {
        schema_version: 1,
        data: { source: 'active' },
        transform: [],
        mark: { type: mark },
        encoding,
        config: { palette },
    };
}

function data(columns: VizData['columns'], rows: VizData['rows']): VizData {
    return {
        columns,
        rows,
        meta: { row_count: rows.length, truncated: false, engine: 'pandas' },
    };
}

const CARTESIAN_DATA = data(
    [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
        { name: 'color', field: 'segment', kind: 'dimension', type: 'nominal' },
    ],
    [
        ['East', 10, 'Consumer'],
        ['East', 20, 'Corporate'],
    ]
);
const CARTESIAN_ENCODING: VizSpec['encoding'] = {
    x: { field: 'region', type: 'nominal' },
    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
    color: { field: 'segment', type: 'nominal' },
};

describe('custom chart colors', () => {
    it('assigns stable colors to individual bars without a color encoding', () => {
        const barSpec = spec('bar', {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
        });
        barSpec.config = {
            color: {
                mode: 'categorical',
                binding: { channel: 'x', field: 'region' },
                palette: ['#2563EB', '#0FB981'],
                assignments: [{ value: 'West', color: '#DC2626' }],
            },
        };
        const option = compileSpec(
            barSpec,
            data(
                [
                    { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
                ],
                [
                    ['West', 20],
                    ['East', 10],
                ]
            ),
            theme
        );

        expect(option.series[0].data[0].itemStyle.color).toBe('#DC2626');
        expect(option.series[0].data[1].itemStyle.color).toBe('#0FB981');
    });

    it.each(['bar', 'line', 'area'] as const)(
        'assigns %s colors by encoded series value instead of series order',
        (mark) => {
            const chart = spec(mark, CARTESIAN_ENCODING, ['#2563EB', '#0FB981']);
            chart.config = {
                color: {
                    mode: 'categorical',
                    binding: { channel: 'color', field: 'segment' },
                    palette: ['#2563EB', '#0FB981'],
                    assignments: [{ value: 'Corporate', color: '#DC2626' }],
                },
            };

            const option = compileSpec(chart, CARTESIAN_DATA, theme);
            expect(
                option.series.find((series: { name?: string }) => series.name === 'Corporate')
                    .itemStyle.color
            ).toBe('#DC2626');
        }
    );

    it.each(['bar', 'line', 'area'] as const)('uses one solid color for %s charts', (mark) => {
        const option = compileSpec(spec(mark, CARTESIAN_ENCODING), CARTESIAN_DATA, theme);
        const series = option.series as ColoredSeries[];

        expect(series.every((item) => item.itemStyle.color === SOLID)).toBe(true);
        if (mark === 'line' || mark === 'area') {
            expect(series.every((item) => item.lineStyle?.color === SOLID)).toBe(true);
        }
        if (mark === 'area') {
            expect(series.every((item) => item.areaStyle?.color === SOLID)).toBe(true);
        }
    });

    it('uses one solid color for histograms', () => {
        const option = compileSpec(
            spec('histogram', {
                x: { field: 'revenue', type: 'quantitative', bin: { maxbins: 10 } },
                y: { field: null, type: 'quantitative', aggregate: 'count' },
            }),
            data(
                [
                    { name: 'x', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: null, kind: 'measure', type: 'quantitative' },
                ],
                [
                    [0, 2],
                    [10, 4],
                ]
            ),
            theme
        );

        expect(option.series[0].itemStyle.color).toBe(SOLID);
    });

    it('uses one solid color for every pie slice', () => {
        const option = compileSpec(
            spec('pie', {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            }),
            data(
                [
                    { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
                ],
                [
                    ['East', 10],
                    ['West', 20],
                ]
            ),
            theme
        );

        const slices = option.series[0].data as Array<{ itemStyle: { color: string } }>;
        expect(slices.every((slice) => slice.itemStyle.color === SOLID)).toBe(true);
    });

    it('assigns pie slice colors by category value', () => {
        const chart = spec('pie', {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
        });
        chart.config = {
            color: {
                mode: 'categorical',
                binding: { channel: 'x', field: 'region' },
                assignments: [{ value: 'West', color: '#DC2626' }],
            },
        };
        const option = compileSpec(
            chart,
            data(
                [
                    { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
                ],
                [
                    ['East', 10],
                    ['West', 20],
                ]
            ),
            theme
        );

        expect(option.series[0].data[1].itemStyle.color).toBe('#DC2626');
    });

    it('uses one solid color for every scatter group', () => {
        const option = compileSpec(
            spec('scatter', {
                x: { field: 'quantity', type: 'quantitative' },
                y: { field: 'revenue', type: 'quantitative' },
                color: { field: 'region', type: 'nominal' },
            }),
            data(
                [
                    { name: 'x', field: 'quantity', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'color', field: 'region', kind: 'dimension', type: 'nominal' },
                ],
                [
                    [1, 10, 'East'],
                    [2, 20, 'West'],
                ]
            ),
            theme
        );

        const series = option.series as ColoredSeries[];
        expect(series.every((item) => item.itemStyle.color === SOLID)).toBe(true);
    });

    it('assigns scatter group colors by encoded value', () => {
        const chart = spec('scatter', {
            x: { field: 'quantity', type: 'quantitative' },
            y: { field: 'revenue', type: 'quantitative' },
            color: { field: 'region', type: 'nominal' },
        });
        chart.config = {
            color: {
                mode: 'categorical',
                binding: { channel: 'color', field: 'region' },
                assignments: [{ value: 'West', color: '#DC2626' }],
            },
        };
        const option = compileSpec(
            chart,
            data(
                [
                    { name: 'x', field: 'quantity', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'color', field: 'region', kind: 'dimension', type: 'nominal' },
                ],
                [
                    [1, 10, 'East'],
                    [2, 20, 'West'],
                ]
            ),
            theme
        );

        expect(
            option.series.find((series: { name?: string }) => series.name === 'West').itemStyle
                .color
        ).toBe('#DC2626');
    });

    it('uses a flat color or an ordered gradient for heatmaps', () => {
        const encoding: VizSpec['encoding'] = {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'segment', type: 'nominal' },
            color: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
        };
        const heatmapData = data(
            [
                { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                { name: 'y', field: 'segment', kind: 'dimension', type: 'nominal' },
                { name: 'color', field: 'revenue', kind: 'measure', type: 'quantitative' },
            ],
            [
                ['East', 'Consumer', 10],
                ['West', 'Corporate', 20],
            ]
        );

        expect(
            compileSpec(spec('heatmap', encoding), heatmapData, theme).visualMap.inRange.color
        ).toEqual([SOLID, SOLID]);
        expect(
            compileSpec(
                spec('heatmap', encoding, ['#2563EB', '#FFFFFF', '#DC2626']),
                heatmapData,
                theme
            ).visualMap.inRange.color
        ).toEqual(['#2563EB', '#FFFFFF', '#DC2626']);

        const continuous = spec('heatmap', encoding);
        continuous.config = {
            color: {
                mode: 'continuous',
                binding: { channel: 'color', field: 'revenue' },
                palette: ['#FFFFFF', '#2563EB'],
            },
        };
        expect(compileSpec(continuous, heatmapData, theme).visualMap.inRange.color).toEqual([
            '#FFFFFF',
            '#2563EB',
        ]);

        continuous.config = { ...continuous.config, labels: true, labelFontSize: 18 };
        const automaticLabels = compileSpec(continuous, heatmapData, theme).series[0].label;
        expect(automaticLabels.fontSize).toBe(18);
        expect(automaticLabels).not.toHaveProperty('color');

        continuous.config = {
            ...continuous.config,
            labelColor: '#111827',
        };
        expect(compileSpec(continuous, heatmapData, theme).series[0].label.color).toBe('#111827');
    });

    it('keeps automatic texture decals off until the user enables one', () => {
        const plainSpec = spec('bar', CARTESIAN_ENCODING);
        const plain = compileSpec(plainSpec, CARTESIAN_DATA, theme);
        expect(plain.aria.decal.show).toBe(false);
        expect(plain.series[0].itemStyle.decal).toBeUndefined();

        const texturedSpec: VizSpec = {
            ...plainSpec,
            config: {
                ...plainSpec.config,
                texture: { enabled: true, pattern: 'diagonal' },
            },
        };
        const textured = compileSpec(texturedSpec, CARTESIAN_DATA, theme);
        expect(textured.series[0].itemStyle.decal).toMatchObject({
            rotation: -Math.PI / 4,
            dashArrayY: [4, 3],
        });
    });
});
