import { describe, expect, it } from 'vitest';
import {
    compileSpec,
    formatNumber,
    formatTemporal,
    formatTooltipNumber,
    themeFromMode,
} from './compileSpec';
import type { VizData, VizSpec } from './types';

const theme = themeFromMode(true);

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

const DIM_X = { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' } as const;
const MEAS_Y = { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' } as const;

describe('formatters', () => {
    it('formats numbers compactly', () => {
        expect(formatNumber(1500)).toBe('1.5k');
        expect(formatNumber(2_500_000)).toBe('2.5M');
        expect(formatNumber(42)).toBe('42');
        expect(formatNumber(3.14159)).toBe('3.14');
    });

    it('shows complete grouped values in hover details', () => {
        expect(formatTooltipNumber(3443)).toBe('3,443');
        expect(formatTooltipNumber(1234.5678)).toBe('1,234.5678');
    });

    it('formats temporal period starts by unit', () => {
        expect(formatTemporal('2024-03-01T00:00:00', 'month')).toBe('Mar 2024');
        expect(formatTemporal('2024-04-01T00:00:00', 'quarter')).toBe('Q2 2024');
        expect(formatTemporal('2024-01-01T00:00:00', 'year')).toBe('2024');
        expect(formatTemporal('2024-01-05T00:00:00', 'day')).toBe('2024-01-05');
    });
});

describe('compileSpec cartesian', () => {
    it('renders a horizontal bar (dimension on Y, measure on X)', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar' },
                encoding: {
                    x: { field: 'count', type: 'quantitative', aggregate: 'count' },
                    y: { field: 'location', type: 'nominal' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'count', kind: 'measure', type: 'quantitative' },
                    { name: 'y', field: 'location', kind: 'dimension', type: 'nominal' },
                ],
                [
                    [5, 'NYC'],
                    [3, 'LA'],
                    [1, 'SF'],
                ]
            ),
            theme
        );
        // Categories (locations) belong on the Y axis; counts on the value X axis.
        expect(opt.yAxis.type).toBe('category');
        expect(opt.yAxis.data).toEqual(['NYC', 'LA', 'SF']);
        expect(opt.xAxis.type).toBe('value');
        // Series carries the numeric counts, not the location strings.
        expect(opt.series[0].data.map((d: any) => d.value)).toEqual([5, 3, 1]);
    });

    it('builds a single-series bar option', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar' },
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                },
            }),
            data(
                [DIM_X, MEAS_Y],
                [
                    ['EU', 100],
                    ['NA', 250],
                ]
            ),
            theme
        );
        expect(opt.xAxis.type).toBe('category');
        expect(opt.xAxis.data).toEqual(['EU', 'NA']);
        expect(opt.series).toHaveLength(1);
        expect(opt.series[0].type).toBe('bar');
        // datums carry their tidy-row index for the selection layer
        expect(opt.series[0].data).toEqual([
            { value: 100, __row: 0 },
            { value: 250, __row: 1 },
        ]);
        expect(opt.series[0].name).toBe('SUM(revenue)');
    });

    it('keeps axes compact while showing the full value in the tooltip', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar' },
                encoding: {
                    x: { field: 'item', type: 'nominal' },
                    y: { field: 'quantity', type: 'quantitative', aggregate: 'sum' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'item', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'quantity', kind: 'measure', type: 'quantitative' },
                ],
                [['Juice', 3443]]
            ),
            theme
        );

        expect(opt.yAxis.axisLabel.formatter(3443)).toBe('3.4k');
        const tooltip = opt.tooltip.formatter([
            {
                axisValueLabel: 'Juice',
                dataIndex: 0,
                seriesName: 'SUM(quantity)',
                data: { value: 3443, __row: 0 },
            },
        ]);
        expect(tooltip).toContain('3,443');
        expect(tooltip).not.toContain('3.4k');
    });

    it('pivots a color dimension into multiple aligned series', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'line' },
                encoding: {
                    x: { field: 'month', type: 'temporal', timeUnit: 'month' },
                    y: { field: 'qty', type: 'quantitative', aggregate: 'sum' },
                    color: { field: 'region', type: 'nominal' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'month', kind: 'dimension', type: 'temporal' },
                    { name: 'color', field: 'region', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'qty', kind: 'measure', type: 'quantitative' },
                ],
                [
                    ['2024-01-01T00:00:00', 'EU', 1],
                    ['2024-01-01T00:00:00', 'NA', 2],
                    ['2024-02-01T00:00:00', 'EU', 3],
                ]
            ),
            theme
        );
        expect(opt.xAxis.data).toEqual(['Jan 2024', 'Feb 2024']);
        expect(opt.series).toHaveLength(2);
        const eu = opt.series.find((s: any) => s.name === 'EU');
        const na = opt.series.find((s: any) => s.name === 'NA');
        expect(eu.data).toEqual([
            { value: 1, __row: 0 },
            { value: 3, __row: 2 },
        ]);
        // gap filled with a null datum, not misaligned
        expect(na.data).toEqual([
            { value: 2, __row: 1 },
            { value: null, __row: null },
        ]);
        expect(eu.type).toBe('line');
    });

    it('stacks bar series when mark.stacked', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar', stacked: true },
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                    color: { field: 'item', type: 'nominal' },
                },
            }),
            data(
                [
                    DIM_X,
                    { name: 'color', field: 'item', kind: 'dimension', type: 'nominal' },
                    MEAS_Y,
                ],
                [
                    ['EU', 'a', 10],
                    ['EU', 'b', 20],
                ]
            ),
            theme
        );
        expect(opt.series.every((s: any) => s.stack === 'total')).toBe(true);
    });

    it('labels histogram bins with start–end ranges', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'histogram' },
                encoding: {
                    x: { field: 'revenue', type: 'quantitative', bin: { maxbins: 5 } },
                    y: { field: null, type: 'quantitative', aggregate: 'count' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: null, kind: 'measure', type: 'quantitative' },
                ],
                [
                    [0, 3],
                    [50, 4],
                ],
                { bins: { x: { width: 50 } } }
            ),
            theme
        );
        expect(opt.xAxis.data).toEqual(['0–50', '50–100']);
    });
});

