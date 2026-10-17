import type { ChannelDef, MarkType, ShelfChannel, VizSpec } from './types';

export interface MarkCapabilities {
    channels: readonly ShelfChannel[];
    stacked?: boolean;
    smooth?: boolean;
    points?: boolean;
    grid?: boolean;
    labels?: boolean;
    legend?: boolean;
}

export const MARK_CAPABILITIES: Record<MarkType, MarkCapabilities> = {
    bar: { channels: ['x', 'y', 'color'], stacked: true, grid: true, labels: true, legend: true },
    line: {
        channels: ['x', 'y', 'color'],
        smooth: true,
        points: true,
        grid: true,
        labels: true,
        legend: true,
    },
    area: {
        channels: ['x', 'y', 'color'],
        stacked: true,
        smooth: true,
        points: true,
        grid: true,
        labels: true,
        legend: true,
    },
    scatter: { channels: ['x', 'y', 'color', 'size'], grid: true, legend: true },
    pie: { channels: ['x', 'y', 'color'], labels: true, legend: true },
    histogram: { channels: ['x', 'y', 'color'], grid: true, labels: true, legend: true },
    heatmap: { channels: ['x', 'y', 'color'], grid: true, labels: true, legend: true },
};

function isMeasure(channel: ChannelDef | null | undefined): channel is ChannelDef {
    return Boolean(channel?.aggregate);
}

function isDimension(channel: ChannelDef | null | undefined): channel is ChannelDef {
    return Boolean(channel?.field && !channel.aggregate);
}

function withoutAggregate(channel: ChannelDef): ChannelDef {
    return { ...channel, aggregate: null, bin: null };
}

function withAggregate(channel: ChannelDef): ChannelDef {
    if (channel.aggregate) return channel;
    return {
        ...channel,
        aggregate: channel.type === 'quantitative' ? 'sum' : 'distinct',
        bin: null,
        timeUnit: null,
    };
}

export function isMarkEncodingCompatible(spec: VizSpec): boolean {
    const { x, y, color, size } = spec.encoding;
    const rawNumber = (channel: ChannelDef | null | undefined) =>
        Boolean(
            channel?.field && channel.type === 'quantitative' && !channel.aggregate && !channel.bin
        );
    const aggregateNumber = (channel: ChannelDef | null | undefined) =>
        Boolean(
            channel?.field && channel.type === 'quantitative' && channel.aggregate && !channel.bin
        );
    const optionalDimension = (channel: ChannelDef | null | undefined) =>
        !channel || isDimension(channel);

    switch (spec.mark.type) {
        case 'bar':
            return (
                ((isDimension(x) && isMeasure(y)) || (isMeasure(x) && isDimension(y))) &&
                optionalDimension(color) &&
                !size
            );
        case 'line':
        case 'area':
            return isDimension(x) && isMeasure(y) && optionalDimension(color) && !size;
        case 'pie':
            return isDimension(x) !== isDimension(color) && isMeasure(y) && !size;
        case 'histogram':
            return Boolean(
                x?.field &&
                x.bin &&
                !x.aggregate &&
                (!y || (y.aggregate === 'count' && !y.field)) &&
                optionalDimension(color) &&
                !size
            );
        case 'scatter':
            return (
                optionalDimension(color) &&
                ((rawNumber(x) && rawNumber(y) && (!size || rawNumber(size))) ||
                    (aggregateNumber(x) && aggregateNumber(y) && (!size || aggregateNumber(size))))
            );
        case 'heatmap':
            return isDimension(x) && isDimension(y) && isMeasure(color) && !size;
    }
}

/** Change only the chart type. Field placement remains under the user's control. */
export function transitionMark(spec: VizSpec, nextType: MarkType): VizSpec {
    if (spec.mark.type === nextType) return spec;
    const capabilities = MARK_CAPABILITIES[nextType];
    const color = spec.config?.color;
    const bindingChannel = color?.binding?.channel;
    const keepsColor =
        !color ||
        color.mode === 'single' ||
        (color.mode === 'continuous' && nextType === 'heatmap') ||
        (color.mode === 'categorical' &&
            ((nextType === 'bar' && ['x', 'y', 'color'].includes(bindingChannel ?? '')) ||
                (nextType === 'histogram' && ['x', 'color'].includes(bindingChannel ?? '')) ||
                (nextType === 'pie' && ['x', 'color'].includes(bindingChannel ?? '')) ||
                (['line', 'area', 'scatter'].includes(nextType) && bindingChannel === 'color')));
    return {
        ...spec,
        mark: {
            type: nextType,
            stacked: capabilities.stacked ? Boolean(spec.mark.stacked) : false,
            smooth: capabilities.smooth ? Boolean(spec.mark.smooth) : false,
            point: capabilities.points ? spec.mark.point !== false : false,
        },
        encoding: spec.encoding,
        config: keepsColor ? spec.config : { ...spec.config, color: null },
    };
}

