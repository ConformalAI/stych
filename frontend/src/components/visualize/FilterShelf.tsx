/**
 * Filter shelf: edits spec.transform (pre-aggregation filters). Predicates are
 * offered per field type; values parse to the wire shapes the backend expects
 * (list for in/notin, [low, high] for between, none for isnull/notnull).
 */

import { Plus, X } from 'lucide-react';
import {
    isFilter,
    isFilterAny,
    type FieldCatalog,
    type FilterAnyTransform,
    type FilterTransform,
    type Predicate,
    type VizSpec,
} from './types';

interface Props {
    spec: VizSpec;
    catalog: FieldCatalog | null;
    updateSpec: (updater: (prev: VizSpec) => VizSpec) => void;
}

const PREDICATES_BY_TYPE: Record<string, Array<{ value: Predicate; label: string }>> = {
    nominal: [
        { value: 'eq', label: '=' },
        { value: 'neq', label: '≠' },
        { value: 'in', label: 'is one of' },
        { value: 'notin', label: 'is not one of' },
        { value: 'contains', label: 'contains' },
        { value: 'isnull', label: 'is empty' },
        { value: 'notnull', label: 'is not empty' },
    ],
    quantitative: [
        { value: 'eq', label: '=' },
        { value: 'neq', label: '≠' },
        { value: 'gt', label: '>' },
        { value: 'gte', label: '≥' },
        { value: 'lt', label: '<' },
        { value: 'lte', label: '≤' },
        { value: 'between', label: 'between' },
        { value: 'isnull', label: 'is empty' },
        { value: 'notnull', label: 'is not empty' },
    ],
    temporal: [
        { value: 'gte', label: 'on or after' },
        { value: 'lte', label: 'on or before' },
        { value: 'between', label: 'between' },
        { value: 'isnull', label: 'is empty' },
        { value: 'notnull', label: 'is not empty' },
    ],
};

const inputStyle = {
    background: 'var(--c-slate)',
    border: '1px solid var(--c-glass-border)',
    color: 'var(--c-ink)',
} as const;

function coerceValue(raw: string, fieldType: string): unknown {
    if (fieldType === 'quantitative') {
        const n = Number(raw);
        return Number.isFinite(n) ? n : raw;
    }
    return raw;
}

function ValueEditor({
    t,
    fieldType,
    onChange,
}: {
    t: FilterTransform;
    fieldType: string;
    onChange: (value: unknown) => void;
}) {
    if (t.predicate === 'isnull' || t.predicate === 'notnull') return null;

    const inputType =
        fieldType === 'temporal' ? 'date' : fieldType === 'quantitative' ? 'number' : 'text';

    if (t.predicate === 'between') {
        const pair = Array.isArray(t.value) ? t.value : [null, null];
        return (
            <span className="flex items-center gap-1 min-w-0">
                {[0, 1].map((i) => (
                    <input
                        key={i}
                        type={inputType}
                        value={pair[i] == null ? '' : String(pair[i])}
                        onChange={(e) => {
                            const next = [...pair];
                            next[i] = coerceValue(e.target.value, fieldType);
                            onChange(next);
                        }}
                        className="w-full min-w-0 rounded px-1.5 py-0.5 text-[11px]"
                        style={inputStyle}
                    />
                ))}
            </span>
        );
    }

    if (t.predicate === 'in' || t.predicate === 'notin') {
        const joined = Array.isArray(t.value) ? t.value.join(', ') : '';
        return (
            <input
                type="text"
                placeholder="a, b, c"
                value={joined}
                onChange={(e) =>
                    onChange(
                        e.target.value
                            .split(',')
                            .map((s) => s.trim())
                            .filter(Boolean)
                    )
                }
                className="w-full min-w-0 rounded px-1.5 py-0.5 text-[11px]"
                style={inputStyle}
            />
        );
    }

    return (
        <input
            type={inputType}
            value={t.value == null ? '' : String(t.value)}
            onChange={(e) => onChange(coerceValue(e.target.value, fieldType))}
            className="w-full min-w-0 rounded px-1.5 py-0.5 text-[11px]"
            style={inputStyle}
        />
    );
}

