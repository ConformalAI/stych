/**
 * One entity on the ontology canvas: a card of the dataset's columns.
 *
 * Every field row carries both a source and a target handle keyed by column
 * name, so a relationship is drawn by dragging one column onto another. That is
 * the whole relationship-authoring interaction — the inspector only refines
 * cardinality and join type afterwards.
 */

import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { BadgeCheck, Key, AlertTriangle } from 'lucide-react';
import type { DatasetInfo, EntityDef } from './types';

export interface EntityNodeData extends Record<string, unknown> {
    entity: EntityDef;
    dataset?: DatasetInfo;
    hasError: boolean;
    metricCount: number;
    dimensionCount: number;
    /** Columns used by a relationship — highlighted so joins are visible at a glance. */
    joinFields: Set<string>;
}

const FIELD_ROW_H = 22;
const MAX_VISIBLE_FIELDS = 12;

// Handles sit half-outside the row box, so NO ancestor of theirs may clip.
// That is why neither the card nor the field list uses overflow:hidden —
// rounded corners come from rounding the header/footer instead.
const HANDLE_SIZE = 9;
const handleStyle = {
    width: HANDLE_SIZE,
    height: HANDLE_SIZE,
    background: 'var(--c-slate)',
    border: '1.5px solid var(--c-mist-fg)',
    zIndex: 1,
} as const;

/**
 * Card chrome for each state. Pure and exported so the drag affordance is
 * testable — it cannot be captured mid-gesture through browser automation,
 * which drives a drag as one atomic action.
 */
export function entityCardStyle({
    selected,
    hasError,
    dragging,
}: {
    selected?: boolean;
    hasError?: boolean;
    dragging?: boolean;
}) {
    return {
        borderColor: dragging
            ? 'color-mix(in srgb, var(--c-signal) 45%, transparent)'
            : selected
              ? 'var(--c-signal)'
              : hasError
                ? 'var(--c-danger)'
                : 'var(--c-glass-border)',
        boxShadow: dragging
            ? '0 8px 24px rgba(0,0,0,0.35)'
            : selected
              ? '0 0 0 3px var(--c-signal-dim)'
              : 'var(--c-shadow-sm)',
        cursor: dragging ? 'grabbing' : 'grab',
        // Translucent enough to see the entity you are dragging toward. React
        // Flow moves the node itself, so this IS the drag layer.
        opacity: dragging ? 0.82 : 1,
        transform: dragging ? 'scale(1.03) rotate(-0.4deg)' : undefined,
    } as const;
}

function EntityNodeInner({ data, selected, dragging }: NodeProps) {
    const { entity, dataset, hasError, metricCount, dimensionCount, joinFields } =
        data as EntityNodeData;
    const fields = dataset?.fields ?? [];
    const visible = fields.slice(0, MAX_VISIBLE_FIELDS);
    const hidden = fields.length - visible.length;

    return (
        <div
            className="rounded-xl border text-left transition-shadow"
            style={{
                width: 232,
                background: 'var(--c-slate)',
                ...entityCardStyle({ selected, hasError, dragging }),
            }}
        >
            {/* Header */}
            <div
                className="px-2.5 py-2 border-b rounded-t-xl"
                style={{
                    borderColor: 'var(--c-glass-border)',
                    background: entity.certified ? 'var(--c-signal-dim)' : 'var(--c-glass)',
                }}
            >
                <div className="flex items-center gap-1.5">
                    <span
                        className="text-xs font-semibold truncate"
                        style={{ color: 'var(--c-ink)' }}
                        title={entity.label || entity.name}
                    >
                        {entity.label || entity.name}
                    </span>
                    {entity.certified ? (
                        <BadgeCheck
                            className="w-3 h-3 flex-shrink-0"
                            style={{ color: 'var(--c-signal-text)' }}
                            aria-label="Certified"
                        />
                    ) : (
                        <span
                            className="text-[9px] px-1 py-px rounded uppercase tracking-wide flex-shrink-0 font-mono"
                            style={{ background: 'var(--c-mist)', color: 'var(--c-mist-fg)' }}
                        >
                            draft
                        </span>
                    )}
                    {hasError && (
                        <AlertTriangle
                            className="w-3 h-3 flex-shrink-0 ml-auto"
                            style={{ color: 'var(--c-danger)' }}
                            aria-label="Has errors"
                        />
                    )}
                </div>
                <div
                    className="text-[10px] truncate mt-0.5 font-mono"
                    style={{ color: 'var(--c-mist-fg)' }}
                    title={entity.datasetLabel || entity.dataset}
                >
                    {entity.datasetLabel || entity.dataset}
                </div>
            </div>

            {/* Fields */}
            <div>
                {visible.map((field) => {
                    const isPk = field.name === entity.primaryKey;
                    const isJoin = joinFields.has(field.name);
                    return (
                        <div
                            key={field.name}
                            className="relative flex items-center gap-1 px-2.5 text-[10px]"
                            style={{
                                height: FIELD_ROW_H,
                                color: isJoin ? 'var(--c-signal-text)' : 'var(--c-ink-2)',
                                background: isJoin ? 'var(--c-signal-dim)' : undefined,
                            }}
                        >
                            <Handle
                                type="target"
                                position={Position.Left}
                                id={field.name}
                                style={{ ...handleStyle, left: -(HANDLE_SIZE / 2) }}
                            />
                            {isPk && (
                                <Key
                                    className="w-2.5 h-2.5 flex-shrink-0"
                                    style={{ color: 'var(--c-warning)' }}
                                    aria-label="Primary key"
                                />
                            )}
                            <span className="truncate font-mono" title={field.name}>
                                {field.name}
                            </span>
                            <span
                                className="ml-auto text-[9px] flex-shrink-0"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                {field.type.slice(0, 4)}
                            </span>
                            <Handle
                                type="source"
                                position={Position.Right}
                                id={field.name}
                                style={{ ...handleStyle, right: -(HANDLE_SIZE / 2) }}
                            />
                        </div>
                    );
                })}
                {fields.length === 0 && (
                    <div
                        className="px-2.5 py-2 text-[10px] italic"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        Dataset not loaded in this session
                    </div>
                )}
            </div>

            {/* Footer */}
            <div
                className="px-2.5 py-1 border-t rounded-b-xl flex items-center gap-2 text-[9px]"
                style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
            >
                {hidden > 0 && <span>+{hidden} more</span>}
                <span className="ml-auto">
                    {metricCount} metric{metricCount === 1 ? '' : 's'} · {dimensionCount} dim
                    {dimensionCount === 1 ? '' : 's'}
                </span>
            </div>
        </div>
    );
}

export default memo(EntityNodeInner);
