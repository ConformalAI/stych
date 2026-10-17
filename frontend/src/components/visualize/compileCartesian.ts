import type { ColorValue, VizData, VizSpec } from './types';
import {
    AXIS_NAME_BAND,
    MAX_LABEL_W,
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
    textWidth,
    tooltipDimLabel,
    valueLabelWidth,
    type ChartTheme,
    type CompileOptions,
    type RowDatum,
} from './compileShared';
import { textureDecalOf, textureLineTypeOf, textureSymbolOf } from './chartTextures';
import {
    categoricalColorContext,
    colorForValue,
    colorValueKey,
    effectiveColorMode,
    labelColorOf,
    labelFontSizeOf,
    singleColorOf,
} from './chartColors';

function pivotByColor(
    data: VizData,
    categoryColumn: number,
    colorColumn: number,
    valueColumn: number
) {
    const categories: unknown[] = [];
    const categoryIndexes = new Map<string, number>();
    const seriesMap = new Map<
        string,
        { name: string; colorValue: ColorValue; values: RowDatum[] }
    >();

    for (const row of data.rows) {
        const key = colorValueKey(row[categoryColumn]);
        if (!categoryIndexes.has(key)) {
            categoryIndexes.set(key, categories.length);
            categories.push(row[categoryColumn]);
        }
    }
    for (const row of data.rows) {
        const rawValue = row[colorColumn] as ColorValue;
        const key = colorValueKey(rawValue);
        if (!seriesMap.has(key)) {
            seriesMap.set(key, {
                name: rawValue == null ? '(Missing)' : String(rawValue),
                colorValue: rawValue,
                values: Array.from({ length: categories.length }, () => ({
                    value: null,
                    __row: null,
                })),
            });
        }
    }
    data.rows.forEach((row, rowIndex) => {
        const categoryIndex = categoryIndexes.get(colorValueKey(row[categoryColumn]));
        const colorSeries = seriesMap.get(colorValueKey(row[colorColumn]));
        if (categoryIndex === undefined || !colorSeries) return;
        colorSeries.values[categoryIndex] = {
            value: row[valueColumn],
            __row: rowIndex,
        };
    });
    return { categories, seriesMap };
}

