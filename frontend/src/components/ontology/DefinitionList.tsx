/**
 * Left rail — every definition in the ontology, grouped by kind.
 *
 * Doubles as the "add" surface: entities come from the session's datasets,
 * metrics and dimensions from an entity. Relationships are not added here, they
 * are drawn on the canvas, which is the interaction that makes them obvious.
 */

import { useState } from 'react';
import { AlertTriangle, BadgeCheck, ChevronDown, ChevronRight, Plus } from 'lucide-react';
import {
    emptyDimension,
    emptyEntity,
    emptyMetric,
    uniqueName,
    type DatasetInfo,
    type DefinitionKind,
    type OntologyErrorItem,
    type OntologySpec,
    type Selection,
} from './types';

interface Props {
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errors: OntologyErrorItem[];
    selection: Selection;
    onSelect: (selection: Selection) => void;
    updateSpec: (next: (prev: OntologySpec) => OntologySpec) => void;
}

const GROUPS: { kind: DefinitionKind; label: string }[] = [
    { kind: 'entity', label: 'Entities' },
    { kind: 'relationship', label: 'Relationships' },
    { kind: 'metric', label: 'Metrics' },
    { kind: 'dimension', label: 'Dimensions' },
];

export default function DefinitionList({
    spec,
    datasets,
    errors,
    selection,
    onSelect,
    updateSpec,
}: Props) {
    const [collapsed, setCollapsed] = useState<Set<DefinitionKind>>(new Set());
    const [adding, setAdding] = useState<DefinitionKind | null>(null);

    const itemsFor = (kind: DefinitionKind) => {
        if (kind === 'entity') return spec.entities;
        if (kind === 'relationship') return spec.relationships;
        if (kind === 'metric') return spec.metrics;
        return spec.dimensions;
    };

    const hasError = (kind: DefinitionKind, index: number) => {
        const prefix = `${kind === 'entity' ? 'entities' : `${kind}s`}[${index}]`;
        return errors.some((e) => e.path.startsWith(prefix));
    };

    const addEntity = (dataset: DatasetInfo) => {
        updateSpec((prev) => {
            const name = uniqueName(prev, dataset.filename.replace(/\.[^.]+$/, ''));
            const entity = emptyEntity(name, dataset.id);
            entity.datasetLabel = dataset.filename;
            onSelect({ kind: 'entity', name });
            return { ...prev, entities: [...prev.entities, entity] };
        });
        setAdding(null);
    };

    const addMetric = (entityName: string) => {
        updateSpec((prev) => {
            const name = uniqueName(prev, `${entityName}_count`);
            onSelect({ kind: 'metric', name });
            return { ...prev, metrics: [...prev.metrics, emptyMetric(name, entityName)] };
        });
        setAdding(null);
    };

    const addDimension = (entityName: string) => {
        updateSpec((prev) => {
            const name = uniqueName(prev, `${entityName}_dimension`);
            onSelect({ kind: 'dimension', name });
            return {
                ...prev,
                dimensions: [...prev.dimensions, emptyDimension(name, entityName, '')],
            };
        });
        setAdding(null);
    };

    return (
        <div className="h-full overflow-y-auto py-2">
            {GROUPS.map(({ kind, label }) => {
                const items = itemsFor(kind);
                const isCollapsed = collapsed.has(kind);
                const canAdd =
                    kind === 'entity'
                        ? datasets.length > 0
                        : kind !== 'relationship' && spec.entities.length > 0;

                return (
                    <div key={kind} className="mb-1">
                        <div className="flex items-center gap-1 px-2 py-1">
                            <button
                                type="button"
                                onClick={() =>
                                    setCollapsed((prev) => {
                                        const next = new Set(prev);
                                        if (next.has(kind)) next.delete(kind);
                                        else next.add(kind);
                                        return next;
                                    })
                                }
                                className="flex items-center gap-1 flex-1 min-w-0 text-left"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                {isCollapsed ? (
                                    <ChevronRight className="w-3 h-3 flex-shrink-0" />
                                ) : (
                                    <ChevronDown className="w-3 h-3 flex-shrink-0" />
                                )}
                                <span className="text-[10px] uppercase tracking-widest font-semibold">
                                    {label}
                                </span>
                                <span className="text-[10px] font-mono">{items.length}</span>
                            </button>
                            {canAdd && (
                                <button
                                    type="button"
                                    title={`Add ${kind}`}
                                    onClick={() => setAdding(adding === kind ? null : kind)}
                                    className="p-0.5 rounded transition-colors hover:bg-[var(--c-glass)]"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    <Plus className="w-3 h-3" />
                                </button>
                            )}
                        </div>

                        {/* Add menu */}
                        {adding === kind && (
                            <div
                                className="mx-2 mb-1 rounded-md border overflow-hidden"
                                style={{
                                    borderColor: 'var(--c-glass-border)',
                                    background: 'var(--c-glass)',
                                }}
                            >
                                <p
                                    className="px-2 pt-1.5 pb-1 text-[9px] uppercase tracking-widest"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    {kind === 'entity' ? 'From dataset' : 'On entity'}
                                </p>
                                {kind === 'entity'
                                    ? datasets.map((d) => (
                                          <button
                                              key={d.id}
                                              type="button"
                                              onClick={() => addEntity(d)}
                                              className="w-full text-left px-2 py-1 text-[11px] truncate transition-colors hover:bg-[var(--c-glass)]"
                                              style={{ color: 'var(--c-ink)' }}
                                          >
                                              {d.filename}
                                          </button>
                                      ))
                                    : spec.entities.map((e) => (
                                          <button
                                              key={e.name}
                                              type="button"
                                              onClick={() =>
                                                  kind === 'metric'
                                                      ? addMetric(e.name)
                                                      : addDimension(e.name)
                                              }
                                              className="w-full text-left px-2 py-1 text-[11px] truncate transition-colors hover:bg-[var(--c-glass)]"
                                              style={{ color: 'var(--c-ink)' }}
                                          >
                                              {e.label || e.name}
                                          </button>
                                      ))}
                            </div>
                        )}

                        {!isCollapsed &&
                            items.map((item, index) => {
                                const active =
                                    selection?.kind === kind && selection.name === item.name;
                                const errored = hasError(kind, index);
                                return (
                                    <button
                                        key={item.name}
                                        type="button"
                                        onClick={() => onSelect({ kind, name: item.name })}
                                        className="w-full flex items-center gap-1.5 px-2 py-1 rounded-md text-left transition-colors"
                                        style={{
                                            background: active ? 'var(--c-signal-dim)' : undefined,
                                            color: active
                                                ? 'var(--c-signal-text)'
                                                : 'var(--c-ink-2)',
                                        }}
                                    >
                                        <span className="text-[11px] truncate flex-1 font-mono">
                                            {item.name}
                                        </span>
                                        {errored && (
                                            <AlertTriangle
                                                className="w-3 h-3 flex-shrink-0"
                                                style={{ color: 'var(--c-danger)' }}
                                            />
                                        )}
                                        {item.certified && (
                                            <BadgeCheck
                                                className="w-3 h-3 flex-shrink-0"
                                                style={{ color: 'var(--c-signal-text)' }}
                                            />
                                        )}
                                    </button>
                                );
                            })}

                        {!isCollapsed && items.length === 0 && (
                            <p
                                className="px-2 py-1 text-[10px] italic"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                {kind === 'relationship'
                                    ? 'Drag a column onto another entity'
                                    : `No ${kind === 'entity' ? 'entities' : `${kind}s`} yet`}
                            </p>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
