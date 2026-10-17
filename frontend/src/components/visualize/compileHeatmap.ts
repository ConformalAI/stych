import { continuousPaletteOf, labelColorOf, labelFontSizeOf, singleColorOf } from './chartColors';
import { textureDecalOf } from './chartTextures';
import {
    AXIS_NAME_BAND,
    MAX_LABEL_W,
    axisCommon,
    axisTitle,
    baseOption,
    chromeOf,
    dimLabel,
    escapeHtml,
    explicitTooltipHtml,
    formatMeasure,
    formatTooltipMeasure,
    gridOf,
    nameConfig,
    textWidth,
    tooltipDimLabel,
    type ChartTheme,
    type CompileOptions,
    type RowDatum,
} from './compileShared';
import type { VizData, VizSpec } from './types';

export function compileHeatmap(
    spec: VizSpec,
    data: VizData,
    theme: ChartTheme,
    options?: CompileOptions
) {
    const find = (name: string) => data.columns.findIndex((column) => column.name === name);
    const xColumn = find('x');
    const yColumn = find('y');
    const colorColumn = find('color');
    const xCategories: string[] = [];
    const yCategories: string[] = [];
    const xIndexes = new Map<string, number>();
    const yIndexes = new Map<string, number>();
    const cells: RowDatum[] = [];
    let minimum = Infinity;
    let maximum = -Infinity;

    data.rows.forEach((row, rowIndex) => {
        const xLabel = dimLabel(spec, 'x', row[xColumn], data);
        const yLabel = dimLabel(spec, 'y', row[yColumn], data);
        if (!xIndexes.has(xLabel)) {
            xIndexes.set(xLabel, xCategories.length);
            xCategories.push(xLabel);
        }
        if (!yIndexes.has(yLabel)) {
            yIndexes.set(yLabel, yCategories.length);
            yCategories.push(yLabel);
        }
        const value = row[colorColumn] as number | null;
        if (typeof value === 'number') {
            minimum = Math.min(minimum, value);
            maximum = Math.max(maximum, value);
        }
        const xIndex = xIndexes.get(xLabel);
        const yIndex = yIndexes.get(yLabel);
        if (xIndex !== undefined && yIndex !== undefined) {
            cells.push({ value: [xIndex, yIndex, value], __row: rowIndex });
        }
    });
    if (!Number.isFinite(minimum)) {
        minimum = 0;
        maximum = 1;
    }

    const chrome = chromeOf(spec, options, false);
    const base = baseOption(chrome, theme);
    const customPalette =
        spec.config?.color?.mode === 'single'
            ? [singleColorOf(spec)]
            : spec.config?.color?.mode === 'continuous'
              ? continuousPaletteOf(spec)
              : spec.config?.palette;
    const heatmapPalette =
        customPalette && customPalette.length > 0
            ? customPalette.length === 1
                ? [customPalette[0], customPalette[0]]
                : customPalette
            : theme.isDark
              ? ['#123a2e', '#0fb981', '#1fd68f']
              : ['#e6f7f1', '#34d39e', '#0b8a61'];
    const xTitle = axisTitle(spec, 'x');
    const yTitle = axisTitle(spec, 'y');
    const xLabelWidth = Math.min(MAX_LABEL_W, Math.max(0, ...xCategories.map(textWidth)));
    const yLabelWidth = Math.min(MAX_LABEL_W, Math.max(0, ...yCategories.map(textWidth)));
    const rotateX = xCategories.length > 10;
    const xBand = rotateX ? Math.min(96, xLabelWidth * 0.66) + 10 : 20;

    return {
        ...base,
        tooltip: {
            ...base.tooltip,
            trigger: 'item',
            formatter: (param: { data?: RowDatum }) => {
                const value = Array.isArray(param.data?.value) ? param.data.value : [];
                const rowIndex = param.data?.__row;
                const row = rowIndex != null ? data.rows[rowIndex] : null;
                const xLabel = row
                    ? tooltipDimLabel(spec, 'x', row[xColumn], data)
                    : (xCategories[Number(value[0])] ?? '—');
                const yLabel = row
                    ? tooltipDimLabel(spec, 'y', row[yColumn], data)
                    : (yCategories[Number(value[1])] ?? '—');
                return `${escapeHtml(xLabel)} × ${escapeHtml(
                    yLabel
                )}<br/>${escapeHtml(axisTitle(spec, 'color') ?? 'Value')}: ${escapeHtml(
                    typeof value[2] === 'number'
                        ? formatTooltipMeasure(spec, 'color', value[2])
                        : (value[2] ?? '—')
                )}${explicitTooltipHtml(spec, data, rowIndex ?? null)}`;
            },
        },
        grid: gridOf(chrome, {
            left: yLabelWidth + 12 + (yTitle ? AXIS_NAME_BAND : 0),
            right: 80,
            bottom: xBand + (xTitle ? AXIS_NAME_BAND : 0),
        }),
        xAxis: {
            type: 'category',
            data: xCategories,
            ...nameConfig(xTitle, false, xBand + 10, theme),
            ...axisCommon(theme, false),
            axisLabel: {
                color: theme.text,
                fontSize: 11,
                rotate: rotateX ? 40 : 0,
                width: MAX_LABEL_W,
                overflow: 'truncate',
            },
        },
        yAxis: {
            type: 'category',
            data: yCategories,
            ...nameConfig(yTitle, true, yLabelWidth + 14, theme),
            ...axisCommon(theme, false),
            axisLabel: {
                color: theme.text,
                fontSize: 11,
                width: MAX_LABEL_W,
                overflow: 'truncate',
            },
        },
        visualMap: {
            min: minimum,
            max: maximum,
            calculable: true,
            orient: 'vertical',
            right: 8,
            top: 'center',
            textStyle: { color: theme.text, fontSize: 10 },
            inRange: { color: heatmapPalette },
        },
        series: [
            {
                type: 'heatmap',
                data: cells,
                label: spec.config?.labels
                    ? {
                          show: true,
                          ...(labelColorOf(spec) ? { color: labelColorOf(spec) } : {}),
                          fontSize: labelFontSizeOf(spec, 12),
                          formatter: (param: { value?: unknown[] }) =>
                              typeof param.value?.[2] === 'number'
                                  ? formatMeasure(spec, 'color', param.value[2])
                                  : '',
                      }
                    : { show: false },
                itemStyle: {
                    decal: textureDecalOf(spec, theme),
                    borderColor: theme.isDark ? '#0b1524' : '#ffffff',
                    borderWidth: 1,
                },
            },
        ],
    };
}
