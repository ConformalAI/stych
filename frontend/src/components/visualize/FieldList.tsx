/**
 * Draggable field catalog panel (dimensions vs measures), Tableau-style, plus
 * the calculated-fields section: user-defined expression columns (validated
 * server-side by backend/viz/expr.py) that drag onto shelves exactly like
 * real columns. Drag handling lives in VizView.
 */

import { useState } from 'react';
import { useDraggable } from '@dnd-kit/core';
import {
    Calendar,
    Hash,
    PanelLeftClose,
    Pencil,
    Plus,
    Sigma,
    ToggleLeft,
    Type,
    KeyRound,
    X,
} from 'lucide-react';
import type { CalculateTransform, FieldCatalog, FieldInfo, SavedViz, SpecErrorItem } from './types';
import DatasetSelector, { type DatasetSelectProps } from './DatasetSelector';

export const FIELD_DRAG_PREFIX = 'field:';

function fieldIcon(f: FieldInfo) {
    if (f.is_calculated) return <Sigma className="w-3 h-3" />;
    if (f.is_id) return <KeyRound className="w-3 h-3" />;
    switch (f.type) {
        case 'temporal':
            return <Calendar className="w-3 h-3" />;
        case 'quantitative':
            return <Hash className="w-3 h-3" />;
        case 'nominal':
        case 'ordinal':
            return f.cardinality === 2 ? (
                <ToggleLeft className="w-3 h-3" />
            ) : (
                <Type className="w-3 h-3" />
            );
        default:
            return <Type className="w-3 h-3" />;
    }
}

/**
 * Presentational pill — shared by the in-list draggable and the DragOverlay
 * (the floating copy that follows the pointer while dragging).
 */
export function FieldPillBody({ field, overlay = false }: { field: FieldInfo; overlay?: boolean }) {
    const isMeasure = field.role === 'measure';
    return (
        <div
            className="flex items-center gap-1.5 px-2 py-1 rounded-md border text-xs select-none"
            style={{
                background: overlay ? 'var(--c-slate)' : 'var(--c-glass)',
                borderColor: overlay
                    ? 'color-mix(in srgb, var(--c-signal) 45%, transparent)'
                    : 'var(--c-glass-border)',
                color: isMeasure ? 'var(--c-signal-text)' : 'var(--c-ink-2)',
                opacity: !overlay && field.is_id ? 0.6 : 1,
                boxShadow: overlay ? '0 8px 24px rgba(0,0,0,0.35)' : undefined,
                cursor: overlay ? 'grabbing' : undefined,
            }}
        >
            <span style={{ color: isMeasure ? 'var(--c-signal-text)' : 'var(--c-mist-fg)' }}>
                {fieldIcon(field)}
            </span>
            <span className="truncate">{field.name}</span>
        </div>
    );
}

function FieldPill({ field }: { field: FieldInfo }) {
    const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
        id: `${FIELD_DRAG_PREFIX}${field.name}`,
        data: { field },
    });
    const hint = [
        field.type,
        field.is_calculated ? 'calculated' : null,
        field.cardinality != null ? `${field.cardinality} distinct` : null,
        field.null_pct ? `${field.null_pct}% null` : null,
        field.is_id ? 'looks like an ID' : null,
    ]
        .filter(Boolean)
        .join(' · ');

    return (
        <div
            ref={setNodeRef}
            {...listeners}
            {...attributes}
            title={`Drag to an encoding shelf · ${hint}`}
            aria-label={`Drag ${field.name} to an encoding shelf. ${hint}`}
            className="cursor-grab active:cursor-grabbing flex-1 min-w-0"
            style={{ opacity: isDragging ? 0.35 : 1 }}
        >
            <FieldPillBody field={field} />
        </div>
    );
}

// ---------------------------------------------------------------------------
// Calculated-field editor
// ---------------------------------------------------------------------------

