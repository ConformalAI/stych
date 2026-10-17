/**
 * Editing engine for the Visualize tab.
 *
 * The draft used by the controls is intentionally separate from the last
 * successfully materialized spec/data pair used by the canvas. This prevents
 * an in-flight edit from relabelling stale data and makes every async chart
 * transition atomic.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import { useStore } from '../../store/useStore';
import {
    emptySpec,
    type FieldCatalog,
    type FieldInfo,
    type SavedViz,
    type SpecErrorItem,
    type VizData,
    type VizSpec,
} from './types';
import { persistVizDraft } from './vizDraftStorage';
import { useVizDataActions } from './useVizDataActions';
import { useVizPersistenceActions } from './useVizPersistenceActions';
import { useVizSpecLifecycle } from './useVizSpecLifecycle';
import { filterSavedVisualizations, type UseVizSpec, type VizConflict } from './useVizSpecTypes';

export type { UseVizSpec, VizConflict } from './useVizSpecTypes';

const UNDO_LIMIT = 50;

export function useVizSpec(sessionId: string): UseVizSpec {
    const lastVizEvent = useStore((state) => state.lastVizEvent);
    const openVizId = useStore((state) => state.openVizId);
    const setOpenVizId = useStore((state) => state.setOpenVizId);
    const uploadedFileIds = useStore((state) =>
        state.uploadedFiles.map((file) => file.file_id).join(',')
    );

    const [catalog, setCatalog] = useState<FieldCatalog | null>(null);
    const [catalogError, setCatalogError] = useState<string | null>(null);
    const [calcFields, setCalcFields] = useState<FieldInfo[]>([]);
    const [calcErrors, setCalcErrors] = useState<SpecErrorItem[]>([]);
    const [saved, setSaved] = useState<SavedViz[]>([]);
    const [activeVizId, setActiveVizId] = useState<string | null>(null);
    const [activeVersion, setActiveVersion] = useState<number | null>(null);
    const [spec, setSpec] = useState<VizSpec>(emptySpec);
    const [renderedSpec, setRenderedSpec] = useState<VizSpec | null>(null);
    const [title, setTitleState] = useState('');
    const [data, setData] = useState<VizData | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [dirty, setDirty] = useState(false);
    const [conflict, setConflict] = useState<VizConflict | null>(null);
    const [specErrors, setSpecErrors] = useState<SpecErrorItem[]>([]);
    const [queryError, setQueryError] = useState<string | null>(null);

    const specRef = useRef(spec);
    const titleRef = useRef(title);
    const activeIdRef = useRef<string | null>(activeVizId);
    const versionRef = useRef<number | null>(activeVersion);
    const dirtyRef = useRef(dirty);
    const draftSessionRef = useRef(sessionId);
    const savedRef = useRef(saved);
    const undoStack = useRef<VizSpec[]>([]);
    const redoStack = useRef<VizSpec[]>([]);
    const sessionGeneration = useRef(0);
    const transitionGeneration = useRef(0);
    const queryGeneration = useRef(0);
    const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    const setActive = useCallback((id: string | null, version: number | null) => {
        activeIdRef.current = id;
        versionRef.current = version;
        setActiveVizId(id);
        setActiveVersion(version);
    }, []);

    const setDirtyState = useCallback((value: boolean) => {
        dirtyRef.current = value;
        setDirty(value);
    }, []);

    const resetStacks = useCallback(() => {
        undoStack.current = [];
        redoStack.current = [];
    }, []);

    const cancelPending = useCallback(() => {
        transitionGeneration.current += 1;
        queryGeneration.current += 1;
        if (debounceTimer.current) {
            clearTimeout(debounceTimer.current);
            debounceTimer.current = null;
        }
        setLoading(false);
    }, []);

    const setDraft = useCallback(
        (next: VizSpec, markDirty: boolean) => {
            draftSessionRef.current = sessionId;
            specRef.current = next;
            titleRef.current = next.title ?? '';
            setSpec(next);
            setTitleState(next.title ?? '');
            if (markDirty) {
                setDirtyState(true);
                persistVizDraft(sessionId, {
                    activeVizId: activeIdRef.current,
                    baseVersion: versionRef.current,
                    spec: next,
                    title: next.title ?? '',
                });
            }
        },
        [sessionId, setDirtyState]
    );

    const {
        runQuery,
        scheduleQuery,
        applySpec,
        loadVisualization,
        selectViz,
        newViz: resetNewViz,
        refreshSaved,
    } = useVizDataActions({
        sessionId,
        specRef,
        titleRef,
        activeIdRef,
        versionRef,
        dirtyRef,
        savedRef,
        sessionGeneration,
        transitionGeneration,
        queryGeneration,
        debounceTimer,
        cancelPending,
        resetStacks,
        setActive,
        setDirtyState,
        setDraft,
        setRenderedSpec,
        setData,
        setLoading,
        setSpecErrors,
        setQueryError,
        setConflict,
        setSaved,
    });

    useVizSpecLifecycle({
        sessionId,
        lastVizEvent,
        openVizId,
        setOpenVizId,
        spec,
        title,
        dirty,
        activeVizId,
        activeVersion,
        specRef,
        activeIdRef,
        versionRef,
        dirtyRef,
        draftSessionRef,
        savedRef,
        sessionGeneration,
        cancelPending,
        resetStacks,
        scheduleQuery,
        setActive,
        setDirtyState,
        setDraft,
        loadVisualization,
        selectViz,
        setCatalog,
        setCatalogError,
        setCalcFields,
        setCalcErrors,
        setSaved,
        setRenderedSpec,
        setData,
        setSpecErrors,
        setQueryError,
        setConflict,
        uploadedFileIds,
    });

    const updateSpec = useCallback(
        (updater: (previous: VizSpec) => VizSpec) => {
            const previous = specRef.current;
            const next = updater(previous);
            if (next === previous) return;
            undoStack.current = [...undoStack.current.slice(-UNDO_LIMIT + 1), previous];
            redoStack.current = [];
            applySpec(next);
        },
        [applySpec]
    );

    const setTitle = useCallback(
        (nextTitle: string) => {
            const previous = specRef.current;
            const next = { ...previous, title: nextTitle || null };
            titleRef.current = nextTitle;
            specRef.current = next;
            setTitleState(nextTitle);
            setSpec(next);
            setRenderedSpec((current) =>
                current ? { ...current, title: nextTitle || null } : current
            );
            setDirtyState(true);
            setConflict(null);
            persistVizDraft(sessionId, {
                activeVizId: activeIdRef.current,
                baseVersion: versionRef.current,
                spec: next,
                title: nextTitle,
            });
        },
        [sessionId, setDirtyState]
    );

    const undo = useCallback(() => {
        const previous = undoStack.current.pop();
        if (!previous) return;
        redoStack.current.push(specRef.current);
        applySpec(previous);
    }, [applySpec]);

    const redo = useCallback(() => {
        const next = redoStack.current.pop();
        if (!next) return;
        undoStack.current.push(specRef.current);
        applySpec(next);
    }, [applySpec]);

    const { save, saveAsCopy, reloadRemote, discardDraft, keepDraft, remove } =
        useVizPersistenceActions({
            sessionId,
            specRef,
            titleRef,
            activeIdRef,
            versionRef,
            savedRef,
            setSaving,
            setSpecErrors,
            setQueryError,
            setConflict,
            setRenderedSpec,
            setData,
            setSaved,
            setActive,
            setDraft,
            setDirtyState,
            refreshSaved,
            newViz: resetNewViz,
            loadVisualization,
        });

    const newViz = useCallback(async () => {
        if (saving) return;
        if (dirtyRef.current) {
            if (loading || !data || !renderedSpec || specErrors.length > 0) {
                setQueryError(
                    loading
                        ? 'Wait for this chart to finish building before creating another.'
                        : 'Finish or discard this chart before creating another.'
                );
                return;
            }
            await save();
        }
        const source =
            specRef.current.data?.source && specRef.current.data.source !== 'active'
                ? specRef.current.data.source
                : (catalog?.source ?? 'active');
        resetNewViz(source);
    }, [
        catalog?.source,
        data,
        loading,
        renderedSpec,
        resetNewViz,
        save,
        saving,
        specErrors.length,
    ]);

    const activeSource =
        spec.data?.source && spec.data.source !== 'active'
            ? spec.data.source
            : (catalog?.source ?? 'active');

    const selectSource = useCallback(
        async (source: string) => {
            const currentSource =
                specRef.current.data?.source && specRef.current.data.source !== 'active'
                    ? specRef.current.data.source
                    : (catalog?.source ?? 'active');
            if (!source || source === currentSource || saving) return;
            if (dirtyRef.current) {
                if (loading || !data || !renderedSpec || specErrors.length > 0) {
                    setQueryError(
                        loading
                            ? 'Wait for this chart to finish building before switching data sources.'
                            : 'Finish or discard this chart before switching data sources.'
                    );
                    return;
                }
                await save();
            }
            resetNewViz(source);
        },
        [
            catalog?.source,
            data,
            loading,
            renderedSpec,
            resetNewViz,
            save,
            saving,
            setQueryError,
            specErrors.length,
        ]
    );

    const refresh = useCallback(() => {
        if (debounceTimer.current) clearTimeout(debounceTimer.current);
        runQuery(specRef.current);
        refreshSaved();
    }, [refreshSaved, runQuery]);

    const effectiveCatalog = useMemo(() => {
        if (!catalog || calcFields.length === 0) return catalog;
        return { ...catalog, fields: [...catalog.fields, ...calcFields] };
    }, [calcFields, catalog]);

    const visibleSaved = useMemo(
        () => filterSavedVisualizations(saved, activeSource),
        [activeSource, saved]
    );

    return useMemo(
        () => ({
            catalog: effectiveCatalog,
            catalogError,
            calcErrors,
            saved: visibleSaved,
            activeSource,
            activeVizId,
            activeVersion,
            spec,
            renderedSpec,
            title,
            setTitle,
            data,
            loading,
            saving,
            dirty,
            conflict,
            specErrors,
            queryError,
            updateSpec,
            undo,
            redo,
            canUndo: undoStack.current.length > 0,
            canRedo: redoStack.current.length > 0,
            newViz,
            selectSource,
            selectViz,
            save,
            saveAsCopy,
            reloadRemote,
            keepDraft,
            discardDraft,
            remove,
            refresh,
            refreshSaved,
        }),
        [
            effectiveCatalog,
            catalogError,
            calcErrors,
            visibleSaved,
            activeSource,
            activeVizId,
            activeVersion,
            spec,
            renderedSpec,
            title,
            setTitle,
            data,
            loading,
            saving,
            dirty,
            conflict,
            specErrors,
            queryError,
            updateSpec,
            undo,
            redo,
            newViz,
            selectSource,
            selectViz,
            save,
            saveAsCopy,
            reloadRemote,
            keepDraft,
            discardDraft,
            remove,
            refresh,
            refreshSaved,
        ]
    );
}
