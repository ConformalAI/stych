/**
 * Shelf-drop rules.
 *
 * A field always lands on the shelf where the user dropped it. Type inference
 * may add a channel-local default such as SUM, a time unit, or histogram bins,
 * but it never moves, swaps, or creates another shelf.
 */

import type { ChannelDef, FieldCatalog, FieldInfo, ShelfChannel, VizSpec } from './types';

function defaultAggregate(field: FieldInfo | undefined): ChannelDef['aggregate'] {
    return (field?.default_aggregate as ChannelDef['aggregate']) ?? 'sum';
}

/** Default ChannelDef for a field landing on the selected shelf. */
export function channelDefFor(field: FieldInfo, channel: ShelfChannel, spec: VizSpec): ChannelDef {
    const base: ChannelDef = { field: field.name, type: field.type as ChannelDef['type'] };
    if (field.type === 'temporal') {
        return { ...base, timeUnit: channel === 'x' ? 'month' : null };
    }
    if (field.role === 'measure' && field.type === 'quantitative') {
        if (channel === 'x' && spec.mark.type === 'histogram') {
            return { ...base, bin: { maxbins: 10 } };
        }
        if (
            (spec.mark.type === 'bar' && (channel === 'x' || channel === 'y')) ||
            (['line', 'area', 'pie'].includes(spec.mark.type) && channel === 'y') ||
            (spec.mark.type === 'scatter' &&
                (channel === 'x' || channel === 'y' || channel === 'size')) ||
            (spec.mark.type === 'heatmap' && channel === 'color')
        ) {
            return {
                ...base,
                aggregate: defaultAggregate(field),
            };
        }
    }
    return base;
}

export function applyFieldDrop(spec: VizSpec, field: FieldInfo, channel: ShelfChannel): VizSpec {
    const colorBinding = spec.config?.color?.binding;
    return {
        ...spec,
        encoding: {
            ...spec.encoding,
            [channel]: channelDefFor(field, channel, spec),
        },
        config:
            colorBinding?.channel === channel && colorBinding.field !== field.name
                ? { ...spec.config, color: null }
                : spec.config,
    };
}

/** Toggle Tableau-style aggregate measures for a scatter chart. */
export function setScatterMeasureAggregation(
    spec: VizSpec,
    catalog: FieldCatalog | null,
    enabled: boolean
): VizSpec {
    if (spec.mark.type !== 'scatter') return spec;

    const encoding = { ...spec.encoding };
    (['x', 'y', 'size'] as const).forEach((channel) => {
        const current = encoding[channel];
        if (!current?.field || current.type !== 'quantitative') return;
        const field = catalog?.fields.find((candidate) => candidate.name === current.field);
        if (field && field.role !== 'measure') return;
        encoding[channel] = {
            ...current,
            aggregate: enabled ? defaultAggregate(field) : null,
            bin: null,
        };
    });
    return { ...spec, encoding };
}
