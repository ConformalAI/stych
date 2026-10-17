/**
 * Encoding shelves (x / y / color / size): droppable targets that edit
 * spec.encoding. Clicking a filled shelf expands its channel options
 * (aggregate, timeUnit, bin, sort, topK). All edits go through updateSpec —
 * the same spec the agent edits.
 */

import { useState } from 'react';
import { useDroppable } from '@dnd-kit/core';
import { X, ChevronDown, ChevronRight } from 'lucide-react';
import {
    SHELF_CHANNELS,
    type Aggregate,
    type ChannelDef,
    type FieldCatalog,
    type ShelfChannel,
    type SortDir,
    type TimeUnit,
    type VizSpec,
} from './types';
import { MARK_CAPABILITIES } from './markCapabilities';
import { setScatterMeasureAggregation } from './dropRules';

/** Select value ↔ ChannelDef.sort mapping. A dimension can sort by its own
 *  values ("asc"/"desc") or BY the measure ("val_asc"/"val_desc" → sort-by-encoding). */
function sortToSelectValue(sort: ChannelDef['sort']): string {
    if (sort == null) return '';
    if (typeof sort === 'string') return sort;
    return `val_${sort.order}`;
}

export const SHELF_DROP_PREFIX = 'shelf:';

const SHELF_LABELS: Record<ShelfChannel, string> = {
    x: 'X axis',
    y: 'Y axis',
    color: 'Color',
    size: 'Size',
};

const AGGREGATES: Array<{ value: Aggregate | ''; label: string }> = [
    { value: '', label: 'None (group by)' },
    { value: 'sum', label: 'Sum' },
    { value: 'mean', label: 'Average' },
    { value: 'min', label: 'Minimum' },
    { value: 'max', label: 'Maximum' },
    { value: 'count', label: 'Count' },
    { value: 'distinct', label: 'Distinct count' },
];

const TIME_UNITS: Array<TimeUnit | ''> = ['', 'year', 'quarter', 'month', 'week', 'day', 'hour'];

interface Props {
    spec: VizSpec;
    catalog: FieldCatalog | null;
    updateSpec: (updater: (prev: VizSpec) => VizSpec) => void;
}

function channelSummary(ch: ChannelDef): string {
    if (ch.aggregate === 'count') return 'COUNT(*)';
    const parts: string[] = [];
    if (ch.aggregate === 'distinct') parts.push(`COUNTD(${ch.field})`);
    else if (ch.aggregate) {
        const aggregate = ch.aggregate === 'mean' ? 'AVG' : ch.aggregate.toUpperCase();
        parts.push(`${aggregate}(${ch.field})`);
    } else if (ch.timeUnit) parts.push(`${ch.timeUnit.toUpperCase()}(${ch.field})`);
    else parts.push(ch.field ?? '');
    if (ch.bin) parts.push(`· ${ch.bin.maxbins} bins`);
    if (ch.topK) parts.push(`· top ${ch.topK.n}${ch.topK.others ? '+other' : ''}`);
    if (ch.sort) {
        if (typeof ch.sort === 'string') parts.push(ch.sort === 'asc' ? '↑' : '↓');
        else parts.push(ch.sort.order === 'asc' ? '↑ val' : '↓ val');
    }
    return parts.join(' ');
}

const selectStyle = {
    background: 'var(--c-slate)',
    border: '1px solid var(--c-glass-border)',
    color: 'var(--c-ink)',
} as const;