/** Build the layout offered by the explicit "Use recommended arrangement" action. */
export function recommendedMarkEncoding(spec: VizSpec): VizSpec | null {
    const all = [spec.encoding.x, spec.encoding.y, spec.encoding.color, spec.encoding.size].filter(
        (channel): channel is ChannelDef => Boolean(channel)
    );
    const dimensions = all.filter(isDimension);
    const measures = all.filter(isMeasure);
    const quantitative = all.filter((channel) => channel.type === 'quantitative' && channel.field);
    const differentField = (channel: ChannelDef, other: ChannelDef | undefined) =>
        !other?.field || channel.field !== other.field;
    let encoding: VizSpec['encoding'];

    if (spec.mark.type === 'histogram') {
        const field = quantitative[0];
        encoding = {
            x: field ? { ...withoutAggregate(field), bin: field.bin ?? { maxbins: 10 } } : null,
            y: { field: null, type: 'quantitative', aggregate: 'count' },
            color: isDimension(spec.encoding.color) ? spec.encoding.color : null,
            size: null,
            tooltip: spec.encoding.tooltip ?? [],
        };
    } else if (spec.mark.type === 'scatter') {
        const axes = quantitative
            .filter(
                (channel, index, channels) =>
                    channels.findIndex((candidate) => candidate.field === channel.field) === index
            )
            .map(withAggregate);
        encoding = {
            x: axes[0] ?? null,
            y: axes[1] ?? null,
            color:
                spec.encoding.color?.type !== 'quantitative' && isDimension(spec.encoding.color)
                    ? spec.encoding.color
                    : null,
            size: axes[2] ?? null,
            tooltip: spec.encoding.tooltip ?? [],
        };
    } else if (spec.mark.type === 'heatmap') {
        let measure = measures[0];
        let axes = measure
            ? dimensions.filter((channel) => differentField(channel, measure))
            : dimensions;
        if (axes.length < 2) axes = dimensions;
        if (!measure) {
            const rawMeasure = quantitative.find(
                (channel) =>
                    dimensions.filter((dimension) => differentField(dimension, channel)).length >= 2
            );
            if (rawMeasure) {
                measure = withAggregate(rawMeasure);
                axes = dimensions.filter((channel) => differentField(channel, rawMeasure));
            }
        }
        encoding = {
            x: axes[0] ?? null,
            y: axes[1] ?? null,
            color: measure ?? null,
            size: null,
            tooltip: spec.encoding.tooltip ?? [],
        };
    } else if (spec.mark.type === 'pie') {
        const dimension =
            dimensions.find((channel) => channel.type !== 'quantitative') ?? dimensions[0];
        const measure =
            measures.find((channel) => differentField(channel, dimension)) ??
            quantitative.find((channel) => differentField(channel, dimension)) ??
            measures[0];
        encoding = {
            x: dimension ?? null,
            y: measure ? withAggregate(measure) : null,
            color: null,
            size: null,
            tooltip: spec.encoding.tooltip ?? [],
        };
    } else {
        const dimension =
            dimensions.find((channel) => channel.type !== 'quantitative') ?? dimensions[0];
        const measure =
            measures.find((channel) => differentField(channel, dimension)) ??
            quantitative.find((channel) => differentField(channel, dimension)) ??
            measures[0];
        encoding = {
            x: dimension ?? null,
            y: measure ? withAggregate(measure) : null,
            color:
                dimensions.find(
                    (channel) =>
                        differentField(channel, dimension) &&
                        differentField(channel, measure) &&
                        channel.type !== 'quantitative'
                ) ?? null,
            size: null,
            tooltip: spec.encoding.tooltip ?? [],
        };
    }

    const recommendation = { ...spec, encoding };
    return isMarkEncodingCompatible(recommendation) ? recommendation : null;
}
