import { useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react';
import {
    getVisualization,
    getVisualizationData,
    listVisualizations,
    queryVizSpec,
} from '../../services/viz';
import {
    emptySpec,
    specErrorsFromAxios,
    type SavedViz,
    type SpecErrorItem,
    type VizData,
    type VizSpec,
} from './types';
import { clearVizDraft, readVizDraft, rememberActiveViz } from './vizDraftStorage';
import {
    applyPresentation,
    adoptRowTitle,
    errorDetail,
    hasSameQueryShape,
    isQueryable,
    nextNewChartTitle,
    type VizConflict,
} from './useVizSpecTypes';

const QUERY_DEBOUNCE_MS = 350;

interface Context {
    sessionId: string;
    specRef: MutableRefObject<VizSpec>;
    titleRef: MutableRefObject<string>;
    activeIdRef: MutableRefObject<string | null>;
    versionRef: MutableRefObject<number | null>;
    dirtyRef: MutableRefObject<boolean>;
    savedRef: MutableRefObject<SavedViz[]>;
    sessionGeneration: MutableRefObject<number>;
    transitionGeneration: MutableRefObject<number>;
    queryGeneration: MutableRefObject<number>;
    debounceTimer: MutableRefObject<ReturnType<typeof setTimeout> | null>;
    cancelPending: () => void;
    resetStacks: () => void;
    setActive: (id: string | null, version: number | null) => void;
    setDirtyState: (dirty: boolean) => void;
    setDraft: (spec: VizSpec, dirty: boolean) => void;
    setRenderedSpec: Dispatch<SetStateAction<VizSpec | null>>;
    setData: Dispatch<SetStateAction<VizData | null>>;
    setLoading: Dispatch<SetStateAction<boolean>>;
    setSpecErrors: Dispatch<SetStateAction<SpecErrorItem[]>>;
    setQueryError: Dispatch<SetStateAction<string | null>>;
    setConflict: Dispatch<SetStateAction<VizConflict | null>>;
    setSaved: Dispatch<SetStateAction<SavedViz[]>>;
}

export function useVizDataActions(context: Context) {
    const {
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
    } = context;

    const runQuery = useCallback(
        (requested: VizSpec) => {
            if (!isQueryable(requested)) {
                queryGeneration.current += 1;
                setRenderedSpec(null);
                setData(null);
                setSpecErrors([]);
                setQueryError(null);
                setLoading(false);
                return;
            }
            const generation = ++queryGeneration.current;
            setLoading(true);
            queryVizSpec(sessionId, requested)
                .then((response) => {
                    if (generation !== queryGeneration.current) return;
                    const currentDraft = {
                        ...specRef.current,
                        title: titleRef.current || null,
                    };
                    const canonical = applyPresentation(response.spec, currentDraft);
                    setRenderedSpec(canonical);
                    setData(response.data);
                    // The server may add derived channels (for example the count
                    // behind a histogram). Keep those in the rendered spec only:
                    // the shelves should continue to reflect exactly what the
                    // user placed there.
                    setDraft(
                        {
                            ...currentDraft,
                            data: canonical.data,
                        },
                        false
                    );
                    setSpecErrors([]);
                    setQueryError(null);
                })
                .catch((error) => {
                    if (generation !== queryGeneration.current) return;
                    setRenderedSpec(null);
                    setData(null);
                    const errors = specErrorsFromAxios(error);
                    if (errors) {
                        setSpecErrors(errors);
                        setQueryError(null);
                    } else {
                        setQueryError(errorDetail(error, 'Could not build the chart.'));
                    }
                })
                .finally(() => {
                    if (generation === queryGeneration.current) setLoading(false);
                });
        },
        [
            queryGeneration,
            sessionId,
            setData,
            setDraft,
            setLoading,
            setQueryError,
            setRenderedSpec,
            setSpecErrors,
            specRef,
            titleRef,
        ]
    );

    const scheduleQuery = useCallback(
        (next: VizSpec) => {
            queryGeneration.current += 1;
            if (debounceTimer.current) clearTimeout(debounceTimer.current);
            debounceTimer.current = setTimeout(() => {
                debounceTimer.current = null;
                runQuery(next);
            }, QUERY_DEBOUNCE_MS);
        },
        [debounceTimer, queryGeneration, runQuery]
    );

    const applySpec = useCallback(
        (next: VizSpec) => {
            const previous = specRef.current;
            setDraft(next, true);
            setConflict(null);
            if (hasSameQueryShape(previous, next)) {
                setRenderedSpec((current) =>
                    current ? applyPresentation(current, next) : current
                );
            } else {
                scheduleQuery(next);
            }
        },
        [scheduleQuery, setConflict, setDraft, setRenderedSpec, specRef]
    );

    const loadVisualization = useCallback(
        async (id: string, discardStoredDraft: boolean) => {
            cancelPending();
            const generation = transitionGeneration.current;
            setLoading(true);
            setSpecErrors([]);
            setQueryError(null);
            try {
                const knownRow = savedRef.current.find((item) => item.id === id);
                const [response, fetchedRow] = await Promise.all([
                    getVisualizationData(id),
                    knownRow ? Promise.resolve(knownRow) : getVisualization(id),
                ]);
                if (generation !== transitionGeneration.current) return;
                const row = fetchedRow ?? knownRow;
                const canonical = adoptRowTitle(response.spec, row?.title);
                const stored = discardStoredDraft ? null : readVizDraft(sessionId, id);
                const nextDraft = stored?.spec ?? canonical;
                resetStacks();
                setActive(id, row?.version ?? stored?.baseVersion ?? null);
                setRenderedSpec(canonical);
                setData(response.data);
                setDraft(nextDraft, Boolean(stored));
                setDirtyState(Boolean(stored));
                setConflict(null);
                rememberActiveViz(sessionId, id);
                if (discardStoredDraft) clearVizDraft(sessionId, id);
                if (stored) scheduleQuery(nextDraft);
            } catch (error) {
                if (generation !== transitionGeneration.current) return;
                setQueryError(errorDetail(error, 'Could not load this chart.'));
            } finally {
                if (generation === transitionGeneration.current) setLoading(false);
            }
        },
        [
            cancelPending,
            resetStacks,
            savedRef,
            scheduleQuery,
            sessionId,
            setActive,
            setConflict,
            setData,
            setDirtyState,
            setDraft,
            setLoading,
            setQueryError,
            setRenderedSpec,
            setSpecErrors,
            transitionGeneration,
        ]
    );

    const selectViz = useCallback(
        async (id: string) => {
            if (id === activeIdRef.current) return;
            await loadVisualization(id, false);
        },
        [activeIdRef, loadVisualization]
    );

    const newViz = useCallback(
        (source = 'active') => {
            cancelPending();
            resetStacks();
            clearVizDraft(sessionId, null);
            const currentTitle = activeIdRef.current || !dirtyRef.current ? '' : titleRef.current;
            const next = {
                ...emptySpec(),
                title: nextNewChartTitle(savedRef.current, currentTitle),
                data: { source },
            };
            setActive(null, null);
            setDraft(next, false);
            setDirtyState(false);
            setRenderedSpec(null);
            setData(null);
            setSpecErrors([]);
            setQueryError(null);
            setConflict(null);
            rememberActiveViz(sessionId, null);
        },
        [
            cancelPending,
            activeIdRef,
            dirtyRef,
            resetStacks,
            savedRef,
            sessionId,
            setActive,
            setConflict,
            setData,
            setDirtyState,
            setDraft,
            setQueryError,
            setRenderedSpec,
            setSpecErrors,
            titleRef,
        ]
    );

    const refreshSaved = useCallback(() => {
        const generation = sessionGeneration.current;
        listVisualizations(sessionId)
            .then((rows) => {
                if (generation !== sessionGeneration.current) return;
                savedRef.current = rows;
                setSaved(rows);
                const active = activeIdRef.current;
                const remote = active ? rows.find((row) => row.id === active) : null;
                if (remote && dirtyRef.current && remote.version !== versionRef.current) {
                    setConflict({
                        kind: 'remote-update',
                        message: 'This chart changed elsewhere while you were editing.',
                        serverVersion: remote.version,
                    });
                } else if (remote && !dirtyRef.current && remote.version !== versionRef.current) {
                    loadVisualization(remote.id, true).catch(() => undefined);
                }
            })
            .catch(() => undefined);
    }, [
        activeIdRef,
        dirtyRef,
        loadVisualization,
        savedRef,
        sessionGeneration,
        sessionId,
        setConflict,
        setSaved,
        versionRef,
    ]);

    return {
        runQuery,
        scheduleQuery,
        applySpec,
        loadVisualization,
        selectViz,
        newViz,
        refreshSaved,
    };
}