function ChannelOptions({
    channel,
    ch,
    catalog,
    patch,
    measureChannel,
}: {
    channel: ShelfChannel;
    ch: ChannelDef;
    catalog: FieldCatalog | null;
    patch: (p: Partial<ChannelDef>) => void;
    /** The chart's measure channel, if any — enables "sort by value". */
    measureChannel: ShelfChannel | null;
}) {
    const info = ch.field ? catalog?.fields.find((f) => f.name === ch.field) : null;
    const isQuant = info?.type === 'quantitative';
    const isTemporal = info?.type === 'temporal';
    const isDim = !ch.aggregate;
    const isCategorical = isDim && !isTemporal && !ch.bin;
    // A dimension can be ordered by the measure (sort-by-encoding) as well as
    // by its own values — the fix for "sort a bar chart by value, not label".
    const canSortByValue = isDim && measureChannel != null && measureChannel !== channel;

    return (
        <div className="flex flex-col gap-1.5 px-2 pb-2 pt-1">
            {(isQuant || ch.aggregate === 'count') && (
                <label className="flex items-center justify-between gap-2 text-[11px]">
                    <span style={{ color: 'var(--c-mist-fg)' }}>Aggregate</span>
                    <select
                        className="rounded px-1.5 py-0.5 text-[11px]"
                        style={selectStyle}
                        value={ch.aggregate ?? ''}
                        onChange={(e) =>
                            patch({
                                aggregate: (e.target.value || null) as Aggregate | null,
                                bin: e.target.value ? null : ch.bin,
                            })
                        }
                    >
                        {AGGREGATES.map((a) => (
                            <option key={a.value} value={a.value}>
                                {a.label}
                            </option>
                        ))}
                    </select>
                </label>
            )}
            {isTemporal && (
                <label className="flex items-center justify-between gap-2 text-[11px]">
                    <span style={{ color: 'var(--c-mist-fg)' }}>Time unit</span>
                    <select
                        className="rounded px-1.5 py-0.5 text-[11px]"
                        style={selectStyle}
                        value={ch.timeUnit ?? ''}
                        onChange={(e) =>
                            patch({ timeUnit: (e.target.value || null) as TimeUnit | null })
                        }
                    >
                        {TIME_UNITS.map((u) => (
                            <option key={u} value={u}>
                                {u || 'exact value'}
                            </option>
                        ))}
                    </select>
                </label>
            )}
            {isQuant && isDim && (
                <label className="flex items-center justify-between gap-2 text-[11px]">
                    <span style={{ color: 'var(--c-mist-fg)' }}>Bin</span>
                    <span className="flex items-center gap-1.5">
                        <input
                            type="checkbox"
                            checked={Boolean(ch.bin)}
                            onChange={(e) =>
                                patch({ bin: e.target.checked ? { maxbins: 10 } : null })
                            }
                        />
                        {ch.bin && (
                            <input
                                type="number"
                                min={2}
                                max={100}
                                value={ch.bin.maxbins}
                                onChange={(e) =>
                                    patch({
                                        bin: { maxbins: Math.max(2, Number(e.target.value) || 10) },
                                    })
                                }
                                className="w-14 rounded px-1.5 py-0.5 text-[11px]"
                                style={selectStyle}
                            />
                        )}
                    </span>
                </label>
            )}
            {isCategorical && (
                <label className="flex items-center justify-between gap-2 text-[11px]">
                    <span style={{ color: 'var(--c-mist-fg)' }}>Top K</span>
                    <span className="flex items-center gap-1.5">
                        <input
                            type="checkbox"
                            checked={Boolean(ch.topK)}
                            onChange={(e) =>
                                patch({ topK: e.target.checked ? { n: 10, others: true } : null })
                            }
                        />
                        {ch.topK && (
                            <>
                                <input
                                    type="number"
                                    min={1}
                                    max={500}
                                    value={ch.topK.n}
                                    onChange={(e) =>
                                        patch({
                                            topK: {
                                                ...ch.topK!,
                                                n: Math.max(1, Number(e.target.value) || 10),
                                            },
                                        })
                                    }
                                    className="w-14 rounded px-1.5 py-0.5 text-[11px]"
                                    style={selectStyle}
                                />
                                <label
                                    className="flex items-center gap-1 text-[10px]"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    <input
                                        type="checkbox"
                                        checked={ch.topK.others}
                                        onChange={(e) =>
                                            patch({
                                                topK: { ...ch.topK!, others: e.target.checked },
                                            })
                                        }
                                    />
                                    other
                                </label>
                            </>
                        )}
                    </span>
                </label>
            )}
            <label className="flex items-center justify-between gap-2 text-[11px]">
                <span style={{ color: 'var(--c-mist-fg)' }}>Sort</span>
                <select
                    className="rounded px-1.5 py-0.5 text-[11px]"
                    style={selectStyle}
                    value={sortToSelectValue(ch.sort)}
                    onChange={(e) => {
                        const v = e.target.value;
                        if (v === 'val_asc' || v === 'val_desc') {
                            patch({
                                sort: {
                                    encoding: measureChannel as ShelfChannel,
                                    order: v === 'val_asc' ? 'asc' : 'desc',
                                },
                            });
                        } else {
                            patch({ sort: (v || null) as SortDir | null });
                        }
                    }}
                >
                    <option value="">auto</option>
                    <option value="asc">label ↑</option>
                    <option value="desc">label ↓</option>
                    {canSortByValue && (
                        <>
                            <option value="val_desc">value ↓</option>
                            <option value="val_asc">value ↑</option>
                        </>
                    )}
                </select>
            </label>
            {channel === 'x' || channel === 'y' ? (
                <label className="flex items-center justify-between gap-2 text-[11px]">
                    <span style={{ color: 'var(--c-mist-fg)' }}>Axis title</span>
                    <input
                        type="text"
                        value={ch.title ?? ''}
                        placeholder="auto"
                        onChange={(e) => patch({ title: e.target.value || null })}
                        className="w-28 rounded px-1.5 py-0.5 text-[11px]"
                        style={selectStyle}
                    />
                </label>
            ) : null}
        </div>
    );
}

