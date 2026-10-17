import { useEffect, useRef } from 'react';
import { getVizCatalog, listVisualizations, validateCalculations } from '../../services/viz';
import { SOURCE_REMOVED_MESSAGE, isSourceRemovedError } from '../../sourceRemoved';
import { emptySpec, isCalculate } from './types';
import { persistVizDraft, readActiveVizId, readVizDraft } from './vizDraftStorage';
import {
    filterSavedVisualizations,
    errorDetail,
    nextNewChartTitle,
    type LifecycleContext,
} from './useVizSpecTypes';

export function useVizSpecLifecycle(context: LifecycleContext): void {
    const {
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
    } = context;
    const initialOpenVizId = useRef(openVizId);

    useEffect(() => {
        cancelPending();
        const generation = ++sessionGeneration.current;
        const rememberedId = readActiveVizId(sessionId);
        const stored = readVizDraft(sessionId, rememberedId);
        resetStacks();
        setCatalog(null);
        setCatalogError(null);
        setSaved([]);
        savedRef.current = [];
        setRenderedSpec(null);
        setData(null);
        setSpecErrors([]);
        setQueryError(null);
        setConflict(null);

        if (stored) {
            setActive(stored.activeVizId, stored.baseVersion);
            setDraft(stored.spec, true);
            setDirtyState(true);
            scheduleQuery(stored.spec);
        } else {
            setActive(null, null);
            setDraft({ ...emptySpec(), title: nextNewChartTitle([]) }, false);
            setDirtyState(false);
        }

        Promise.all([
            listVisualizations(sessionId),
            getVizCatalog(sessionId).catch((error) => {
                if (isSourceRemovedError(error)) {
                    setCatalogError(SOURCE_REMOVED_MESSAGE);
                    return null;
                }
                throw error;
            }),
        ])
            .then(([rows, initialCatalog]) => {
                if (generation !== sessionGeneration.current) return;
                savedRef.current = rows;
                setSaved(rows);
                if (stored) {
                    const remote = stored.activeVizId
                        ? rows.find((row) => row.id === stored.activeVizId)
                        : null;
                    if (stored.activeVizId && (!remote || remote.version !== stored.baseVersion)) {
                        setConflict({
                            kind: 'remote-update',
                            message: remote
                                ? 'This chart changed while your draft was saved.'
                                : 'The saved chart for this draft is no longer available.',
                            serverVersion: remote?.version ?? null,
                        });
                    }
                    return;
                }
                if (initialOpenVizId.current) return;
                if (!initialCatalog) return;
                const initialSource = initialCatalog.source ?? 'active';
                const sourceRows = filterSavedVisualizations(rows, initialSource);
                const requested =
                    rememberedId && sourceRows.some((row) => row.id === rememberedId)
                        ? rememberedId
                        : [...sourceRows].sort((a, b) =>
                              (b.updated_at ?? b.created_at ?? '').localeCompare(
                                  a.updated_at ?? a.created_at ?? ''
                              )
                          )[0]?.id;
                if (requested) loadVisualization(requested, false).catch(() => undefined);
            })
            .catch(() => undefined);

        return () => {
            sessionGeneration.current += 1;
            cancelPending();
        };
    }, [
        cancelPending,
        loadVisualization,
        resetStacks,
        scheduleQuery,
        sessionId,
        setActive,
        setCatalog,
        setCatalogError,
        setConflict,
        setData,
        setDirtyState,
        setDraft,
        setQueryError,
        setRenderedSpec,
        setSaved,
        setSpecErrors,
        sessionGeneration,
        savedRef,
    ]);

    const prevUploadedIds = useRef(uploadedFileIds);
    useEffect(() => {
        const prev = new Set(prevUploadedIds.current.split(',').filter(Boolean));
        const next = new Set(uploadedFileIds.split(',').filter(Boolean));
        prevUploadedIds.current = uploadedFileIds;
        const removed = [...prev].filter((id) => !next.has(id));
        if (removed.length === 0) return;
        const source = specRef.current.data?.source;
        const sourceGone =
            (source && source !== 'active' && removed.includes(source)) ||
            ((!source || source === 'active') && next.size === 0 && prev.size > 0);
        if (!sourceGone) return;
        const remaining = uploadedFileIds.split(',').filter(Boolean)[0];
        setActive(null, null);
        setDirtyState(false);
        setRenderedSpec(null);
        setData(null);
        if (remaining) {
            setDraft(
                {
                    ...emptySpec(),
                    data: { source: remaining },
                    title: nextNewChartTitle(savedRef.current),
                },
                false
            );
            setCatalogError(null);
        } else {
            setDraft({ ...emptySpec(), title: nextNewChartTitle([]) }, false);
            setCatalog(null);
            setCatalogError(SOURCE_REMOVED_MESSAGE);
        }
    }, [
        savedRef,
        setActive,
        setCatalog,
        setCatalogError,
        setData,
        setDirtyState,
        setDraft,
        setRenderedSpec,
        specRef,
        uploadedFileIds,
    ]);

    const source = spec.data?.source ?? 'active';
    useEffect(() => {
        let live = true;
        setCatalog(null);
        setCatalogError(null);
        setCalcFields([]);
        setCalcErrors([]);
        getVizCatalog(sessionId, source)
            .then((nextCatalog) => {
                if (live) setCatalog(nextCatalog);
            })
            .catch((error) => {
                if (!live) return;
                setCatalogError(
                    isSourceRemovedError(error)
                        ? SOURCE_REMOVED_MESSAGE
                        : errorDetail(error, 'Could not load fields.')
                );
            });
        return () => {
            live = false;
        };
    }, [sessionId, source, setCalcErrors, setCalcFields, setCatalog, setCatalogError]);

    useEffect(() => {
        // Session changes render once with the previous session's React state.
        // Read the synchronized refs because the reset effect above updates them
        // before this persistence effect runs.
        if (draftSessionRef.current !== sessionId || !dirtyRef.current) return;
        const current = specRef.current;
        persistVizDraft(sessionId, {
            activeVizId: activeIdRef.current,
            baseVersion: versionRef.current,
            spec: current,
            title: current.title ?? '',
        });
    }, [
        activeIdRef,
        activeVersion,
        activeVizId,
        dirty,
        dirtyRef,
        draftSessionRef,
        sessionId,
        spec,
        specRef,
        title,
        versionRef,
    ]);

    const calculationsKey = JSON.stringify((spec.transform ?? []).filter(isCalculate));
    useEffect(() => {
        const calculations = (specRef.current.transform ?? []).filter(isCalculate);
        if (calculations.length === 0) {
            setCalcFields([]);
            setCalcErrors([]);
            return;
        }
        let live = true;
        validateCalculations(sessionId, calculations, source)
            .then((response) => {
                if (!live) return;
                setCalcFields(response.fields);
                setCalcErrors(response.errors);
            })
            .catch(() => undefined);
        return () => {
            live = false;
        };
    }, [calculationsKey, sessionId, setCalcErrors, setCalcFields, source, specRef]);

    useEffect(() => {
        if (!lastVizEvent || lastVizEvent.sessionId !== sessionId) return;
        if (lastVizEvent.source === 'user') return;
        const generation = sessionGeneration.current;
        listVisualizations(sessionId)
            .then((rows) => {
                if (generation !== sessionGeneration.current) return;
                savedRef.current = rows;
                setSaved(rows);
                const eventId = lastVizEvent.vizId;
                if (!eventId) return;
                const row = rows.find((item) => item.id === eventId);
                if (eventId === activeIdRef.current && dirtyRef.current) {
                    setConflict({
                        kind: 'remote-update',
                        message: 'This chart changed elsewhere while you were editing.',
                        serverVersion: row?.version ?? null,
                    });
                    return;
                }
                if (eventId === activeIdRef.current) {
                    loadVisualization(eventId, true).catch(() => undefined);
                } else if (
                    lastVizEvent.action === 'created' &&
                    !activeIdRef.current &&
                    !dirtyRef.current
                ) {
                    loadVisualization(eventId, false).catch(() => undefined);
                }
            })
            .catch(() => undefined);
    }, [
        activeIdRef,
        dirtyRef,
        lastVizEvent,
        loadVisualization,
        savedRef,
        sessionGeneration,
        sessionId,
        setConflict,
        setSaved,
    ]);

    useEffect(() => {
        if (!openVizId) return;
        let live = true;
        selectViz(openVizId)
            .then(() => {
                if (live && activeIdRef.current === openVizId) setOpenVizId(null);
            })
            .catch(() => undefined);
        return () => {
            live = false;
        };
    }, [activeIdRef, openVizId, selectViz, setOpenVizId]);

    void versionRef;
}