export interface CalcSectionProps {
    entries: CalculateTransform[];
    /** Validate + commit; resolves to [] on success, or the blocking errors. */
    onSave: (prevName: string | null, entry: CalculateTransform) => Promise<SpecErrorItem[]>;
    onDelete: (name: string) => void;
}

const EXPR_PLACEHOLDER = "(revenue - cost) / revenue\nif(qty >= 5, 'high', 'low')";

function CalcEditor({
    initial,
    onSave,
    onCancel,
}: {
    initial: CalculateTransform | null;
    onSave: CalcSectionProps['onSave'];
    onCancel: () => void;
}) {
    const [name, setName] = useState(initial?.as ?? '');
    const [expr, setExpr] = useState(initial?.expr ?? '');
    const [errors, setErrors] = useState<SpecErrorItem[]>([]);
    const [saving, setSaving] = useState(false);

    const inputStyle = {
        background: 'var(--c-slate)',
        border: '1px solid var(--c-glass-border)',
        color: 'var(--c-ink)',
    } as const;

    const save = async () => {
        if (!name.trim() || !expr.trim()) return;
        setSaving(true);
        try {
            const errs = await onSave(initial?.as ?? null, {
                op: 'calculate',
                as: name.trim(),
                expr: expr.trim(),
            });
            if (errs.length === 0) onCancel();
            else setErrors(errs);
        } catch {
            setErrors([{ path: '', code: 'network', message: 'Could not validate the field.' }]);
        } finally {
            setSaving(false);
        }
    };

    return (
        <div
            className="flex flex-col gap-1.5 rounded-lg border p-2"
            style={{ background: 'var(--c-glass)', borderColor: 'var(--c-glass-border)' }}
        >
            <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Field name"
                className="rounded px-1.5 py-1 text-[11px]"
                style={inputStyle}
                autoFocus
            />
            <textarea
                value={expr}
                onChange={(e) => setExpr(e.target.value)}
                placeholder={EXPR_PLACEHOLDER}
                rows={3}
                spellCheck={false}
                className="rounded px-1.5 py-1 text-[11px] font-mono resize-y"
                style={inputStyle}
            />
            {errors.map((e, i) => (
                <p key={i} className="text-[10px]" style={{ color: 'var(--c-danger)' }}>
                    {e.message}
                </p>
            ))}
            <div className="flex items-center gap-2">
                <button
                    type="button"
                    onClick={save}
                    disabled={saving || !name.trim() || !expr.trim()}
                    className="rounded px-2 py-0.5 text-[11px] font-semibold disabled:opacity-50"
                    style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
                >
                    {saving ? 'Checking…' : 'Save'}
                </button>
                <button
                    type="button"
                    onClick={onCancel}
                    className="text-[11px]"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

/** Saved-chart selector, hosted in the sidebar header. */
export interface ChartSelectProps {
    saved: SavedViz[];
    activeVizId: string | null;
    newChartTitle: string;
    onSelect: (id: string) => void;
    onNew: () => void;
}

interface Props {
    catalog: FieldCatalog | null;
    error?: string | null;
    calc?: CalcSectionProps;
    charts: ChartSelectProps;
    datasets: DatasetSelectProps;
    onCollapse: () => void;
}

export default function FieldList({ catalog, error, calc, charts, datasets, onCollapse }: Props) {
    // null = closed; '' = creating; a name = editing that calc field
    const [editing, setEditing] = useState<string | null>(null);

    const base = catalog?.fields.filter((f) => !f.is_calculated) ?? [];
    const dims = base.filter((f) => f.role === 'dimension');
    const measures = base.filter((f) => f.role === 'measure');
    const calculated = catalog?.fields.filter((f) => f.is_calculated) ?? [];

    const sectionTitle = (label: string) => (
        <p
            className="text-[10px] font-semibold uppercase tracking-wider mb-1.5"
            style={{ color: 'var(--c-mist-fg)' }}
        >
            {label}
        </p>
    );

    return (
        <div
            className="w-52 flex-shrink-0 flex flex-col gap-3 rounded-xl border p-3 overflow-y-auto"
            style={{ background: 'var(--c-glass)', borderColor: 'var(--c-glass-border)' }}
        >
            <DatasetSelector {...datasets} />

            {/* Chart selector — lives with the fields it configures */}
            <div className="flex items-center gap-1.5">
                <select
                    value={charts.activeVizId ?? ''}
                    onChange={(e) =>
                        e.target.value ? charts.onSelect(e.target.value) : charts.onNew()
                    }
                    className="flex-1 min-w-0 rounded-md border px-2 py-1.5 text-xs font-medium"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-ink)',
                    }}
                >
                    <option value="">
                        {charts.activeVizId ? 'New chart…' : charts.newChartTitle || 'New Chart'}
                    </option>
                    {charts.saved.map((v) => (
                        <option key={v.id} value={v.id}>
                            {v.title || 'Untitled chart'}
                            {v.source_available === false ? ' — source unavailable' : ''}
                        </option>
                    ))}
                </select>
                <button
                    type="button"
                    onClick={charts.onNew}
                    title="New chart"
                    className="flex-shrink-0"
                    style={{ color: 'var(--c-ink-2)' }}
                >
                    <Plus className="w-4 h-4" />
                </button>
                <button
                    type="button"
                    onClick={onCollapse}
                    title="Hide fields"
                    className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-md border transition-colors"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-signal-text)',
                    }}
                >
                    <PanelLeftClose className="w-3.5 h-3.5" />
                </button>
            </div>

            {error ? (
                <p className="text-xs" style={{ color: 'var(--c-danger)' }}>
                    {error}
                </p>
            ) : !catalog ? (
                <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                    Loading fields…
                </p>
            ) : (
                <>
                    <div>
                        {sectionTitle('Dimensions')}
                        <div className="flex flex-col gap-1">
                            {dims.map((f) => (
                                <FieldPill key={f.name} field={f} />
                            ))}
                        </div>
                    </div>
                    <div>
                        {sectionTitle('Measures')}
                        <div className="flex flex-col gap-1">
                            {measures.map((f) => (
                                <FieldPill key={f.name} field={f} />
                            ))}
                        </div>
                    </div>
                    {calc && (
                        <div>
                            <div className="flex items-center justify-between mb-1.5">
                                <p
                                    className="text-[10px] font-semibold uppercase tracking-wider"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Calculated
                                </p>
                                <button
                                    type="button"
                                    onClick={() => setEditing('')}
                                    title="Add calculated field"
                                    className="flex items-center gap-0.5 text-[11px] font-medium"
                                    style={{ color: 'var(--c-signal-text)' }}
                                >
                                    <Plus className="w-3 h-3" /> Add
                                </button>
                            </div>
                            <div className="flex flex-col gap-1">
                                {calculated.map((f) => (
                                    <div key={f.name} className="flex items-center gap-1">
                                        <FieldPill field={f} />
                                        <button
                                            type="button"
                                            onClick={() => setEditing(f.name)}
                                            title="Edit expression"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            <Pencil className="w-3 h-3" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => calc.onDelete(f.name)}
                                            title="Remove calculated field"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    </div>
                                ))}
                                {editing !== null && (
                                    <CalcEditor
                                        initial={
                                            editing === ''
                                                ? null
                                                : (calc.entries.find((c) => c.as === editing) ??
                                                  null)
                                        }
                                        onSave={calc.onSave}
                                        onCancel={() => setEditing(null)}
                                    />
                                )}
                            </div>
                        </div>
                    )}
                    <p className="text-[10px] mt-auto" style={{ color: 'var(--c-mist-fg)' }}>
                        {catalog.row_count.toLocaleString()} rows · drag a field to a shelf
                    </p>
                </>
            )}
        </div>
    );
}