function Shelf({
    channel,
    spec,
    catalog,
    updateSpec,
    supported,
}: {
    channel: ShelfChannel;
    spec: VizSpec;
    catalog: FieldCatalog | null;
    updateSpec: Props['updateSpec'];
    supported: boolean;
}) {
    const { setNodeRef, isOver } = useDroppable({
        id: `${SHELF_DROP_PREFIX}${channel}`,
        disabled: !supported,
    });
    const [open, setOpen] = useState(false);
    const ch = spec.encoding[channel] ?? null;
    // First channel carrying an aggregate is the measure — the target for
    // "sort by value" on the other (dimension) channels.
    const measureChannel = SHELF_CHANNELS.find((c) => spec.encoding[c]?.aggregate != null) ?? null;

    const patch = (p: Partial<ChannelDef>) =>
        updateSpec((prev) => {
            const nextChannel = { ...(prev.encoding[channel] as ChannelDef), ...p };
            const color = prev.config?.color;
            const invalidatesCategoricalBinding =
                color?.mode === 'categorical' &&
                color.binding?.channel === channel &&
                Boolean(nextChannel.aggregate);
            return {
                ...prev,
                encoding: {
                    ...prev.encoding,
                    [channel]: nextChannel,
                },
                config: invalidatesCategoricalBinding
                    ? { ...prev.config, color: null }
                    : prev.config,
            };
        });

    const clear = () =>
        updateSpec((prev) => ({
            ...prev,
            encoding: { ...prev.encoding, [channel]: null },
            config:
                prev.config?.color?.binding?.channel === channel
                    ? { ...prev.config, color: null }
                    : prev.config,
        }));

    return (
        <div
            ref={setNodeRef}
            className="rounded-lg border transition-colors"
            style={{
                borderColor: isOver ? 'var(--c-signal)' : 'var(--c-glass-border)',
                background: isOver ? 'var(--c-signal-dim)' : 'var(--c-glass)',
            }}
        >
            <div className="flex items-center gap-2 px-2 py-1.5">
                <span
                    className="text-[10px] font-semibold uppercase tracking-wider w-11 flex-shrink-0"
                    style={{ color: 'var(--c-mist-fg)' }}
                    title={
                        supported
                            ? undefined
                            : `Remove this channel or choose a compatible chart type`
                    }
                >
                    {SHELF_LABELS[channel]}
                    {!supported && ' !'}
                </span>
                {ch ? (
                    <>
                        <button
                            type="button"
                            onClick={() => setOpen((v) => !v)}
                            className="flex items-center gap-1 min-w-0 flex-1 text-left text-xs font-medium"
                            style={{ color: 'var(--c-signal-text)' }}
                        >
                            {open ? (
                                <ChevronDown className="w-3 h-3 flex-shrink-0" />
                            ) : (
                                <ChevronRight className="w-3 h-3 flex-shrink-0" />
                            )}
                            <span className="truncate">{channelSummary(ch)}</span>
                        </button>
                        <button
                            type="button"
                            onClick={clear}
                            title="Remove"
                            className="flex-shrink-0"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            <X className="w-3 h-3" />
                        </button>
                    </>
                ) : (
                    <span className="text-xs italic" style={{ color: 'var(--c-mist-fg)' }}>
                        drop a field
                    </span>
                )}
            </div>
            {ch && open && (
                <ChannelOptions
                    channel={channel}
                    ch={ch}
                    catalog={catalog}
                    patch={patch}
                    measureChannel={measureChannel}
                />
            )}
        </div>
    );
}

export default function EncodingShelves({ spec, catalog, updateSpec }: Props) {
    const allowed = MARK_CAPABILITIES[spec.mark.type].channels;
    const visible = SHELF_CHANNELS.filter(
        (channel) => allowed.includes(channel) || Boolean(spec.encoding[channel])
    );
    const scatterMeasures = (['x', 'y', 'size'] as const)
        .map((channel) => spec.encoding[channel])
        .filter((channel) => channel?.field && channel.type === 'quantitative');
    const scatterAggregated =
        scatterMeasures.length > 0 && scatterMeasures.every((channel) => channel?.aggregate);

    return (
        <div className="flex flex-col gap-1.5">
            <p
                className="text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                Encoding
            </p>
            {spec.mark.type === 'scatter' && (
                <label
                    className="flex items-center justify-between gap-2 rounded-lg border px-2 py-1.5 text-[11px]"
                    style={{
                        background: 'var(--c-glass)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-mist-fg)',
                    }}
                >
                    <span>Aggregate measures</span>
                    <input
                        type="checkbox"
                        checked={scatterAggregated}
                        disabled={scatterMeasures.length === 0}
                        onChange={(event) =>
                            updateSpec((prev) =>
                                setScatterMeasureAggregation(prev, catalog, event.target.checked)
                            )
                        }
                    />
                </label>
            )}
            {visible.map((c) => (
                <Shelf
                    key={c}
                    channel={c}
                    spec={spec}
                    catalog={catalog}
                    updateSpec={updateSpec}
                    supported={allowed.includes(c)}
                />
            ))}
        </div>
    );
}
