/**
 * Pure compiler facade: (VizSpec, VizData, theme) → ECharts option object.
 * Chart-family implementations live in focused modules; callers depend only
 * on this stable entry point.
 */

import { compileCartesian } from './compileCartesian';
import { compileHeatmap } from './compileHeatmap';
import {
    PALETTE,
    formatNumber,
    formatNumberWithPattern,
    formatTemporal,
    formatTooltipNumber,
    themeFromMode,
    type ChartTheme,
    type CompileOptions,
    type RowDatum,
} from './compileShared';
import { compilePie, compileScatter } from './compileSpecial';
import type { VizData, VizSpec } from './types';

export {
    PALETTE,
    formatNumber,
    formatNumberWithPattern,
    formatTemporal,
    formatTooltipNumber,
    themeFromMode,
    type ChartTheme,
    type CompileOptions,
    type RowDatum,
};

export function compileSpec(
    spec: VizSpec,
    data: VizData,
    theme: ChartTheme,
    options?: CompileOptions
): Record<string, any> {
    const mark = spec.mark.type;
    if (mark === 'pie') return compilePie(spec, data, theme, options);
    if (mark === 'heatmap') return compileHeatmap(spec, data, theme, options);
    if (mark === 'scatter') return compileScatter(spec, data, theme, options);
    return compileCartesian(spec, data, theme, options);
}
