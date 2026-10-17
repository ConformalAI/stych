import { useCallback } from 'react';
import { useStore } from '../../store/useStore';
import { createVisualization, deleteVisualization, updateVisualization } from '../../services/viz';
import { specErrorsFromAxios, type SavedViz, type VizData } from './types';
import { clearVizDraft, rememberActiveViz } from './vizDraftStorage';
import {
    adoptRowTitle,
    errorDetail,
    isConflictResponse,
    nextNewChartTitle,
    type PersistenceContext,
} from './useVizSpecTypes';

export function useVizPersistenceActions(context: PersistenceContext) {
    const {
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
        newViz,
        loadVisualization,
    } = context;

    const commitSaved = useCallback(
        (
            response: { visualization: SavedViz; data: VizData },
            oldId: string | null,
            action: 'created' | 'updated'
        ) => {
            const row = response.visualization;
            useStore.getState().notifySessionActivity(sessionId);
            const canonical = adoptRowTitle(row.spec, row.title);
            clearVizDraft(sessionId, oldId);
            clearVizDraft(sessionId, row.id);
            setActive(row.id, row.version);
            setDraft(canonical, false);
            setDirtyState(false);
            setRenderedSpec(canonical);
            setData(response.data);
            setConflict(null);
            setSpecErrors([]);
            setQueryError(null);
            rememberActiveViz(sessionId, row.id);
            savedRef.current = savedRef.current.some((item) => item.id === row.id)
                ? savedRef.current.map((item) => (item.id === row.id ? row : item))
                : [...savedRef.current, row];
            setSaved(savedRef.current);
            useStore.getState().setLastVizEvent({
                sessionId,
                vizId: row.id,
                action,
                source: 'user',
                version: row.version,
                at: Date.now(),
            });
        },
        [
            savedRef,
            sessionId,
            setActive,
            setConflict,
            setData,
            setDirtyState,
            setDraft,
            setQueryError,
            setRenderedSpec,
            setSaved,
            setSpecErrors,
        ]
    );

    const saveAsCopy = useCallback(async () => {
        const title = titleRef.current || nextNewChartTitle(savedRef.current);
        const draft = { ...specRef.current, title };
        setSaving(true);
        try {
            const response = await createVisualization(sessionId, draft, title);
            commitSaved(response, activeIdRef.current, 'created');
        } catch (error) {
            const errors = specErrorsFromAxios(error);
            if (errors) setSpecErrors(errors);
            else setQueryError(errorDetail(error, 'Could not save the chart.'));
            throw error;
        } finally {
            setSaving(false);
        }
    }, [
        activeIdRef,
        commitSaved,
        savedRef,
        sessionId,
        setQueryError,
        setSaving,
        setSpecErrors,
        specRef,
        titleRef,
    ]);

    const save = useCallback(async () => {
        const id = activeIdRef.current;
        if (!id) return saveAsCopy();
        const draft = { ...specRef.current, title: titleRef.current || null };
        setSaving(true);
        try {
            const response = await updateVisualization(
                id,
                draft,
                titleRef.current || null,
                versionRef.current
            );
            commitSaved(response, id, 'updated');
        } catch (error) {
            if (isConflictResponse(error)) {
                refreshSaved();
                setConflict({
                    kind: 'save-conflict',
                    message: 'A newer version was saved before yours.',
                    serverVersion: null,
                });
            } else {
                const errors = specErrorsFromAxios(error);
                if (errors) setSpecErrors(errors);
                else setQueryError(errorDetail(error, 'Could not save the chart.'));
            }
            throw error;
        } finally {
            setSaving(false);
        }
    }, [
        activeIdRef,
        commitSaved,
        refreshSaved,
        saveAsCopy,
        setConflict,
        setQueryError,
        setSaving,
        setSpecErrors,
        specRef,
        titleRef,
        versionRef,
    ]);

    const reloadRemote = useCallback(async () => {
        const id = activeIdRef.current;
        if (!id) {
            clearVizDraft(sessionId, null);
            newViz();
            return;
        }
        await loadVisualization(id, true);
    }, [activeIdRef, loadVisualization, newViz, sessionId]);

    const discardDraft = useCallback(async () => {
        clearVizDraft(sessionId, activeIdRef.current);
        await reloadRemote();
    }, [activeIdRef, reloadRemote, sessionId]);

    const keepDraft = useCallback(() => setConflict(null), [setConflict]);

    const remove = useCallback(async () => {
        const id = activeIdRef.current;
        if (!id) {
            clearVizDraft(sessionId, null);
            newViz();
            return;
        }
        await deleteVisualization(id);
        clearVizDraft(sessionId, id);
        savedRef.current = savedRef.current.filter((item) => item.id !== id);
        setSaved(savedRef.current);
        newViz();
    }, [activeIdRef, newViz, savedRef, sessionId, setSaved]);

    return { save, saveAsCopy, reloadRemote, discardDraft, keepDraft, remove };
}