describe('compileSpec chrome (title / legend / axis captions)', () => {
    const barSpec = (partial: Partial<VizSpec> = {}) =>
        spec({
            mark: { type: 'bar' },
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            },
            ...partial,
        });
    const barData = data(
        [DIM_X, MEAS_Y],
        [
            ['EU', 100],
            ['NA', 250],
        ]
    );

    it('renders the spec title, not a one-entry legend', () => {
        const opt = compileSpec(barSpec({ title: 'Revenue by region' }), barData, theme);
        expect(opt.title.text).toBe('Revenue by region');
        expect(opt.title.left).toBe('center');
        // A single series' name would otherwise masquerade as the chart title.
        expect(opt.legend.show).toBe(false);
    });

    it('falls back to a derived headline when untitled', () => {
        expect(compileSpec(barSpec(), barData, theme).title.text).toBe('SUM(revenue) by region');
    });

    it('omits the title when the host renders its own header', () => {
        const opt = compileSpec(barSpec({ title: 'Revenue' }), barData, theme, {
            showTitle: false,
        });
        expect(opt.title).toBeUndefined();
    });

    it('shows a bottom legend once a colour dimension splits the series', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar' },
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                    color: { field: 'item', type: 'nominal' },
                },
            }),
            data(
                [
                    DIM_X,
                    { name: 'color', field: 'item', kind: 'dimension', type: 'nominal' },
                    MEAS_Y,
                ],
                [
                    ['EU', 'a', 10],
                    ['EU', 'b', 20],
                ]
            ),
            theme
        );
        expect(opt.legend.show).not.toBe(false);
        expect(opt.legend.bottom).toBe(0);
    });

    it('captions the value axis vertically along the Y side', () => {
        const opt = compileSpec(barSpec(), barData, theme);
        expect(opt.yAxis.name).toBe('SUM(revenue)');
        expect(opt.yAxis.nameLocation).toBe('middle');
        expect(opt.yAxis.nameRotate).toBe(90);
        expect(opt.xAxis.name).toBe('region');
        expect(opt.xAxis.nameRotate).toBe(0);
        // Margins must clear the caption band the names occupy.
        expect(opt.grid.left).toBeGreaterThan(opt.yAxis.nameGap);
    });

    it('flips the vertical caption to the category axis on horizontal bars', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'bar' },
                encoding: {
                    x: { field: null, type: 'quantitative', aggregate: 'count' },
                    y: { field: 'location', type: 'nominal' },
                },
            }),
            data(
                [
                    { name: 'x', field: null, kind: 'measure', type: 'quantitative' },
                    { name: 'y', field: 'location', kind: 'dimension', type: 'nominal' },
                ],
                [
                    [5, 'NYC'],
                    [3, 'LA'],
                ]
            ),
            theme
        );
        expect(opt.yAxis.name).toBe('location');
        expect(opt.yAxis.nameRotate).toBe(90);
        expect(opt.xAxis.name).toBe('COUNT');
        expect(opt.xAxis.nameRotate).toBe(0);
    });
});

