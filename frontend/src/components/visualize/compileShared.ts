import type { ChannelDef, TimeUnit, VizData, VizSpec } from './types';

export const PALETTE = [
    '#0fb981',
    '#1fd68f',
    '#06b6d4',
    '#3b82f6',
    '#f59e0b',
    '#e8a13a',
    '#ef4444',
    '#ec4899',
];

export interface ChartTheme {
    isDark: boolean;
    text: string;
    axisLine: string;
    grid: string;
    tooltipBg: string;
    tooltipBorder: string;
}

export interface CompileOptions {
    showTitle?: boolean;
}

export interface Cols {
    x: number;
    y: number;
    color: number;
    size: number;
}

export interface Chrome {
    title?: string;
    legend: boolean;
}

export interface RowDatum {
    value: unknown;
    __row: number | null;
    [key: string]: unknown;
}

export function themeFromMode(isDark: boolean): ChartTheme {
    return isDark
        ? {
              isDark,
              text: '#94a3b8',
              axisLine: 'rgba(255,255,255,0.15)',
              grid: 'rgba(255,255,255,0.07)',
              tooltipBg: '#0f2744',
              tooltipBorder: 'rgba(255,255,255,0.1)',
          }
        : {
              isDark,
              text: '#64748b',
              axisLine: '#cbd5e1',
              grid: '#e2e8f0',
              tooltipBg: '#ffffff',
              tooltipBorder: '#e2e8f0',
          };
}

export function formatNumber(value: number): string {
    if (!Number.isFinite(value)) return String(value);
    if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
    return value % 1 === 0 ? String(value) : value.toFixed(2);
}

/** Full-precision display for hover details; axes and labels stay compact. */
export function formatTooltipNumber(value: number): string {
    if (!Number.isFinite(value)) return String(value);
    return new Intl.NumberFormat('en-US', {
        useGrouping: true,
        maximumFractionDigits: 20,
    }).format(value);
}