export function compileCartesian(
    spec: VizSpec,
    data: VizData,
    theme: ChartTheme,
    options?: CompileOptions
) {
    const columns = columnIndexes(data);
    const mark = spec.mark.type;
    const colorIsDimension = columns.color >= 0 && data.columns[columns.color].kind === 'dimension';
    const yIsDimension = columns.y >= 0 && data.columns[columns.y]?.kind === 'dimension';
    const xIsMeasure = columns.x >= 0 && data.columns[columns.x]?.kind === 'measure';
    const horizontal = mark === 'bar' && yIsDimension && xIsMeasure;
    const categoryChannel: 'x' | 'y' = horizontal ? 'y' : 'x';
    const valueChannel: 'x' | 'y' = horizontal ? 'x' : 'y';
    const categoryColumn = horizontal ? columns.y : columns.x;
    const valueColumn = horizontal ? columns.x : columns.y;

    let categories: unknown[];
    let series: Array<{ name: string; colorValue: ColorValue | null; values: RowDatum[] }>;
    if (colorIsDimension) {
        const pivoted = pivotByColor(data, categoryColumn, columns.color, valueColumn);
        categories = pivoted.categories;
        series = Array.from(pivoted.seriesMap.values());
    } else {
        categories = data.rows.map((row) => row[categoryColumn]);
        series = [
            {
                name: axisTitle(spec, valueChannel) ?? 'Value',
                colorValue: null,
                values: data.rows.map((row, rowIndex) => ({
                    value: row[valueColumn],
                    __row: rowIndex,
                })),
            },
        ];
    }

    const categoryLabels = categories.map((category) =>
        dimLabel(spec, categoryChannel, category, data)
    );
    const echartsType = mark === 'line' || mark === 'area' ? 'line' : 'bar';
    const colorContext = categoricalColorContext(spec, data);
    const colorMode = effectiveColorMode(spec, data);
    const stacked = Boolean(spec.mark.stacked) && (mark === 'bar' || mark === 'area');
    const chrome = chromeOf(spec, options, series.length > 1);
    const base = baseOption(chrome, theme);
    const rotate = !horizontal && categoryLabels.length > 10;
    const categoryLabelWidth = Math.min(MAX_LABEL_W, Math.max(0, ...categoryLabels.map(textWidth)));
    const valueWidth = valueLabelWidth(
        series.flatMap((item) => item.values.map((datum) => datum.value as number | null))
    );
    const categoryBand = horizontal
        ? 0
        : rotate
          ? Math.min(96, categoryLabelWidth * 0.66) + 10
          : 20;
    const categoryName = axisTitle(spec, categoryChannel);
    const valueName = axisTitle(spec, valueChannel);

    const categoryAxis = {
        type: 'category' as const,
        data: categoryLabels,
        ...nameConfig(
            categoryName,
            horizontal,
            horizontal ? categoryLabelWidth + 14 : categoryBand + 10,
            theme
        ),
        ...axisCommon(theme, false),
        axisLabel: {
            color: theme.text,
            fontSize: 11,
            rotate: rotate ? 40 : 0,
            interval: categoryLabels.length > 30 ? 'auto' : 0,
            width: MAX_LABEL_W,
            overflow: 'truncate',
        },
    };
    const valueAxis = {
        type: 'value' as const,
        ...nameConfig(valueName, !horizontal, horizontal ? 28 : valueWidth + 16, theme),
        ...axisCommon(theme, spec.config?.grid !== false),
        axisLabel: {
            color: theme.text,
            fontSize: 11,
            formatter: (value: number) => formatMeasure(spec, valueChannel, value),
        },
    };

    return {
        ...base,
        grid: gridOf(chrome, {
            left: horizontal
                ? categoryLabelWidth + 12 + (categoryName ? AXIS_NAME_BAND : 0)
                : valueWidth + 10 + (valueName ? AXIS_NAME_BAND : 0),
            right: 24,
            bottom: horizontal
                ? 24 + (valueName ? AXIS_NAME_BAND : 0)
                : categoryBand + (categoryName ? AXIS_NAME_BAND : 0),
        }),
        tooltip: {
            ...base.tooltip,
            trigger: 'axis',
            axisPointer: { type: echartsType === 'bar' ? 'shadow' : 'line' },
            formatter: (rawParams: unknown) => {
                const params = (Array.isArray(rawParams) ? rawParams : [rawParams]) as Array<{
                    axisValueLabel?: string;
                    dataIndex?: number;
                    marker?: string;
                    seriesName?: string;
                    data?: RowDatum;
                }>;
                const first = params.find((param) => typeof param.data?.__row === 'number');
                const categoryIndex = params[0]?.dataIndex;
                const category =
                    typeof categoryIndex === 'number' ? categories[categoryIndex] : undefined;
                const title =
                    category !== undefined
                        ? `<strong>${escapeHtml(
                              tooltipDimLabel(spec, categoryChannel, category, data)
                          )}</strong>`
                        : params[0]?.axisValueLabel
                          ? `<strong>${escapeHtml(params[0].axisValueLabel)}</strong>`
                          : '';
                const values = params
                    .filter((param) => param.data?.value != null)
                    .map(
                        (param) =>
                            `<br/>${param.marker ?? ''}${escapeHtml(param.seriesName ?? 'Value')}: ${escapeHtml(
                                typeof param.data?.value === 'number'
                                    ? formatTooltipMeasure(spec, valueChannel, param.data.value)
                                    : (param.data?.value ?? '—')
                            )}`
                    )
                    .join('');
                return `${title}${values}${explicitTooltipHtml(spec, data, first?.data?.__row ?? null)}`;
            },
        },
        xAxis: horizontal ? valueAxis : categoryAxis,
        yAxis: horizontal ? categoryAxis : valueAxis,
        series: series.map((item, index) => {
            const seriesColor =
                colorContext?.binding.channel === 'color'
                    ? colorForValue(spec, colorContext.binding, item.colorValue, index)
                    : singleColorOf(spec);
            const values =
                colorMode === 'categorical' &&
                colorContext &&
                colorContext.binding.channel === categoryChannel
                    ? item.values.map((datum, datumIndex) => ({
                          ...datum,
                          itemStyle: {
                              color: colorForValue(
                                  spec,
                                  colorContext.binding,
                                  categories[datumIndex],
                                  datumIndex
                              ),
                          },
                      }))
                    : item.values;
            return {
                name: item.name,
                type: echartsType,
                data: values,
                stack: stacked ? 'total' : undefined,
                smooth: Boolean(spec.mark.smooth),
                showSymbol: spec.mark.point !== false,
                symbol: textureSymbolOf(spec),
                symbolSize: 6,
                lineStyle:
                    echartsType === 'line'
                        ? { color: seriesColor, type: textureLineTypeOf(spec) }
                        : undefined,
                areaStyle:
                    mark === 'area'
                        ? { color: seriesColor, opacity: theme.isDark ? 0.18 : 0.14 }
                        : undefined,
                itemStyle: {
                    color: seriesColor,
                    decal: textureDecalOf(spec, theme),
                    borderRadius:
                        echartsType === 'bar' && !stacked
                            ? horizontal
                                ? [0, 3, 3, 0]
                                : [3, 3, 0, 0]
                            : 0,
                },
                barMaxWidth: mark === 'histogram' ? undefined : 48,
                barCategoryGap: mark === 'histogram' ? '0%' : undefined,
                label:
                    spec.config?.labels && series.length === 1
                        ? {
                              show: true,
                              position: horizontal ? 'right' : 'top',
                              color: labelColorOf(spec, theme.text),
                              fontSize: labelFontSizeOf(spec, 10),
                              formatter: (param: { value?: unknown }) =>
                                  typeof param.value === 'number'
                                      ? formatMeasure(spec, valueChannel, param.value)
                                      : '',
                          }
                        : { show: false },
            };
        }),
    };
}
