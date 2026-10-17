import type { ColorValue, VizData, VizSpec } from './types';
import {
    AXIS_NAME_BAND,
    axisCommon,
    axisTitle,
    baseOption,
    chromeOf,
    columnIndexes,
    dimLabel,
    escapeHtml,
    explicitTooltipHtml,
    formatMeasure,
    formatTooltipMeasure,
    gridOf,
    nameConfig,
    tooltipDimLabel,
    valueLabelWidth,
    type ChartTheme,
    type CompileOptions,
    type RowDatum,
} from './compileShared';
import { textureDecalOf, textureSymbolOf } from './chartTextures';
import {
    categoricalColorContext,
    colorForValue,
    colorValueKey,
    labelColorOf,
    labelFontSizeOf,
    singleColorOf,
} from './chartColors';

export function compilePie(
    spec: VizSpec,
    data: VizData,
    theme: ChartTheme,
    options?: CompileOptions
) {
    const columns = columnIndexes(data);
    const colorContext = categoricalColorContext(spec, data);
    const dimensionIndex =
        columns.x >= 0 && data.columns[columns.x].kind === 'dimension' ? columns.x : columns.color;
    const measureIndex =
        columns.y >= 0 && data.columns[columns.y].kind === 'measure'
            ? columns.y
            : data.columns.findIndex((column) => column.kind === 'measure');
    const dimensionChannel = dimensionIndex === columns.color ? 'color' : 'x';
    const chrome = chromeOf(spec, options, true);
    const base = baseOption(chrome, theme);

    return {
        ...base,
        tooltip: {
            ...base.tooltip,
            trigger: 'item',
            formatter: (param: {
                name?: string;
                percent?: number;
                data?: { value?: number; __row?: number };
            }) =>
                `<strong>${escapeHtml(axisTitle(spec, dimensionChannel) ?? 'Category')}</strong>: ${escapeHtml(
                    param.data?.__row != null
                        ? tooltipDimLabel(
                              spec,
                              dimensionChannel,
                              data.rows[param.data.__row]?.[dimensionIndex],
                              data
                          )
                        : (param.name ?? '—')
                )}<br/>${escapeHtml(axisTitle(spec, 'y') ?? 'Value')}: ${escapeHtml(
                    typeof param.data?.value === 'number'
                        ? formatTooltipMeasure(spec, 'y', param.data.value)
                        : (param.data?.value ?? '—')
                )}${typeof param.percent === 'number' ? ` (${param.percent.toFixed(1)}%)` : ''}${explicitTooltipHtml(
                    spec,
                    data,
                    param.data?.__row ?? null
                )}`,
        },
        series: [
            {
                type: 'pie',
                radius: ['32%', '64%'],
                center: ['50%', `${50 + (chrome.title ? 4 : 0) - (chrome.legend ? 2 : 0)}%`],
                padAngle: 1,
                itemStyle: { borderRadius: 4 },
                labelLayout: { hideOverlap: true },
                labelLine: { length: 8, length2: 10 },
                label: {
                    show: Boolean(spec.config?.labels),
                    color: labelColorOf(spec, theme.text),
                    fontSize: labelFontSizeOf(spec, 11),
                    formatter: '{b} {d}%',
                },
                data: data.rows.map((row, rowIndex) => ({
                    name: dimLabel(
                        spec,
                        dimensionIndex === columns.color ? 'color' : 'x',
                        row[dimensionIndex],
                        data
                    ),
                    value: row[measureIndex],
                    __row: rowIndex,
                    itemStyle: {
                        color: colorContext
                            ? colorForValue(
                                  spec,
                                  colorContext.binding,
                                  row[dimensionIndex],
                                  rowIndex
                              )
                            : singleColorOf(spec),
                        decal: textureDecalOf(spec, theme),
                    },
                })),
            },
        ],
    };
}