export default function FilterShelf({ spec, catalog, updateSpec }: Props) {
    // This shelf edits only simple filters; calculations and exact selection
    // groups remain intact when a user changes the visible filter list.
    const filters = (spec.transform ?? []).filter(isFilter);
    const selectionFilters = (spec.transform ?? [])
        .map((transform, index) => ({ transform, index }))
        .filter((entry): entry is { transform: FilterAnyTransform; index: number } =>
            isFilterAny(entry.transform)
        );

    const setFilters = (next: FilterTransform[]) =>
        updateSpec((prev) => ({
            ...prev,
            transform: [...(prev.transform ?? []).filter((t) => !isFilter(t)), ...next],
        }));

    const patchFilter = (i: number, p: Partial<FilterTransform>) =>
        setFilters(filters.map((f, idx) => (idx === i ? { ...f, ...p } : f)));

    const addFilter = () => {
        const first = catalog?.fields[0];
        if (!first) return;
        setFilters([
            ...filters,
            { op: 'filter', field: first.name, predicate: 'notnull', value: null },
        ]);
    };

    const clearSelectionFilter = (transformIndex: number) =>
        updateSpec((prev) => ({
            ...prev,
            transform: (prev.transform ?? []).filter((_, index) => index !== transformIndex),
        }));

    const fieldType = (name: string): string =>
        catalog?.fields.find((f) => f.name === name)?.type ?? 'nominal';

    return (
        <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
                <p
                    className="text-[10px] font-semibold uppercase tracking-wider"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Filters
                </p>
                <button
                    type="button"
                    onClick={addFilter}
                    className="flex items-center gap-0.5 text-[11px] font-medium"
                    style={{ color: 'var(--c-signal-text)' }}
                >
                    <Plus className="w-3 h-3" /> Add
                </button>
            </div>
            {filters.length === 0 && selectionFilters.length === 0 && (
                <p className="text-xs italic" style={{ color: 'var(--c-mist-fg)' }}>
                    No filters — full dataset
                </p>
            )}
            {selectionFilters.map(({ transform: selection, index }) => (
                <div
                    key={index}
                    className="flex items-center justify-between gap-2 rounded-lg border px-2 py-1.5 text-xs"
                    style={{
                        background: 'var(--c-glass)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-ink)',
                    }}
                >
                    <span>
                        Exact selection — {selection.clauses.length}{' '}
                        {selection.clauses.length === 1 ? 'group' : 'groups'}
                    </span>
                    <button
                        type="button"
                        onClick={() => clearSelectionFilter(index)}
                        aria-label="Clear exact selection filter"
                        className="rounded p-0.5 hover:bg-white/10"
                        title="Clear selection filter"
                    >
                        <X className="h-3 w-3" />
                    </button>
                </div>
            ))}
            {filters.map((t, i) => {
                const ftype = fieldType(t.field);
                const preds = PREDICATES_BY_TYPE[ftype] ?? PREDICATES_BY_TYPE.nominal;
                return (
                    <div
                        key={i}
                        className="flex flex-col gap-1 rounded-lg border px-2 py-1.5"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                        }}
                    >
                        <div className="flex items-center gap-1.5">
                            <select
                                value={t.field}
                                onChange={(e) => {
                                    const nextType = fieldType(e.target.value);
                                    const stillValid = (
                                        PREDICATES_BY_TYPE[nextType] ?? PREDICATES_BY_TYPE.nominal
                                    ).some((p) => p.value === t.predicate);
                                    patchFilter(i, {
                                        field: e.target.value,
                                        predicate: stillValid ? t.predicate : 'notnull',
                                        value: null,
                                    });
                                }}
                                className="min-w-0 flex-1 rounded px-1.5 py-0.5 text-[11px]"
                                style={inputStyle}
                            >
                                {(catalog?.fields ?? []).map((f) => (
                                    <option key={f.name} value={f.name}>
                                        {f.name}
                                    </option>
                                ))}
                            </select>
                            <select
                                value={t.predicate}
                                onChange={(e) =>
                                    patchFilter(i, {
                                        predicate: e.target.value as Predicate,
                                        value: null,
                                    })
                                }
                                className="rounded px-1.5 py-0.5 text-[11px]"
                                style={inputStyle}
                            >
                                {preds.map((p) => (
                                    <option key={p.value} value={p.value}>
                                        {p.label}
                                    </option>
                                ))}
                            </select>
                            <button
                                type="button"
                                onClick={() => setFilters(filters.filter((_, idx) => idx !== i))}
                                title="Remove filter"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                <X className="w-3 h-3" />
                            </button>
                        </div>
                        <ValueEditor
                            t={t}
                            fieldType={ftype}
                            onChange={(value) => patchFilter(i, { value })}
                        />
                    </div>
                );
            })}
        </div>
    );
}
