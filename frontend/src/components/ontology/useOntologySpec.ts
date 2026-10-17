/**
 * Editing engine for the Ontology tab.
 *
 * Mirrors useVizSpec's split: `spec` is the live draft every control writes to,
 * `saved` is what the server last confirmed. Edits are local and explicit-save
 * (unlike the viz editor's debounced query loop) because an ontology edit has no
 * cheap preview to run on every keystroke — the expensive round trip is a metric
 * query, which the user asks for deliberately.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
    certifyDefinitions,
    getOntology,
    restoreOntologyVersion,
    saveOntology,
} from '../../services/ontology';
import {
    emptySpec,
    type DatasetInfo,
    type OntologyErrorItem,
    type OntologySpec,
    type Selection,
} from './types';

const UNDO_LIMIT = 50;

interface Conflict {
    expected: number;
    current: number;
}

/**
 * The ontology is per user, not per session. `sessionId` is only the context
 * the server validates the spec against (which datasets exist, what counts as
 * a missing reference), so it may be null.
 */
export function useOntologySpec(sessionId: string | null) {
    const [ontologyId, setOntologyId] = useState<string | null>(null);
    const [spec, setSpec] = useState<OntologySpec>(emptySpec);
    const [savedSpec, setSavedSpec] = useState<OntologySpec>(emptySpec);
    const [version, setVersion] = useState(0);
    const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
    const [missingDatasets, setMissingDatasets] = useState<string[]>([]);
    const [errors, setErrors] = useState<OntologyErrorItem[]>([]);
    const [selection, setSelection] = useState<Selection>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saveError, setSaveError] = useState<string | null>(null);
    const [conflict, setConflict] = useState<Conflict | null>(null);

    const undoStack = useRef<OntologySpec[]>([]);
    const redoStack = useRef<OntologySpec[]>([]);
    const [historyTick, setHistoryTick] = useState(0);

    const dirty = JSON.stringify(spec) !== JSON.stringify(savedSpec);

    const applyModel = useCallback(
        (model: {
            id: string;
            spec: OntologySpec;
            version: number;
            errors?: OntologyErrorItem[];
            datasets?: DatasetInfo[];
            missing_datasets?: string[];
        }) => {
            setOntologyId(model.id);
            setSpec(model.spec);
            setSavedSpec(model.spec);
            setVersion(model.version);
            setErrors(model.errors ?? []);
            if (model.datasets) setDatasets(model.datasets);
            if (model.missing_datasets) setMissingDatasets(model.missing_datasets);
            setConflict(null);
        },
        []
    );

    const load = useCallback(async () => {
        setLoading(true);
        // 404 is no longer retried: the backend now serves a session with no
        // AgentState instead of rejecting it, so a 404 here is a real error, not
        // a race. Only 401 is genuinely transient, while auth settles on load.
        const RETRY_STATUSES = [401];
        const MAX_ATTEMPTS = 3;
        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
            try {
                applyModel(await getOntology(sessionId));
                setLoadError(null);
                setLoading(false);
                return;
            } catch (err: unknown) {
                const status = (err as { response?: { status?: number } })?.response?.status;
                const retryable = status !== undefined && RETRY_STATUSES.includes(status);
                if (!retryable || attempt === MAX_ATTEMPTS) {
                    setLoadError('Could not load the ontology for this session.');
                    setLoading(false);
                    return;
                }
                await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
            }
        }
    }, [sessionId, applyModel]);

    useEffect(() => {
        void load();
    }, [load]);

    /** Every mutation goes through here so undo history is never bypassed. */
    const updateSpec = useCallback(
        (next: OntologySpec | ((prev: OntologySpec) => OntologySpec)) => {
            setSpec((prev) => {
                const resolved = typeof next === 'function' ? next(prev) : next;
                if (JSON.stringify(resolved) === JSON.stringify(prev)) return prev;
                undoStack.current = [...undoStack.current, prev].slice(-UNDO_LIMIT);
                redoStack.current = [];
                setHistoryTick((t) => t + 1);
                return resolved;
            });
        },
        []
    );

    const undo = useCallback(() => {
        setSpec((prev) => {
            const previous = undoStack.current.pop();
            if (!previous) return prev;
            redoStack.current = [...redoStack.current, prev].slice(-UNDO_LIMIT);
            setHistoryTick((t) => t + 1);
            return previous;
        });
    }, []);

    const redo = useCallback(() => {
        setSpec((prev) => {
            const next = redoStack.current.pop();
            if (!next) return prev;
            undoStack.current = [...undoStack.current, prev].slice(-UNDO_LIMIT);
            setHistoryTick((t) => t + 1);
            return next;
        });
    }, []);

    const save = useCallback(
        async (summary?: string) => {
            setSaving(true);
            setSaveError(null);
            try {
                const result = await saveOntology(sessionId, spec, version, summary);
                setSavedSpec(result.spec);
                setSpec(result.spec);
                setVersion(result.version);
                setErrors(result.errors ?? []);
                setConflict(null);
                return true;
            } catch (err: unknown) {
                const detail = (
                    err as { response?: { status?: number; data?: { detail?: unknown } } }
                )?.response;
                if (detail?.status === 409) {
                    const info = detail.data?.detail as Conflict | undefined;
                    setConflict({
                        expected: info?.expected ?? version,
                        current: info?.current ?? version,
                    });
                    setSaveError(
                        'Someone else (or the agent) changed this ontology. Reload to see their version.'
                    );
                } else {
                    setSaveError('Could not save the ontology.');
                }
                return false;
            } finally {
                setSaving(false);
            }
        },
        [sessionId, spec, version]
    );

    const certify = useCallback(async (names: string[], certified = true) => {
        if (names.length === 0) return;
        setSaving(true);
        try {
            const result = await certifyDefinitions(names, certified);
            setSpec(result.spec);
            setSavedSpec(result.spec);
            setVersion(result.version);
            setErrors(result.errors ?? []);
        } catch {
            setSaveError('Could not update certification.');
        } finally {
            setSaving(false);
        }
    }, []);

    const restore = useCallback(
        async (versionN: number) => {
            if (!ontologyId) return;
            setSaving(true);
            try {
                const result = await restoreOntologyVersion(ontologyId, versionN);
                setSpec(result.spec);
                setSavedSpec(result.spec);
                setVersion(result.version);
                await load();
            } catch {
                setSaveError('Could not restore that version.');
            } finally {
                setSaving(false);
            }
        },
        [ontologyId, load]
    );

    const discard = useCallback(() => {
        undoStack.current = [];
        redoStack.current = [];
        setHistoryTick((t) => t + 1);
        setSpec(savedSpec);
    }, [savedSpec]);

    return {
        ontologyId,
        spec,
        updateSpec,
        version,
        datasets,
        missingDatasets,
        errors,
        selection,
        setSelection,
        loading,
        saving,
        dirty,
        loadError,
        saveError,
        setSaveError,
        conflict,
        save,
        certify,
        restore,
        reload: load,
        discard,
        undo,
        redo,
        canUndo: undoStack.current.length > 0,
        canRedo: redoStack.current.length > 0,
        historyTick,
    };
}