export function compileScatter(
    spec: VizSpec,
    data: VizData,
    theme: ChartTheme,
    options?: CompileOptions
) {
    const columns = columnIndexes(data);
    const colorIsDimension = columns.color >= 0 && data.columns[columns.color].kind === 'dimension';
    let sizeScale: ((value: number) => number) | null = null;
    if (columns.size >= 0) {
        const values = data.rows
            .map((row) => row[columns.size])
            .filter((value): value is number => typeof value === 'number');
        const minimum = Math.min(...values);
        const maximum = Math.max(...values);
        sizeScale = (value: number) =>
            maximum === minimum ? 12 : 6 + ((value - minimum) / (maximum - minimum)) * 22;
    }

    const groups = new Map<
        string,
        {
            name: string;
            colorValue: ColorValue | null;
            points: Array<[number, number, number | null, number]>;
        }
    >();
    data.rows.forEach((row, rowIndex) => {
        const colorValue = colorIsDimension ? (row[columns.color] as ColorValue) : null;
        const key = colorIsDimension ? colorValueKey(colorValue) : '__all__';
        if (!groups.has(key)) {
            groups.set(key, {
                name:
                    colorIsDimension && colorValue == null
                        ? '(Missing)'
                        : colorIsDimension
                          ? String(colorValue)
                          : '__all__',
                colorValue,
                points: [],
            });
        }
        const group = groups.get(key);
        if (!group) return;
        group.points.push([
            row[columns.x] as number,
            row[columns.y] as number,
            columns.size >= 0 ? (row[columns.size] as number | null) : null,
            rowIndex,
        ]);
    });

    const colorContext = categoricalColorContext(spec, data);
    const chrome = chromeOf(spec, options, colorIsDimension);
    const base = baseOption(chrome, theme);
    const xTitle = axisTitle(spec, 'x');
    const yTitle = axisTitle(spec, 'y');
    const yLabelWidth = valueLabelWidth(data.rows.map((row) => row[columns.y] as number | null));

    return {
        ...base,
        grid: gridOf(chrome, {
            left: yLabelWidth + 10 + (yTitle ? AXIS_NAME_BAND : 0),
            right: 24,
            bottom: 24 + (xTitle ? AXIS_NAME_BAND : 0),
        }),
        tooltip: {
            ...base.tooltip,
            trigger: 'item',
            formatter: (param: { seriesName?: string; data?: RowDatum }) => {
                const value = Array.isArray(param.data?.value) ? param.data.value : [];
                const rowIndex = param.data?.__row;
                const sizeValue =
                    columns.size >= 0 && rowIndex != null
                        ? data.rows[rowIndex]?.[columns.size]
                        : undefined;
                const sizeDetail =
                    typeof sizeValue === 'number'
                        ? `<br/>${escapeHtml(axisTitle(spec, 'size') ?? 'Size')}: ${escapeHtml(
                              formatTooltipMeasure(spec, 'size', sizeValue)
                          )}`
                        : '';
                return `${param.seriesName ? `<strong>${escapeHtml(param.seriesName)}</strong><br/>` : ''}${escapeHtml(
                    axisTitle(spec, 'x') ?? 'x'
                )}: ${escapeHtml(
                    typeof value[0] === 'number'
                        ? formatTooltipMeasure(spec, 'x', value[0])
                        : (value[0] ?? '—')
                )}<br/>${escapeHtml(axisTitle(spec, 'y') ?? 'y')}: ${escapeHtml(
                    typeof value[1] === 'number'
                        ? formatTooltipMeasure(spec, 'y', value[1])
                        : (value[1] ?? '—')
                )}${sizeDetail}${explicitTooltipHtml(spec, data, rowIndex ?? null)}`;
            },
        },
        xAxis: {
            type: 'value',
            ...nameConfig(xTitle, false, 28, theme),
            ...axisCommon(theme, spec.config?.grid !== false),
            axisLabel: {
                color: theme.text,
                fontSize: 11,
                formatter: (value: number) => formatMeasure(spec, 'x', value),
            },
        },
        yAxis: {
            type: 'value',
            ...nameConfig(yTitle, true, yLabelWidth + 16, theme),
            ...axisCommon(theme, spec.config?.grid !== false),
            axisLabel: {
                color: theme.text,
                fontSize: 11,
                formatter: (value: number) => formatMeasure(spec, 'y', value),
            },
        },
        series: Array.from(groups.values()).map(({ name, colorValue, points }, index) => ({
            name: name === '__all__' ? undefined : name,
            type: 'scatter',
            data: points.map(([x, y, size, rowIndex]) => ({
                value: [x, y],
                __row: rowIndex,
                symbolSize: sizeScale && typeof size === 'number' ? sizeScale(size) : 8,
            })),
            symbol: textureSymbolOf(spec),
            itemStyle: {
                color:
                    colorContext?.binding.channel === 'color'
                        ? colorForValue(spec, colorContext.binding, colorValue, index)
                        : singleColorOf(spec),
                decal: textureDecalOf(spec, theme),
                opacity: 0.75,
            },
        })),
    };
}
