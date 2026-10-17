/**
 * The Visualize workspace tab: field list (left) → ECharts canvas (center) →
 * encoding shelves / filters / style inspector (right). All edits — drag,
 * inspector controls, or agent tool calls — converge on the same VizSpec.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
    DndContext,
    DragOverlay,
    KeyboardSensor,
    PointerSensor,
    useSensor,
    useSensors,
    type DragEndEvent,
    type DragStartEvent,
} from '@dnd-kit/core';
import { AlertTriangle, PanelRightClose, PanelRightOpen } from 'lucide-react';
import type { EChartsRendererHandle } from './EChartsRenderer';
import ChartCompatibilityNotice from './ChartCompatibilityNotice';
import EncodingShelves, { SHELF_DROP_PREFIX } from './EncodingShelves';
import FieldList, { FIELD_DRAG_PREFIX, FieldPillBody } from './FieldList';
import FilterShelf from './FilterShelf';
import StylePanel from './StylePanel';
import { applyFieldDrop } from './dropRules';
import { recommendedMarkEncoding } from './markCapabilities';
import { buildSelectionPayload, toggleRows } from './selection';
import { useVizSpec } from './useVizSpec';
import { intermediateFieldForSpec, isQueryable } from './useVizSpecTypes';
import { askVizSelection, validateCalculations } from '../../services/viz';
import { useStore } from '../../store/useStore';
import VersionHistory from './VersionHistory';
import VizChartCanvas from './VizChartCanvas';
import VizIntermediateTable from './VizIntermediateTable';
import VizToolbar from './VizToolbar';
import {
    isCalculate,
    specErrorsFromAxios,
    type AskMode,
    type AskResponse,
    type CalculateTransform,
    type FieldInfo,
    type ShelfChannel,
    type SpecErrorItem,
} from './types';

interface Props {
    sessionId: string;
}

const FIELDS_W = 208; // w-52
const INSPECTOR_W = 256; // w-64
const SLIDE = 'transition-[width] duration-300 ease-in-out';

export default function VizView({ sessionId }: Props) {
    const viz = useVizSpec(sessionId);
    const datasets = useStore((state) => state.uploadedFiles);
    const chartRef = useRef<EChartsRendererHandle>(null);
    const [showHistory, setShowHistory] = useState(false);
    const compactDefault = () => typeof window !== 'undefined' && window.innerWidth < 1024;
    const [fieldsCollapsed, setFieldsCollapsed] = useState(compactDefault);
    const [inspectorCollapsed, setInspectorCollapsed] = useState(compactDefault);
    const [dragField, setDragField] = useState<FieldInfo | null>(null);
    const sensors = useSensors(
        useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
        useSensor(KeyboardSensor)
    );
    const queryable = isQueryable(viz.spec);
    const renderedSpec = queryable ? viz.renderedSpec : null;
    const encodedFields = [
        viz.spec.encoding.x,
        viz.spec.encoding.y,
        viz.spec.encoding.color,
        viz.spec.encoding.size,
    ].filter((channel) => Boolean(channel?.field));
    const intermediateField = intermediateFieldForSpec(viz.spec, viz.catalog, renderedSpec);
    const showCompatibilityError = encodedFields.length > 1 && !queryable;
    const recommendation = showCompatibilityError ? recommendedMarkEncoding(viz.spec) : null;

    // ── "Ask this chart" selection state ─────────────────────────────────────
    const [selectedRows, setSelectedRows] = useState<Set<number>>(new Set());
    const [asking, setAsking] = useState(false);
    const [askResult, setAskResult] = useState<AskResponse | null>(null);
    const [askError, setAskError] = useState<string | null>(null);
    const askGeneration = useRef(0);

    // Row indices are only meaningful for the table they came from.
    useEffect(() => {
        askGeneration.current += 1;
        setSelectedRows(new Set());
        setAsking(false);
        setAskResult(null);
        setAskError(null);
    }, [viz.data]);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setSelectedRows(new Set());
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, []);

    const onSelectRows = useCallback((rows: number[], additive: boolean) => {
        setSelectedRows((prev) =>
            rows.length === 0 && !additive ? new Set() : toggleRows(prev, rows, additive)
        );
    }, []);

    const ask = async (mode: AskMode, question?: string) => {
        if (!viz.data) return;
        if (!renderedSpec) return;
        const selection = buildSelectionPayload(renderedSpec, viz.data, selectedRows);
        if (!selection) {
            setAskResult(null);
            setAskError(
                'This selection cannot be mapped to source rows exactly. Try a chart with grouping dimensions.'
            );
            return;
        }
        const generation = ++askGeneration.current;
        setAsking(true);
        setAskError(null);
        setAskResult(null);
        try {
            const res = await askVizSelection(sessionId, {
                spec: renderedSpec,
                selection,
                mode,
                question: question ?? null,
                viz_id: viz.activeVizId,
            });
            if (generation !== askGeneration.current) return;
            setAskResult(res);
            if (res.drill) viz.refreshSaved();
        } catch (err) {
            if (generation !== askGeneration.current) return;
            const errs = specErrorsFromAxios(err);
            const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
                ?.detail;
            setAskError(
                errs?.[0]?.message ??
                    (typeof detail === 'string'
                        ? detail
                        : 'Could not analyze this selection. Please try a smaller selection.')
            );
        } finally {
            if (generation === askGeneration.current) setAsking(false);
        }
    };

    // ── Calculated fields ────────────────────────────────────────────────────
    const calcEntries = (viz.spec.transform ?? []).filter(isCalculate);

    const onSaveCalc = async (
        prevName: string | null,
        entry: CalculateTransform
    ): Promise<SpecErrorItem[]> => {
        const others = calcEntries.filter((c) => c.as !== prevName);
        const res = await validateCalculations(
            sessionId,
            [...others, entry],
            viz.spec.data?.source
        );
        if (res.errors.length > 0) return res.errors;
        viz.updateSpec((prev) => {
            const rest = (prev.transform ?? []).filter(
                (t) => !(isCalculate(t) && t.as === prevName)
            );
            const lastCalc = rest.reduce((acc, t, i) => (isCalculate(t) ? i + 1 : acc), 0);
            return {
                ...prev,
                transform: [...rest.slice(0, lastCalc), entry, ...rest.slice(lastCalc)],
            };
        });
        return [];
    };

    const onDeleteCalc = (name: string) =>
        viz.updateSpec((prev) => ({
            ...prev,
            transform: (prev.transform ?? []).filter((t) => !(isCalculate(t) && t.as === name)),
        }));

    const onDragStart = (event: DragStartEvent) => {
        if (!String(event.active.id).startsWith(FIELD_DRAG_PREFIX)) return;
        setDragField(
            (event.active.data.current as { field?: FieldInfo } | undefined)?.field ?? null
        );
    };

    const onDragEnd = (event: DragEndEvent) => {
        setDragField(null);
        const fieldId = String(event.active.id);
        const overId = event.over ? String(event.over.id) : '';
        if (!fieldId.startsWith(FIELD_DRAG_PREFIX) || !overId.startsWith(SHELF_DROP_PREFIX)) return;
        const field = (event.active.data.current as { field?: FieldInfo } | undefined)?.field;
        const channel = overId.slice(SHELF_DROP_PREFIX.length) as ShelfChannel;
        if (!field) return;
        viz.updateSpec((prev) => applyFieldDrop(prev, field, channel));
    };

    const errors = [...viz.specErrors, ...viz.calcErrors];

    return (
        <div className="flex-1 flex flex-col min-h-0 p-3 sm:p-4 gap-3">
            {/* ── Error banner ── */}
            {viz.conflict && (
                <div
                    role="alert"
                    className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs"
                    style={{
                        background: 'color-mix(in srgb, var(--c-warning) 10%, transparent)',
                        borderColor: 'color-mix(in srgb, var(--c-warning) 35%, transparent)',
                        color: 'var(--c-ink-2)',
                    }}
                >
                    <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                    <span className="flex-1 min-w-[180px]">{viz.conflict.message}</span>
                    <button
                        type="button"
                        onClick={() => viz.reloadRemote().catch(() => undefined)}
                        className="rounded-md border px-2 py-1 font-semibold"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        Reload latest
                    </button>
                    <button
                        type="button"
                        onClick={() => viz.saveAsCopy().catch(() => undefined)}
                        className="rounded-md border px-2 py-1 font-semibold"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        Save a copy
                    </button>
                    <button
                        type="button"
                        onClick={viz.keepDraft}
                        className="px-2 py-1 font-semibold"
                    >
                        Keep editing
                    </button>
                </div>
            )}

            {(errors.length > 0 || viz.queryError) && (
                <div
                    role="alert"
                    className="flex items-start gap-2 rounded-lg border px-3 py-2 text-xs"
                    style={{
                        background: 'color-mix(in srgb, var(--c-danger) 8%, transparent)',
                        borderColor: 'color-mix(in srgb, var(--c-danger) 30%, transparent)',
                        color: 'var(--c-danger)',
                    }}
                >
                    <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                    <div className="flex flex-col gap-0.5">
                        {viz.queryError && <span>{viz.queryError}</span>}
                        {errors.map((e, i) => (
                            <span key={i}>
                                <span className="font-semibold">{e.path}</span>: {e.message}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {showCompatibilityError && (
                <ChartCompatibilityNotice
                    markType={viz.spec.mark.type}
                    recommendation={recommendation}
                    onApply={(next) => viz.updateSpec(() => next)}
                />
            )}

            {/* ── Workspace ── */}
            <DndContext
                sensors={sensors}
                onDragStart={onDragStart}
                onDragEnd={onDragEnd}
                onDragCancel={() => setDragField(null)}
            >
                <div className="flex-1 flex min-h-0 gap-2 relative">
                    {/* ── Fields sidebar: full height, top aligns with the title band ── */}
                    <div
                        className={`flex-shrink-0 min-h-0 flex overflow-hidden ${SLIDE} max-lg:absolute max-lg:inset-y-0 max-lg:left-0 max-lg:z-30 max-lg:shadow-2xl`}
                        style={{ width: fieldsCollapsed ? 0 : FIELDS_W }}
                    >
                        <FieldList
                            catalog={viz.catalog}
                            error={viz.catalogError}
                            datasets={{
                                datasets,
                                activeSource: viz.activeSource,
                                onSelect: (source) =>
                                    viz.selectSource(source).catch(() => undefined),
                            }}
                            charts={{
                                saved: viz.saved,
                                activeVizId: viz.activeVizId,
                                newChartTitle: viz.title,
                                onSelect: (id) => viz.selectViz(id).catch(() => undefined),
                                onNew: () => viz.newViz().catch(() => undefined),
                            }}
                            calc={{
                                entries: calcEntries,
                                onSave: onSaveCalc,
                                onDelete: onDeleteCalc,
                            }}
                            onCollapse={() => setFieldsCollapsed(true)}
                        />
                    </div>

                    {/* ── Right region: title + action band above, chart + inspector below ── */}
                    <div className="flex-1 min-w-0 flex flex-col min-h-0 gap-2">
                        <VizToolbar
                            viz={viz}
                            chartRef={chartRef}
                            fieldsCollapsed={fieldsCollapsed}
                            onShowFields={() => setFieldsCollapsed(false)}
                            onShowHistory={() => setShowHistory(true)}
                        />

                        {/* Chart + inspector row */}
                        <div className="flex-1 flex min-h-0 gap-2 relative">
                            {/* Chart canvas */}
                            <div
                                className="flex-1 min-w-0 flex flex-col rounded-xl border"
                                style={{
                                    background: 'var(--c-glass)',
                                    borderColor: 'var(--c-glass-border)',
                                }}
                            >
                                {intermediateField ? (
                                    <VizIntermediateTable
                                        field={intermediateField}
                                        data={viz.data}
                                        loading={viz.loading}
                                    />
                                ) : renderedSpec ? (
                                    <VizChartCanvas
                                        viz={viz}
                                        renderedSpec={renderedSpec}
                                        chartRef={chartRef}
                                        selectedRows={selectedRows}
                                        asking={asking}
                                        askResult={askResult}
                                        askError={askError}
                                        onSelectRows={onSelectRows}
                                        onAsk={ask}
                                        onClearSelection={() => setSelectedRows(new Set())}
                                        onCloseInsight={() => {
                                            askGeneration.current += 1;
                                            setAsking(false);
                                            setAskResult(null);
                                            setAskError(null);
                                        }}
                                    />
                                ) : (
                                    <div
                                        className="flex flex-1 items-center justify-center text-sm"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        {showCompatibilityError
                                            ? 'This chart type cannot use the current field arrangement.'
                                            : viz.loading
                                              ? 'Building chart…'
                                              : 'Drag fields onto the shelves to start.'}
                                    </div>
                                )}
                            </div>

                            {/* Inspector: encoding / filters / style — grayed card */}
                            <div
                                className={`flex-shrink-0 min-h-0 flex overflow-hidden ${SLIDE} max-lg:absolute max-lg:inset-y-0 max-lg:right-0 max-lg:z-30 max-lg:shadow-2xl`}
                                style={{ width: inspectorCollapsed ? 0 : INSPECTOR_W }}
                            >
                                <div
                                    className="w-64 flex-shrink-0 flex flex-col gap-4 overflow-y-auto rounded-xl border p-3"
                                    style={{
                                        background: 'var(--c-glass)',
                                        borderColor: 'var(--c-glass-border)',
                                    }}
                                >
                                    <div className="flex items-center justify-start">
                                        <button
                                            type="button"
                                            onClick={() => setInspectorCollapsed(true)}
                                            title="Hide inspector"
                                            className="flex items-center justify-center w-6 h-6 rounded-md border transition-colors"
                                            style={{
                                                background: 'var(--c-slate)',
                                                borderColor: 'var(--c-glass-border)',
                                                color: 'var(--c-signal-text)',
                                            }}
                                        >
                                            <PanelRightClose className="w-3.5 h-3.5" />
                                        </button>
                                    </div>
                                    <EncodingShelves
                                        spec={viz.spec}
                                        catalog={viz.catalog}
                                        updateSpec={viz.updateSpec}
                                    />
                                    <FilterShelf
                                        spec={viz.spec}
                                        catalog={viz.catalog}
                                        updateSpec={viz.updateSpec}
                                    />
                                    <StylePanel
                                        spec={viz.spec}
                                        data={viz.data}
                                        updateSpec={viz.updateSpec}
                                    />
                                </div>
                            </div>

                            {/* Inspector expand handle — small square, only when collapsed */}
                            {inspectorCollapsed && (
                                <button
                                    type="button"
                                    onClick={() => setInspectorCollapsed(false)}
                                    title="Show inspector"
                                    className="absolute right-2 top-2 z-10 flex items-center justify-center w-6 h-6 rounded-md border shadow-sm"
                                    style={{
                                        background: 'var(--c-slate)',
                                        borderColor: 'var(--c-glass-border)',
                                        color: 'var(--c-mist-fg)',
                                    }}
                                >
                                    <PanelRightOpen className="w-3.5 h-3.5" />
                                </button>
                            )}
                            {showHistory && viz.activeVizId && viz.activeVersion != null && (
                                <VersionHistory
                                    vizId={viz.activeVizId}
                                    currentVersion={viz.activeVersion}
                                    onClose={() => setShowHistory(false)}
                                    onRestored={viz.reloadRemote}
                                />
                            )}
                        </div>
                    </div>
                </div>

                {/* Floating copy of the field pill that follows the pointer. */}
                <DragOverlay dropAnimation={null}>
                    {dragField ? <FieldPillBody field={dragField} overlay /> : null}
                </DragOverlay>
            </DndContext>
        </div>
    );
}
