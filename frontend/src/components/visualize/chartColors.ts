import { PALETTE } from './compileShared';
import type { ColorBinding, ColorConfig, ColorMode, ColorValue, VizData, VizSpec } from './types';

export interface CategoricalColorContext {
    binding: ColorBinding;
    values: ColorValue[];
}

export const CONTINUOUS_PALETTE = ['#E6F7F1', '#34D39E', '#0B8A61'];

function scalar(value: unknown): ColorValue {
    if (value == null || ['string', 'number', 'boolean'].includes(typeof value)) {
        return value as ColorValue;
    }
    return String(value);
}

export function colorValueKey(value: unknown): string {
    if (value === null || value === undefined) return 'null:';
    return `${typeof value}:${String(value)}`;
}

export function displayColorValue(value: ColorValue): string {
    return value == null ? '(Missing)' : String(value);
}

export function sameColorBinding(
    left: ColorBinding | null | undefined,
    right: ColorBinding | null | undefined
): boolean {
    return Boolean(left && right && left.channel === right.channel && left.field === right.field);
}

function contextForChannel(
    spec: VizSpec,
    data: VizData | null | undefined,
    channel: ColorBinding['channel']
): CategoricalColorContext | null {
    const definition = spec.encoding[channel];
    if (!definition?.field || definition.aggregate || !data) return null;
    const column = data.columns.find((candidate) => candidate.name === channel);
    if (!column || column.kind !== 'dimension') return null;
    const columnIndex = data.columns.indexOf(column);
    const values: ColorValue[] = [];
    const seen = new Set<string>();
    for (const row of data.rows) {
        const value = scalar(row[columnIndex]);
        const key = colorValueKey(value);
        if (seen.has(key)) continue;
        seen.add(key);
        values.push(value);
    }
    return { binding: { channel, field: definition.field }, values };
}

export function categoricalColorContext(
    spec: VizSpec,
    data: VizData | null | undefined
): CategoricalColorContext | null {
    const encoded = contextForChannel(spec, data, 'color');
    if (encoded) return encoded;

    switch (spec.mark.type) {
        case 'bar': {
            const y = data?.columns.find((column) => column.name === 'y');
            const x = data?.columns.find((column) => column.name === 'x');
            return y?.kind === 'dimension'
                ? contextForChannel(spec, data, 'y')
                : x?.kind === 'dimension'
                  ? contextForChannel(spec, data, 'x')
                  : null;
        }
        case 'histogram':
            return contextForChannel(spec, data, 'x');
        case 'pie':
            return contextForChannel(spec, data, 'x');
        default:
            return null;
    }
}

export function continuousColorBinding(
    spec: VizSpec,
    data: VizData | null | undefined
): ColorBinding | null {
    if (spec.mark.type !== 'heatmap' || !data) return null;
    const definition = spec.encoding.color;
    const column = data.columns.find((candidate) => candidate.name === 'color');
    return definition?.aggregate && column?.kind === 'measure'
        ? { channel: 'color', field: definition.field ?? null }
        : null;
}

export function effectiveColorMode(spec: VizSpec, data: VizData | null | undefined): ColorMode {
    if (spec.config?.color?.mode) return spec.config.color.mode;
    if (spec.mark.type === 'heatmap' && spec.config?.palette?.length === 1) return 'single';
    if (continuousColorBinding(spec, data)) return 'continuous';
    const categorical = categoricalColorContext(spec, data);
    if (spec.mark.type === 'pie' || categorical?.binding.channel === 'color') {
        return 'categorical';
    }
    return 'single';
}

export function colorPaletteOf(spec: VizSpec): string[] {
    const configured = spec.config?.color?.palette;
    if (configured?.length) return configured;
    const legacy = spec.config?.palette;
    return legacy?.length ? legacy : PALETTE;
}

export function continuousPaletteOf(spec: VizSpec): string[] {
    const color = spec.config?.color;
    if (color?.palette?.length === 1) return [color.palette[0], color.palette[0]];
    if (color?.palette && color.palette.length >= 2) return color.palette;
    const legacy = spec.config?.palette;
    if (legacy?.length === 1) return [legacy[0], legacy[0]];
    return legacy?.length ? legacy : CONTINUOUS_PALETTE;
}

export function singleColorOf(spec: VizSpec): string {
    const color = spec.config?.color;
    if (color?.mode === 'single' && color.value) return color.value;
    return colorPaletteOf(spec)[0];
}

export function colorForValue(
    spec: VizSpec,
    binding: ColorBinding,
    value: unknown,
    fallbackIndex: number
): string {
    const color = spec.config?.color;
    if (color?.mode === 'single' && color.value) return color.value;
    const palette = colorPaletteOf(spec);
    if (color?.mode !== 'categorical' || !sameColorBinding(color.binding, binding)) {
        return palette[fallbackIndex % palette.length];
    }
    const assignment = (color.assignments ?? []).find(
        (candidate) => colorValueKey(candidate.value) === colorValueKey(value)
    );
    return assignment?.color ?? palette[fallbackIndex % palette.length];
}

export function makeColorConfig(
    spec: VizSpec,
    mode: ColorMode,
    data: VizData | null | undefined
): ColorConfig | null {
    const current = spec.config?.color;
    if (mode === 'single') {
        return { ...current, mode, value: singleColorOf(spec) };
    }
    if (mode === 'continuous') {
        const binding = continuousColorBinding(spec, data);
        return binding ? { ...current, mode, binding, palette: continuousPaletteOf(spec) } : null;
    }
    const context = categoricalColorContext(spec, data);
    if (!context) return null;
    return {
        ...current,
        mode,
        binding: context.binding,
        palette: colorPaletteOf(spec),
        assignments:
            current && sameColorBinding(current.binding, context.binding)
                ? current.assignments
                : [],
    };
}

export function labelFontSizeOf(spec: VizSpec, fallback: number): number {
    return spec.config?.labelFontSize ?? fallback;
}

export function labelColorOf(spec: VizSpec, fallback?: string): string | undefined {
    return spec.config?.labelColor ?? fallback;
}