export function formatNumberWithPattern(value: number, pattern?: string | null): string {
    if (!pattern) return formatNumber(value);
    const match = pattern.match(/^(.*?)(,)?(?:\.(\d+))?([f%])$/);
    if (!match) return formatNumber(value);
    const [, prefix, grouped, digitsRaw, kind] = match;
    const digits = Math.min(6, Number(digitsRaw ?? 0));
    const scaled = kind === '%' ? value * 100 : value;
    const rendered = new Intl.NumberFormat('en-US', {
        useGrouping: Boolean(grouped),
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    }).format(scaled);
    return `${prefix}${rendered}${kind === '%' ? '%' : ''}`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function formatTemporal(iso: string, unit?: TimeUnit | null): string {
    const [date, time] = String(iso).split('T');
    const [year, month, day] = (date || '').split('-');
    if (!year) return String(iso);
    const monthIndex = Math.max(0, parseInt(month || '1', 10) - 1);
    switch (unit) {
        case 'year':
            return year;
        case 'quarter':
            return `Q${Math.floor(monthIndex / 3) + 1} ${year}`;
        case 'month':
            return `${MONTHS[monthIndex]} ${year}`;
        case 'hour':
            return `${MONTHS[monthIndex]} ${day} ${(time || '').slice(0, 5)}`;
        case 'week':
        case 'day':
            return `${year}-${month}-${day}`;
        default:
            return time ? `${year}-${month}-${day} ${time.slice(0, 5)}` : `${year}-${month}-${day}`;
    }
}

export function columnIndexes(data: VizData): Cols {
    const find = (name: string) => data.columns.findIndex((column) => column.name === name);
    return { x: find('x'), y: find('y'), color: find('color'), size: find('size') };
}

export function paletteOf(spec: VizSpec): string[] {
    const custom = spec.config?.palette;
    return custom && custom.length > 0 ? custom : PALETTE;
}

export function formatMeasure(
    spec: VizSpec,
    channel: 'x' | 'y' | 'color' | 'size',
    value: number
): string {
    const pattern = spec.encoding[channel]?.format ?? spec.config?.numberFormat ?? null;
    return formatNumberWithPattern(value, pattern);
}

export function formatTooltipMeasure(
    spec: VizSpec,
    channel: 'x' | 'y' | 'color' | 'size',
    value: number
): string {
    const pattern = spec.encoding[channel]?.format ?? spec.config?.numberFormat ?? null;
    return pattern ? formatNumberWithPattern(value, pattern) : formatTooltipNumber(value);
}

export function dimLabel(
    spec: VizSpec,
    channel: 'x' | 'y' | 'color',
    value: unknown,
    data: VizData
): string {
    if (value === null || value === undefined) return '—';
    const definition = spec.encoding[channel];
    const column = data.columns.find((candidate) => candidate.name === channel);
    if (column?.type === 'temporal' && typeof value === 'string') {
        return formatTemporal(value, definition?.timeUnit);
    }
    if (definition?.bin && typeof value === 'number') {
        const width = data.meta.bins?.[channel]?.width;
        if (width) {
            return `${formatNumberWithPattern(value, definition.format)}–${formatNumberWithPattern(value + width, definition.format)}`;
        }
        return formatNumberWithPattern(value, definition.format);
    }
    if (typeof value === 'number') return formatNumberWithPattern(value, definition?.format);
    return String(value);
}

export function tooltipDimLabel(
    spec: VizSpec,
    channel: 'x' | 'y' | 'color',
    value: unknown,
    data: VizData
): string {
    if (value === null || value === undefined) return '—';
    const definition = spec.encoding[channel];
    const column = data.columns.find((candidate) => candidate.name === channel);
    if (column?.type === 'temporal' && typeof value === 'string') {
        return formatTemporal(value, definition?.timeUnit);
    }
    if (definition?.bin && typeof value === 'number') {
        const width = data.meta.bins?.[channel]?.width;
        const format = (number: number) =>
            definition.format
                ? formatNumberWithPattern(number, definition.format)
                : formatTooltipNumber(number);
        return width ? `${format(value)}–${format(value + width)}` : format(value);
    }
    if (typeof value === 'number') {
        return definition?.format
            ? formatNumberWithPattern(value, definition.format)
            : formatTooltipNumber(value);
    }
    return String(value);
}

export function escapeHtml(value: unknown): string {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function tooltipValue(spec: VizSpec, channel: ChannelDef, value: unknown): string {
    if (value == null) return '—';
    if (channel.type === 'temporal' && typeof value === 'string') {
        return formatTemporal(value, channel.timeUnit);
    }
    if (channel.type === 'quantitative' && typeof value === 'number') {
        const pattern = channel.format ?? spec.config?.numberFormat;
        return pattern ? formatNumberWithPattern(value, pattern) : formatTooltipNumber(value);
    }
    return String(value);
}

export function explicitTooltipHtml(spec: VizSpec, data: VizData, rowIndex: number | null): string {
    if (rowIndex == null || rowIndex < 0) return '';
    const row = data.rows[rowIndex];
    if (!row) return '';
    return (spec.encoding.tooltip ?? [])
        .map((channel, index) => {
            const columnIndex = data.columns.findIndex(
                (column) => column.name === `tooltip_${index}` || column.field === channel.field
            );
            if (columnIndex < 0) return '';
            const label = channel.title || channel.field || `Value ${index + 1}`;
            return `<br/><span style="opacity:.72">${escapeHtml(label)}</span>: ${escapeHtml(
                tooltipValue(spec, channel, row[columnIndex])
            )}`;
        })
        .join('');
}

const LABEL_CHAR_W = 6.4;
export const AXIS_NAME_BAND = 18;
export const MAX_LABEL_W = 132;
const TITLE_BAND = 26;
const LEGEND_BAND = 24;

export function textWidth(value: string): number {
    return value.length * LABEL_CHAR_W;
}

export function axisTitle(
    spec: VizSpec,
    channel: 'x' | 'y' | 'color' | 'size'
): string | undefined {
    const definition = spec.encoding[channel];
    if (!definition) return undefined;
    if (definition.title) return definition.title;
    if (definition.aggregate === 'count') return 'COUNT';
    if (!definition.field) return undefined;
    if (definition.aggregate) {
        const aggregate =
            definition.aggregate === 'mean'
                ? 'AVG'
                : definition.aggregate === 'distinct'
                  ? 'COUNTD'
                  : definition.aggregate.toUpperCase();
        return `${aggregate}(${definition.field})`;
    }
    return definition.timeUnit ? `${definition.field} (${definition.timeUnit})` : definition.field;
}

function autoTitle(spec: VizSpec): string | undefined {
    const measure: 'x' | 'y' | null = spec.encoding.y?.aggregate
        ? 'y'
        : spec.encoding.x?.aggregate
          ? 'x'
          : null;
    if (!measure) return undefined;
    const measureTitle = axisTitle(spec, measure);
    if (!measureTitle) return undefined;
    const dimensionTitle = axisTitle(spec, measure === 'y' ? 'x' : 'y');
    return dimensionTitle ? `${measureTitle} by ${dimensionTitle}` : measureTitle;
}

export function chromeOf(
    spec: VizSpec,
    options: CompileOptions | undefined,
    wantsLegend: boolean
): Chrome {
    const chartTitle = spec.title?.trim() || autoTitle(spec);
    return {
        title: options?.showTitle !== false && chartTitle ? chartTitle : undefined,
        legend: wantsLegend && spec.config?.legend !== false,
    };
}

export function baseOption(chrome: Chrome, theme: ChartTheme) {
    return {
        animationDuration: 300,
        aria: { enabled: true, decal: { show: false } },
        backgroundColor: 'transparent',
        textStyle: { color: theme.text },
        title: chrome.title
            ? {
                  text: chrome.title,
                  left: 'center',
                  top: 0,
                  textStyle: {
                      color: theme.isDark ? '#e2e8f0' : '#0f172a',
                      fontSize: 13,
                      fontWeight: 600,
                  },
              }
            : undefined,
        tooltip: {
            backgroundColor: theme.tooltipBg,
            borderColor: theme.tooltipBorder,
            textStyle: { color: theme.text, fontSize: 12 },
        },
        legend: chrome.legend
            ? {
                  textStyle: { color: theme.text, fontSize: 11 },
                  bottom: 0,
                  left: 'center',
                  type: 'scroll',
                  icon: 'roundRect',
                  itemWidth: 11,
                  itemHeight: 8,
              }
            : { show: false },
    };
}

export function gridOf(chrome: Chrome, margins: { left: number; right: number; bottom: number }) {
    return {
        left: margins.left,
        right: margins.right,
        top: 8 + (chrome.title ? TITLE_BAND : 0),
        bottom: margins.bottom + (chrome.legend ? LEGEND_BAND : 0),
        containLabel: false,
    };
}

export function nameConfig(
    name: string | undefined,
    onY: boolean,
    gap: number,
    theme: ChartTheme
): Record<string, unknown> {
    if (!name) return {};
    return {
        name,
        nameLocation: 'middle',
        nameRotate: onY ? 90 : 0,
        nameGap: gap,
        nameTextStyle: { color: theme.text, fontSize: 11, fontWeight: 500 },
    };
}

export function axisCommon(theme: ChartTheme, showGrid: boolean) {
    return {
        axisLabel: { color: theme.text, fontSize: 11 },
        axisLine: { lineStyle: { color: theme.axisLine } },
        axisTick: { lineStyle: { color: theme.axisLine } },
        splitLine: showGrid ? { show: true, lineStyle: { color: theme.grid } } : { show: false },
    };
}

export function valueLabelWidth(values: Array<number | null | undefined>): number {
    let maxAbs = 0;
    let negative = false;
    for (const value of values) {
        if (typeof value !== 'number' || !Number.isFinite(value)) continue;
        maxAbs = Math.max(maxAbs, Math.abs(value));
        if (value < 0) negative = true;
    }
    return Math.max(26, textWidth(formatNumber(maxAbs)) + (negative ? LABEL_CHAR_W : 0) + 6);
}