describe('compileSpec pie / scatter / heatmap', () => {
    it('builds pie data from dimension + measure', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'pie' },
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                },
            }),
            data(
                [DIM_X, MEAS_Y],
                [
                    ['EU', 100],
                    ['NA', 250],
                ]
            ),
            theme
        );
        expect(opt.series[0].type).toBe('pie');
        expect(opt.series[0].data).toEqual([
            expect.objectContaining({ name: 'EU', value: 100 }),
            expect.objectContaining({ name: 'NA', value: 250 }),
        ]);
    });

    it('groups scatter points by color dimension', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'scatter' },
                encoding: {
                    x: { field: 'qty', type: 'quantitative' },
                    y: { field: 'revenue', type: 'quantitative' },
                    color: { field: 'region', type: 'nominal' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'qty', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'color', field: 'region', kind: 'dimension', type: 'nominal' },
                ],
                [
                    [1, 100, 'EU'],
                    [2, 250, 'NA'],
                    [3, 80, 'EU'],
                ]
            ),
            theme
        );
        expect(opt.series).toHaveLength(2);
        expect(opt.series[0].data).toHaveLength(2);
        expect(opt.xAxis.type).toBe('value');
    });

    it('labels missing scatter colors without presenting them as the string null', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'scatter' },
                encoding: {
                    x: { field: 'qty', type: 'quantitative' },
                    y: { field: 'revenue', type: 'quantitative' },
                    color: { field: 'region', type: 'nominal' },
                },
            }),
            data(
                [
                    { name: 'x', field: 'qty', kind: 'dimension', type: 'quantitative' },
                    { name: 'y', field: 'revenue', kind: 'dimension', type: 'quantitative' },
                    { name: 'color', field: 'region', kind: 'dimension', type: 'nominal' },
                ],
                [[1, 100, null]]
            ),
            theme
        );
        expect(opt.series[0].name).toBe('(Missing)');
    });

    it('builds heatmap cells + visualMap range', () => {
        const opt = compileSpec(
            spec({
                mark: { type: 'heatmap' },
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'item', type: 'nominal' },
                    color: { field: null, type: 'quantitative', aggregate: 'count' },
                },
            }),
            data(
                [
                    DIM_X,
                    { name: 'y', field: 'item', kind: 'dimension', type: 'nominal' },
                    { name: 'color', field: null, kind: 'measure', type: 'quantitative' },
                ],
                [
                    ['EU', 'a', 3],
                    ['NA', 'b', 7],
                ]
            ),
            theme
        );
        expect(opt.series[0].type).toBe('heatmap');
        expect(opt.visualMap.min).toBe(3);
        expect(opt.visualMap.max).toBe(7);
        expect(opt.series[0].data).toEqual([
            { value: [0, 0, 3], __row: 0 },
            { value: [1, 1, 7], __row: 1 },
        ]);
    });
});
